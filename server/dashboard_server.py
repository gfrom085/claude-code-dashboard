#!/usr/bin/env python3
"""
Token metrics dashboard server.
Serves the dashboard HTML and the sidecar JSON from a single port.

Usage: python3 ~/.claude/state/dashboard_server.py [port]
Default port: 8765
"""

import itertools
import json
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

SIDECAR_FILE = Path("/tmp/langfuse-token-metrics.json")
DASHBOARD_FILE = Path(__file__).parent / "token-dashboard.html"
PROJECTS_DIR = Path.home() / ".claude" / "projects"
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8765


def scan_task_counts() -> dict:
    """Scan active session transcripts for Task tool calls.

    Returns {session_id: {turn_n: {"count": N, "agents": [{"desc": ..., "type": ...}]}}}
    for sessions present in the sidecar. Only scans sessions visible in the sidecar.
    """
    if not SIDECAR_FILE.exists():
        return {}
    try:
        sidecar = json.loads(SIDECAR_FILE.read_text())
    except (json.JSONDecodeError, IOError):
        return {}

    # Build sessionId → transcript file mapping
    sid_to_file: dict[str, Path] = {}
    if PROJECTS_DIR.exists():
        for project_dir in PROJECTS_DIR.iterdir():
            if not project_dir.is_dir():
                continue
            for tf in itertools.chain(project_dir.glob("*.jsonl"), project_dir.glob("*/subagents/agent-*.jsonl")):
                try:
                    first = json.loads(tf.read_text().split("\n")[0])
                    sid = first.get("sessionId", tf.stem)
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
    def log_message(self, fmt, *args):
        # Suppress default access logs (too noisy)
        pass

    def send_json(self, data: dict | list, status: int = 200) -> None:
        body = json.dumps(data, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
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
            if SIDECAR_FILE.exists():
                try:
                    data = json.loads(SIDECAR_FILE.read_text())
                    self.send_json(data)
                except (json.JSONDecodeError, IOError) as e:
                    self.send_json({"error": str(e)}, 500)
            else:
                self.send_json({})

        elif path == "/api/task-counts":
            self.send_json(scan_task_counts())

        elif path == "/api/cache-audit":
            self._handle_cache_audit()

        elif path == "/api/health":
            self.send_json({"ok": True, "sidecar_exists": SIDECAR_FILE.exists()})

        else:
            self.send_json({"error": "not found"}, 404)

    def _handle_cache_audit(self) -> None:
        """Run cache_audit.py and return results as JSON."""
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        script = Path(__file__).parent.parent / "scripts" / "cache_audit.py"
        if not script.exists():
            self.send_json({"error": "cache_audit.py not found"}, 404)
            return

        cmd = [sys.executable, str(script), "--no-write"]
        if "session" in params:
            cmd.extend(["--session", params["session"][0]])
        if "project" in params:
            cmd.extend(["--project", params["project"][0]])

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


def main():
    server = HTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Dashboard server running at http://localhost:{PORT}")
    print(f"Sidecar: {SIDECAR_FILE}")
    print(f"Dashboard: {DASHBOARD_FILE}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
