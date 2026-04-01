#!/usr/bin/env python3
"""
Token metrics dashboard server.
Serves the dashboard HTML and the sidecar JSON from a single port.

Usage: python3 ~/.claude/state/dashboard_server.py [port]
Default port: 8765
"""

import itertools
import json
import os
import socket as _socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs, urlparse

SIDECAR_FILE = Path("/tmp/langfuse-token-metrics.json")
SIDECAR_JSONL = Path("/tmp/langfuse-token-metrics.jsonl")
METRICS_DATA_DIR = Path.home() / "active-projects" / "claude-code-dashboard" / "data"
DASHBOARD_FILE = Path(__file__).parent / "token-dashboard.html"
PROJECTS_DIR = Path.home() / ".claude" / "projects"
METRICS_STREAM = Path("/tmp/token-metrics-stream.jsonl")
SKIPS_FILE = Path("/tmp/token-metrics-skips")
CLAUDE_USAGE_JSON = Path("/tmp/claude-ai-usage.json")
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8765

# Task cache with 30s TTL
_task_cache: dict = {"data": {}, "ts": 0.0}
TASK_CACHE_TTL = 30.0

# Thread-safe sidecar cache [red-team P2-S03]
_sidecar_cache = {"data": {}, "ts": 0.0}
_sidecar_cache_lock = threading.Lock()
SIDECAR_CACHE_TTL = 30.0

# SSE heartbeat interval
SSE_HEARTBEAT_S = 15


def read_sidecar_jsonl() -> dict:
    """Read JSONL sidecar, aggregate by session, return dict compatible with old format.
    Uses double-checked locking to prevent thundering herd [red-team P2-S03]."""
    now = time.time()
    if now - _sidecar_cache["ts"] < SIDECAR_CACHE_TTL:
        return _sidecar_cache["data"]

    with _sidecar_cache_lock:
        # Double-check after acquiring lock
        if now - _sidecar_cache["ts"] < SIDECAR_CACHE_TTL:
            return _sidecar_cache["data"]

        result = {}
        seven_days_ago = now - 7 * 86400  # 7-day sliding window [red-team S5]

        if SIDECAR_JSONL.exists():
            try:
                with open(SIDECAR_JSONL) as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            d = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if d.get("ts", 0) < seven_days_ago:
                            continue
                        sid = d.get("sid", "")
                        if sid not in result:
                            result[sid] = {
                                "type": d.get("type", "main"),
                                "project": d.get("project", ""),
                                "turns": [],
                                "last_seen": d.get("ts", 0),
                            }
                        result[sid]["turns"].append({
                            "n": d.get("turn", 0),
                            "ts": d.get("ts", 0),
                            "input": d.get("input", 0),
                            "output": d.get("output", 0),
                            "cache_read": d.get("cache_read", 0),
                            "cache_creation": d.get("cache_creation", 0),
                            "cache_5m": d.get("cache_5m", 0),
                            "cache_1h": d.get("cache_1h", 0),
                            "cache_savings_usd": d.get("cache_savings_usd", 0),
                            "cache_surcharge_usd": d.get("cache_surcharge_usd", 0),
                        })
                        if d.get("ts", 0) > result[sid]["last_seen"]:
                            result[sid]["last_seen"] = d["ts"]
            except IOError:
                pass

        _sidecar_cache["data"] = result
        _sidecar_cache["ts"] = time.time()
        return result


def warm_start_sidecar():
    """Rebuild sidecar from dated JSONL if sidecar is empty [red-team P2-04]."""
    if SIDECAR_JSONL.exists() and SIDECAR_JSONL.stat().st_size > 0:
        return  # sidecar already has data
    if not METRICS_DATA_DIR.exists():
        return
    # Find last 7 days of dated JSONL
    for i in range(7):
        date = datetime.now() - timedelta(days=i)
        dated_file = METRICS_DATA_DIR / f"token-metrics-{date.strftime('%Y-%m-%d')}.jsonl"
        if dated_file.exists():
            try:
                with open(dated_file) as src:
                    fd = os.open(str(SIDECAR_JSONL), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
                    try:
                        for line in src:
                            d = json.loads(line.strip())
                            sidecar_line = json.dumps({
                                "sid": d.get("session_id", ""),
                                "type": d.get("session_type", "main"),
                                "project": d.get("project", ""),
                                "turn": d.get("turn", 0),
                                "ts": d.get("ts", 0),
                                "input": d.get("input", 0),
                                "output": d.get("output", 0),
                                "cache_read": d.get("cache_read", 0),
                                "cache_creation": d.get("cache_creation", 0),
                                "cache_5m": d.get("cache_5m", 0),
                                "cache_1h": d.get("cache_1h", 0),
                            }, separators=(",", ":")) + "\n"
                            os.write(fd, sidecar_line.encode())
                    finally:
                        os.close(fd)
            except (IOError, json.JSONDecodeError):
                continue
    print(f"Warm-started sidecar from dated JSONL files")


def _check_metrics_health() -> dict:
    """Analyze both pipelines and return health status."""
    checks = {}
    now = time.time()

    # 1. Proxy reachability [red-team R10]
    try:
        sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        sock.settimeout(2)
        proxy_ok = sock.connect_ex(("127.0.0.1", 8082)) == 0
        sock.close()
    except Exception:
        proxy_ok = False
    checks["proxy_reachable"] = proxy_ok

    # 2. Stream freshness + idle detection [red-team R9]
    stream_age = None
    if METRICS_STREAM.exists():
        stream_age = now - METRICS_STREAM.stat().st_mtime
    checks["stream_age_s"] = stream_age

    # Check if any transcript was modified recently (idle detection)
    projects_dir = Path.home() / ".claude" / "projects"
    latest_transcript_mtime = 0
    if projects_dir.exists():
        for pd in projects_dir.iterdir():
            if pd.is_dir():
                for jf in pd.glob("*.jsonl"):
                    try:
                        mt = jf.stat().st_mtime
                        if mt > latest_transcript_mtime:
                            latest_transcript_mtime = mt
                    except OSError:
                        continue
    transcripts_active = (now - latest_transcript_mtime) < 120 if latest_transcript_mtime else False
    checks["transcripts_active"] = transcripts_active

    # 3. Read last 50 stream samples
    stream_samples = _read_last_samples(METRICS_STREAM, 50)
    checks["stream_sample_count"] = len(stream_samples)
    checks["stream_all_cache_1h_zero"] = all(s.get("cache_1h", 0) == 0 for s in stream_samples) if stream_samples else True
    checks["stream_all_output_zero"] = all(s.get("output", 0) == 0 for s in stream_samples) if stream_samples else True

    # 4. Read last 50 SSOT samples [red-team R2]
    today = datetime.now().strftime("%Y-%m-%d")
    ssot_file = METRICS_DATA_DIR / f"token-metrics-{today}.jsonl"
    ssot_samples = _read_last_samples(ssot_file, 50) if ssot_file.exists() else []
    checks["ssot_sample_count"] = len(ssot_samples)
    checks["ssot_all_cache_1h_zero"] = all(s.get("cache_1h", 0) == 0 for s in ssot_samples) if ssot_samples else True
    checks["ssot_all_output_zero"] = all(s.get("output", 0) == 0 for s in ssot_samples) if ssot_samples else True

    # 5. Cross-validation on output_tokens, 15min window [red-team R3]
    fifteen_min_ago = now - 900
    stream_output = sum(s.get("output", 0) for s in stream_samples if s.get("ts", 0) > fifteen_min_ago)
    ssot_output = sum(s.get("output", 0) for s in ssot_samples if s.get("ts", 0) > fifteen_min_ago)
    if stream_output > 0 and ssot_output > 0:
        divergence = abs(stream_output - ssot_output) / max(stream_output, ssot_output)
    else:
        divergence = 0
    checks["cross_val_divergence"] = round(divergence, 3)

    # 6. Schema version
    checks["stream_schema_v"] = stream_samples[-1].get("v") if stream_samples else None

    # 7. Skips [red-team P2-09: windowed]
    skips_degraded = 0
    if SKIPS_FILE.exists():
        try:
            sd = json.loads(SKIPS_FILE.read_text())
            # Only count if recent (15 min window)
            if now - sd.get("ts", 0) < 900:
                skips_degraded = sd.get("degraded", 0)
        except (json.JSONDecodeError, IOError):
            pass
    checks["skips_degraded"] = skips_degraded

    # 8. Thinking
    checks["all_thinking_zero"] = all(s.get("thinking_chars", 0) == 0 for s in stream_samples) if len(stream_samples) >= 20 else False

    # Determine status (worst wins)
    if not proxy_ok:
        status = "proxy_down"
    elif skips_degraded > 0:
        status = "drops"
    elif stream_age and stream_age > 120 and transcripts_active:
        status = "stale"
    elif stream_age and stream_age > 120 and not transcripts_active:
        status = "idle"
    # SSOT freshness [audit G2]
    elif ssot_file.exists() and (now - ssot_file.stat().st_mtime) > 300 and transcripts_active:
        status = "stale_ssot"
    elif checks.get("stream_all_output_zero") and checks.get("ssot_all_output_zero") and len(stream_samples) >= 50:
        status = "degraded_output"
    elif checks.get("stream_all_cache_1h_zero") and len(stream_samples) >= 50:
        status = "degraded_stream"
    elif checks.get("ssot_all_cache_1h_zero") and len(ssot_samples) >= 50:
        status = "degraded_ssot"
    elif divergence > 0.15:
        status = "drift"
    elif checks.get("stream_schema_v") is not None and checks["stream_schema_v"] < 2:
        status = "outdated"
    elif checks.get("all_thinking_zero"):
        status = "no_thinking"
    else:
        status = "ok"

    checks["status"] = status
    return checks


def _read_last_samples(path: Path, n: int) -> list:
    """Read last N valid JSON lines from a file."""
    if not path or not path.exists():
        return []
    try:
        lines = path.read_text().strip().split("\n")
        samples = []
        for line in lines[-n:]:
            try:
                samples.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return samples
    except IOError:
        return []


def scan_task_counts() -> dict:
    """Scan active session transcripts for Task tool calls.

    Returns {session_id: {turn_n: {"count": N, "agents": [{"desc": ..., "type": ...}]}}}
    for sessions present in the sidecar. Only scans sessions visible in the sidecar.
    """
    sidecar = read_sidecar_jsonl()
    if not sidecar:
        return {}

    # Build sessionId → transcript file mapping
    sid_to_file: dict[str, Path] = {}
    if PROJECTS_DIR.exists():
        for project_dir in PROJECTS_DIR.iterdir():
            if not project_dir.is_dir():
                continue
            for tf in itertools.chain(project_dir.glob("*.jsonl"), project_dir.glob("*/subagents/agent-*.jsonl")):
                try:
                    with open(tf) as f:
                        first = json.loads(f.readline())
                    raw_sid = first.get("sessionId", tf.stem)
                    is_sub = "/subagents/" in str(tf)
                    sid = f"{raw_sid}::{tf.stem}" if is_sub else raw_sid
                    if sid in sidecar:
                        if sid not in sid_to_file or tf.stat().st_mtime > sid_to_file[sid].stat().st_mtime:
                            sid_to_file[sid] = tf
                except (json.JSONDecodeError, IOError, IndexError):
                    continue

    result: dict[str, dict] = {}

    for sid, tf in sid_to_file.items():
        try:
            lines = tf.read_text().strip().split("\n")
        except IOError:
            continue

        # Mirror the hook's turn counting: a turn = user msg followed by
        # at least one assistant msg.  Consecutive user msgs without an
        # assistant response in between are merged (only the last one counts).
        turn_tasks: dict[str, dict] = {}
        turn_n = 0
        current_user = False
        has_assistant = False

        for line in lines:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            role = obj.get("type") or obj.get("message", {}).get("role", "")

            if role == "user":
                content = obj.get("message", {}).get("content", [])
                is_tool_result = isinstance(content, list) and any(
                    isinstance(b, dict) and b.get("type") == "tool_result"
                    for b in content
                )
                if not is_tool_result:
                    # Finalize previous turn if it had a user+assistant pair
                    if current_user and has_assistant:
                        turn_n += 1
                    current_user = True
                    has_assistant = False

            elif role == "assistant":
                has_assistant = True
                content = obj.get("message", {}).get("content", [])
                if isinstance(content, list) and current_user:
                    cur_turn = turn_n + 1  # prospective turn number
                    for b in content:
                        if (isinstance(b, dict)
                                and b.get("type") == "tool_use"
                                and b.get("name") == "Task"):
                            inp = b.get("input", {})
                            key = str(cur_turn)
                            if key not in turn_tasks:
                                turn_tasks[key] = {"count": 0, "agents": []}
                            turn_tasks[key]["count"] += 1
                            turn_tasks[key]["agents"].append({
                                "desc": inp.get("description", ""),
                                "type": inp.get("subagent_type", ""),
                            })

        # Finalize last turn
        if current_user and has_assistant:
            turn_n += 1

        if turn_tasks:
            result[sid] = turn_tasks

    return result


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002
        pass

    def send_json(self, data: dict | list, status: int = 200) -> None:
        body = json.dumps(data, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        # No CORS header — dashboard is served from same origin (localhost:PORT)
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, content: bytes, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        path = self.path.split("?")[0]

        if path == "/" or path == "/index.html":
            if DASHBOARD_FILE.exists():
                self.send_html(DASHBOARD_FILE.read_bytes())
            else:
                self.send_html(b"<h1>Dashboard not found</h1><p>Expected: " + str(DASHBOARD_FILE).encode() + b"</p>", 404)

        elif path == "/api/sidecar":
            data = read_sidecar_jsonl()
            self.send_json(data)

        elif path == "/api/task-counts":
            now = time.time()
            if now - _task_cache["ts"] > TASK_CACHE_TTL:
                _task_cache["data"] = scan_task_counts()
                _task_cache["ts"] = now
            self.send_json(_task_cache["data"])

        elif path == "/api/cache-audit":
            self._handle_cache_audit()

        elif path == "/api/stream":
            self._handle_sse()

        elif path == "/api/metrics-health":
            self.send_json(_check_metrics_health())

        elif path == "/api/claude-usage":
            self._handle_claude_usage()

        elif path == "/api/health":
            health = {"ok": True, "sidecar_exists": SIDECAR_FILE.exists(),
                      "sidecar_jsonl_exists": SIDECAR_JSONL.exists(),
                      "metrics_stream_exists": METRICS_STREAM.exists()}
            # Add claude.ai bridge status
            if CLAUDE_USAGE_JSON.exists():
                try:
                    cu = json.loads(CLAUDE_USAGE_JSON.read_text())
                    polled_at = cu.get("polled_at", "")
                    breaker = cu.get("breaker", "unknown")
                    age = time.time() - CLAUDE_USAGE_JSON.stat().st_mtime
                    health["claude_ai_bridge"] = {
                        "ok": breaker == "ok" and age < 600,
                        "breaker": breaker,
                        "reason": cu.get("reason"),
                        "polled_at": polled_at,
                        "age_seconds": round(age),
                    }
                except (json.JSONDecodeError, OSError):
                    health["claude_ai_bridge"] = {"ok": False, "breaker": "read_error"}
            else:
                health["claude_ai_bridge"] = {"ok": False, "breaker": "no_data"}
            self.send_json(health)

        else:
            self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/api/session-cost":
            self._handle_session_cost()
        else:
            self.send_json({"error": "not found"}, 404)

    def _handle_session_cost(self) -> None:
        """Append session cost snapshot to dated JSONL."""
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            data = json.loads(body)
            today = datetime.now().strftime("%Y-%m-%d")
            cost_file = METRICS_DATA_DIR / f"cost-sessions-{today}.jsonl"
            METRICS_DATA_DIR.mkdir(parents=True, exist_ok=True)
            line = (json.dumps(data, separators=(",", ":")) + "\n").encode()
            fd = os.open(str(cost_file), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
            try:
                os.write(fd, line)
            finally:
                os.close(fd)
            self.send_json({"ok": True})
        except Exception as e:
            self.send_json({"error": str(e)}, 500)

    def _handle_sse(self) -> None:
        """Stream token metrics via Server-Sent Events.

        Tails /tmp/token-metrics-stream.jsonl and pushes new lines as events.
        Uses raw socket sendall to bypass BufferedWriter buffering.
        """
        self.request.setsockopt(_socket.IPPROTO_TCP, _socket.TCP_NODELAY, 1)

        # Send HTTP headers via raw socket to avoid wfile buffering
        header = (
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: text/event-stream\r\n"
            "Cache-Control: no-cache\r\n"
            "Connection: keep-alive\r\n"
            "\r\n"
        )
        self.request.sendall(header.encode())

        sock = self.request  # raw socket for unbuffered writes

        # Replay existing stream data so gauges rebuild accumulators on reconnect
        pos = 0
        inode = 0
        if METRICS_STREAM.exists():
            stat = METRICS_STREAM.stat()
            inode = stat.st_ino
            try:
                with open(METRICS_STREAM) as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            sock.sendall(f"event: catchup\ndata: {line}\n\n".encode())
                    pos = f.tell()
            except (IOError, OSError):
                pos = stat.st_size  # fallback: skip to end

        last_heartbeat = time.time()

        try:
            while True:
                # Check if file was rotated (inode changed or file shrank)
                if METRICS_STREAM.exists():
                    stat = METRICS_STREAM.stat()
                    if stat.st_ino != inode or stat.st_size < pos:
                        pos = 0
                        inode = stat.st_ino

                    if stat.st_size > pos:
                        with open(METRICS_STREAM) as f:
                            f.seek(pos)
                            new_data = f.read()
                            pos = f.tell()
                        for line in new_data.strip().split("\n"):
                            if line.strip():
                                sock.sendall(f"event: token\ndata: {line}\n\n".encode())

                now = time.time()
                if now - last_heartbeat >= SSE_HEARTBEAT_S:
                    sock.sendall(b": heartbeat\n\n")
                    last_heartbeat = now

                time.sleep(0.5)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # client disconnected

    def _handle_claude_usage(self) -> None:
        """Return claude.ai usage data from the poller's snapshot file."""
        if not CLAUDE_USAGE_JSON.exists():
            self.send_json({"bridge_ok": False, "error": "Poller not running — no data file"}, 503)
            return
        try:
            data = json.loads(CLAUDE_USAGE_JSON.read_text())
            age = time.time() - CLAUDE_USAGE_JSON.stat().st_mtime
            data["bridge_ok"] = data.get("breaker") == "ok" and age < 600
            data["age_seconds"] = round(age)
            self.send_json(data)
        except (json.JSONDecodeError, OSError) as e:
            self.send_json({"bridge_ok": False, "error": str(e)}, 500)

    def _handle_cache_audit(self) -> None:
        """Run cache_audit.py and return results as JSON."""
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        script = Path(__file__).parent.parent / "scripts" / "cache_audit.py"
        if not script.exists():
            self.send_json({"error": "cache_audit.py not found"}, 404)
            return

        import re
        SAFE_PARAM = re.compile(r"^[a-zA-Z0-9._\-]{1,128}$")

        cmd = [sys.executable, str(script), "--no-write"]
        if "session" in params:
            val = params["session"][0]
            if not SAFE_PARAM.match(val):
                self.send_json({"error": "invalid session param"}, 400)
                return
            cmd.extend(["--session", val])
        if "project" in params:
            val = params["project"][0]
            if not SAFE_PARAM.match(val):
                self.send_json({"error": "invalid project param"}, 400)
                return
            cmd.extend(["--project", val])

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=30,
            )
            # Parse JSONL output (stdout lines are JSON objects)
            events = []
            metrics = []
            for line in result.stdout.strip().split("\n"):
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if obj.get("_type") == "session_metrics":
                        metrics.append(obj)
                    else:
                        events.append(obj)
                except json.JSONDecodeError:
                    continue

            self.send_json({"events": events, "metrics": metrics})
        except subprocess.TimeoutExpired:
            self.send_json({"error": "cache audit timed out"}, 504)
        except Exception as e:
            self.send_json({"error": str(e)}, 500)


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


def main():
    server = ThreadedHTTPServer(("127.0.0.1", PORT), Handler)
    warm_start_sidecar()
    print(f"Dashboard server running at http://localhost:{PORT}")
    print(f"Sidecar JSONL: {SIDECAR_JSONL}")
    print(f"Dashboard: {DASHBOARD_FILE}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
