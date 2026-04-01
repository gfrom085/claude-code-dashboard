#!/usr/bin/env python3
"""
Claude.ai Usage Poller — Chrome Cookie Bridge

Reads Chrome's encrypted cookies (via GNOME Keyring), then polls claude.ai
usage API directly via HTTP requests.

Writes results to:
  - /tmp/claude-ai-usage.json  (latest snapshot for REST API)
  - /tmp/token-metrics-stream.jsonl  (SSE stream line for gauges)

Circuit breaker: stops on 401/403/429, org change, schema break, or 2 consecutive errors.
Restart manually after investigating.

Requirements (in project .venv):
  pip install secretstorage cryptography

Usage:
  .venv/bin/python3 lib/claude_ai_poller.py [--interval 120] [--high-threshold 80]
  # Or as daemon:
  nohup .venv/bin/python3 lib/claude_ai_poller.py > /tmp/claude-ai-poller.log 2>&1 &
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

USAGE_JSON = Path("/tmp/claude-ai-usage.json")
STREAM_JSONL = Path("/tmp/token-metrics-stream.jsonl")
STREAM_JSONL_NEW = Path("/tmp/token-metrics-stream.jsonl.new")
CHROME_COOKIES_DB = Path.home() / ".config/google-chrome/Default/Cookies"
BASE_URL = "https://claude.ai"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
BOUNDARY_DELTA_MIN_H = 4   # ignore resets_at shifts < 4h
BOUNDARY_FUTURE_MARGIN_H = 1  # new resets_at must be > now - 1h
GAP_THRESHOLD_H = 10  # log gap warning if delta > 10h
MAX_BAK_FILES = 3


def _get_chrome_aes_key() -> bytes:
    """Get Chrome's AES-128-CBC key from GNOME Keyring via gi.Secret."""
    import gi
    gi.require_version("Secret", "1")
    from gi.repository import Secret

    schema = Secret.Schema.new(
        "chrome_libsecret_os_crypt_password_v2",
        Secret.SchemaFlags.DONT_MATCH_NAME,
        {"application": Secret.SchemaAttributeType.STRING},
    )
    password = Secret.password_lookup_sync(schema, {"application": "chrome"}, None)
    if not password:
        raise RuntimeError("Chrome Safe Storage key not found in GNOME Keyring")
    return hashlib.pbkdf2_hmac("sha1", password.encode(), b"saltysalt", 1, dklen=16)


def _decrypt_v11(encrypted: bytes, aes_key: bytes) -> str:
    """Decrypt a Chrome v11-encrypted cookie value."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    if encrypted[:3] != b"v11":
        return encrypted.decode("utf-8", errors="replace")

    iv = b" " * 16
    ct = encrypted[3:]
    dec = Cipher(algorithms.AES(aes_key), modes.CBC(iv)).decryptor()
    raw = dec.update(ct) + dec.finalize()

    # PKCS7 unpad
    pad = raw[-1]
    if 1 <= pad <= 16 and all(b == pad for b in raw[-pad:]):
        raw = raw[:-pad]

    text = raw.decode("latin1")
    # First AES block (16 bytes) is IV-XOR garbage in CBC mode.
    # Find the cookie value: longest run of printable ASCII after the garbage.
    # Covers: alphanumeric, base64 (+/=), percent-encoding (%), dots, dashes, underscores
    m = re.search(r"[a-zA-Z0-9%._+/=\-]{4,}", text)
    return m.group(0) if m else text


def read_chrome_cookies(names: list[str]) -> dict[str, str]:
    """Read and decrypt specific cookies for claude.ai from Chrome's SQLite DB."""
    if not CHROME_COOKIES_DB.exists():
        raise FileNotFoundError(f"Chrome cookies DB not found: {CHROME_COOKIES_DB}")

    aes_key = _get_chrome_aes_key()

    # Copy DB to avoid locking issues with running Chrome
    fd, tmp_db = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.chmod(tmp_db, 0o600)
    shutil.copy2(CHROME_COOKIES_DB, tmp_db)
    try:
        conn = sqlite3.connect(tmp_db)
        c = conn.cursor()
        placeholders = ",".join("?" * len(names))
        c.execute(
            f"SELECT name, encrypted_value FROM cookies "
            f"WHERE host_key LIKE '%claude.ai%' AND name IN ({placeholders})",
            names,
        )
        cookies = {}
        for name, enc in c.fetchall():
            cookies[name] = _decrypt_v11(enc, aes_key)
        conn.close()
    finally:
        os.unlink(tmp_db)

    return cookies


def fetch_api(path: str, cookie_str: str) -> tuple[int, dict | None]:
    """GET a claude.ai API path. Returns (status_code, parsed_json_or_None)."""
    req = Request(
        f"{BASE_URL}{path}",
        headers={"Cookie": cookie_str, "User-Agent": USER_AGENT},
    )
    try:
        resp = urlopen(req, timeout=15)
        return resp.status, json.loads(resp.read())
    except HTTPError as e:
        return e.code, None
    except (URLError, json.JSONDecodeError, OSError) as e:
        # Log without exposing cookie_str in stack trace
        print(f"[poller] fetch error on {path}: {type(e).__name__}", file=sys.stderr)
        return 0, None


def validate_usage(usage: dict) -> str | None:
    """Validate usage response schema. Returns error string or None if OK."""
    fh = usage.get("five_hour")
    if not isinstance(fh, dict):
        return "missing five_hour"
    util = fh.get("utilization")
    if not isinstance(util, (int, float)) or util < 0 or util > 100:
        return f"five_hour.utilization invalid: {util}"
    if "resets_at" not in fh:
        return "missing five_hour.resets_at"
    return None


def write_snapshot(data: dict) -> None:
    """Atomic write to /tmp/claude-ai-usage.json via tmp+rename."""
    data["polled_at"] = datetime.now(timezone.utc).isoformat()
    data["breaker"] = "ok"
    fd, tmp_path = tempfile.mkstemp(suffix=".json", dir="/tmp")
    try:
        os.write(fd, json.dumps(data, separators=(",", ":")).encode())
        os.close(fd)
        os.rename(tmp_path, str(USAGE_JSON))
    except OSError:
        os.close(fd)
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def write_stream(data: dict) -> None:
    """Append usage line to JSONL stream for SSE gauges."""
    usage = data.get("usage", {})
    fh = usage.get("five_hour", {})
    sd = usage.get("seven_day", {})
    spend = data.get("spend") or {}
    credits_data = data.get("credits") or {}

    line = json.dumps({
        "v": 2,
        "ts": time.time(),
        "source": "claude-ai-usage",
        "five_hour_pct": fh.get("utilization", 0),
        "five_hour_resets_at": fh.get("resets_at", ""),
        "seven_day_pct": sd.get("utilization", 0),
        "seven_day_resets_at": sd.get("resets_at", ""),
        "overage_used_cents": spend.get("used_credits", 0),
        "overage_limit_cents": spend.get("monthly_credit_limit", 0),
        "overage_currency": spend.get("currency", "USD"),
        "prepaid_cents": credits_data.get("amount", 0),
    }, separators=(",", ":")) + "\n"

    try:
        fd = os.open(str(STREAM_JSONL), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line.encode())
        finally:
            os.close(fd)
    except OSError:
        pass


def write_breaker(reason: str) -> None:
    """Write circuit breaker tripped state to the JSON file."""
    data = {
        "breaker": "tripped",
        "reason": reason,
        "tripped_at": datetime.now(timezone.utc).isoformat(),
        "usage": None, "spend": None, "credits": None,
    }
    fd, tmp_path = tempfile.mkstemp(suffix=".json", dir="/tmp")
    try:
        os.write(fd, json.dumps(data, separators=(",", ":")).encode())
        os.close(fd)
        os.rename(tmp_path, str(USAGE_JSON))
    except OSError:
        os.close(fd)
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _parse_iso(s: str) -> datetime | None:
    """Parse an ISO 8601 timestamp string to datetime, or None."""
    try:
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def _make_boundary_line(event: str, boundary_id: str, **kwargs) -> str:
    """Build a JSONL line for a session-boundary event."""
    return json.dumps({
        "v": 2,
        "ts": time.time(),
        "source": "session-boundary",
        "event": event,
        "boundary_id": boundary_id,
        **kwargs,
    }, separators=(",", ":")) + "\n"


def _append_to_file(path: Path, line: str) -> None:
    """Atomic append a line to a file (create if absent, 0o600)."""
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line.encode())
        finally:
            os.close(fd)
    except OSError:
        pass


def rotate_stream(old_resets_at: str, new_resets_at: str,
                  old_utilization: float, new_utilization: float) -> None:
    """Rotate the stream JSONL on 5h window boundary.

    Sequence (invariant: canonical path always exists):
      a. Create .new file
      b. Write boundary event as first line of .new
      c. Write boundary event as last line of old file
      d. Rename .new → canonical path (canonical always present)
      e. Rename old → .bak
      f. Cleanup old .bak files (keep max 3)
    """
    boundary_id = str(int(time.time() * 1000))
    boundary_line = _make_boundary_line(
        "window_reset",
        boundary_id=boundary_id,
        old_resets_at=old_resets_at,
        new_resets_at=new_resets_at,
        old_utilization=old_utilization,
        new_utilization=new_utilization,
    )

    # a. Create new file
    fd = os.open(str(STREAM_JSONL_NEW), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        # b. Write boundary as first line of new
        os.write(fd, boundary_line.encode())
    finally:
        os.close(fd)

    # c. Write boundary as last line of old (for connected SSE clients)
    if STREAM_JSONL.exists():
        _append_to_file(STREAM_JSONL, boundary_line)

        # d. Hard-link old → .bak BEFORE overwriting canonical path
        #    This preserves the old content under a .bak name while canonical still exists.
        date_tag = old_resets_at[:10] if old_resets_at else "unknown"
        bak_path = Path(f"/tmp/token-metrics-stream-{date_tag}.jsonl.bak")
        try:
            if bak_path.exists():
                bak_path.unlink()  # remove stale .bak with same date
            os.link(str(STREAM_JSONL), str(bak_path))
        except OSError:
            pass  # non-critical — .bak is for diagnostics only

    # e. Rename new → canonical (atomic — canonical always present)
    os.rename(str(STREAM_JSONL_NEW), str(STREAM_JSONL))

    # f. Cleanup old .bak files (keep max 3)
    _cleanup_bak_files()

    print(f"[poller] BOUNDARY: rotated stream (old={old_resets_at}, new={new_resets_at}, boundary_id={boundary_id})")


def _cleanup_bak_files() -> None:
    """Keep only the MAX_BAK_FILES most recent .bak files in /tmp/."""
    bak_files = sorted(Path("/tmp").glob("token-metrics-stream-*.jsonl.bak"))
    while len(bak_files) > MAX_BAK_FILES:
        try:
            bak_files.pop(0).unlink()
        except OSError:
            pass


def check_boundary(data: dict, last_resets_at: str | None,
                   last_utilization: float) -> tuple[str | None, bool]:
    """Check if a 5h window boundary occurred.

    Returns (new_resets_at_or_None, is_boundary).
    """
    usage = data.get("usage", {})
    fh = usage.get("five_hour", {})
    new_resets_at = fh.get("resets_at", "")
    new_utilization = fh.get("utilization", 0)

    if not new_resets_at or not last_resets_at:
        return new_resets_at, False

    if new_resets_at == last_resets_at:
        return new_resets_at, False

    # Guard: delta must be >= 4h [RT-F6]
    old_dt = _parse_iso(last_resets_at)
    new_dt = _parse_iso(new_resets_at)
    if not old_dt or not new_dt:
        return new_resets_at, False

    delta_h = abs((new_dt - old_dt).total_seconds()) / 3600
    if delta_h < BOUNDARY_DELTA_MIN_H:
        print(f"[poller] resets_at shift ignored (delta={delta_h:.1f}h < {BOUNDARY_DELTA_MIN_H}h)", file=sys.stderr)
        return new_resets_at, False

    # Guard: new resets_at must be in the future (> now - 1h) [RT-v2-F5]
    now = datetime.now(timezone.utc)
    if new_dt.tzinfo is None:
        new_dt = new_dt.replace(tzinfo=timezone.utc)
    margin = now - timedelta(hours=BOUNDARY_FUTURE_MARGIN_H)
    if new_dt < margin:
        print(f"[poller] resets_at in the past ignored ({new_resets_at} < {margin.isoformat()})", file=sys.stderr)
        return new_resets_at, False

    # Gap detection [RT-F7] [RT-v2-F8]
    if delta_h > GAP_THRESHOLD_H:
        missed = int(delta_h / 5) - 1
        print(f"[poller] GAP: {missed} windows missed (delta={delta_h:.0f}h)", file=sys.stderr)

    # Trigger rotation
    rotate_stream(last_resets_at, new_resets_at, last_utilization, new_utilization)
    return new_resets_at, True


def check_rate_limited(data: dict, already_signaled: bool) -> bool:
    """Check if rate limited, emit event if newly limited. Returns new signaled state."""
    usage = data.get("usage", {})
    fh = usage.get("five_hour", {})
    utilization = fh.get("utilization", 0)
    resets_at = fh.get("resets_at", "")

    # Log high utilization for empirical calibration [RT-F9]
    if utilization > 90:
        print(f"[poller] HIGH UTIL: {utilization}% (resets_at={resets_at})")

    if utilization >= 100 and not already_signaled:
        boundary_id = str(int(time.time() * 1000))
        line = _make_boundary_line(
            "rate_limited",
            boundary_id=boundary_id,
            utilization=utilization,
            resets_at=resets_at,
        )
        _append_to_file(STREAM_JSONL, line)
        print(f"[poller] RATE LIMITED: {utilization}% (resets_at={resets_at})")
        return True

    return already_signaled


def _load_last_resets_at() -> str | None:
    """Load last_resets_at from the JSON snapshot (for restart recovery) [RT-F10].

    Validates that the stored value is in the past (rejects corrupted future values).
    """
    if not USAGE_JSON.exists():
        return None
    try:
        data = json.loads(USAGE_JSON.read_text())
        stored = data.get("last_resets_at")
        if not stored:
            return None
        # Validate stored value is in the past [RT-F10]
        dt = _parse_iso(stored)
        if dt:
            now = datetime.now(timezone.utc)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt > now + timedelta(hours=6):  # allow small future margin for timezone issues
                print(f"[poller] Stored last_resets_at is too far in the future, ignoring: {stored}", file=sys.stderr)
                return None
        return stored
    except (json.JSONDecodeError, OSError):
        return None


def poll_once(initial_org_id: str | None) -> tuple[dict | None, str | None]:
    """Execute one poll cycle. Returns (data_dict, breaker_reason_or_None)."""
    # Read cookies fresh each time (watches for changes)
    try:
        cookies = read_chrome_cookies(["sessionKey", "lastActiveOrg"])
    except Exception as e:
        print(f"[poller] Cookie read error: {e}", file=sys.stderr)
        return None, None  # transient

    if "sessionKey" not in cookies:
        return None, "no_session_cookie"

    # Watch org_id stability
    current_org = cookies.get("lastActiveOrg", "")
    if initial_org_id and current_org and current_org != initial_org_id:
        return None, f"org_changed:{initial_org_id}->{current_org}"

    org_id = current_org or initial_org_id
    if not org_id:
        return None, "no_org_id"

    cookie_str = f"sessionKey={cookies['sessionKey']}"
    base = f"/api/organizations/{org_id}"

    # Fetch all three endpoints
    usage_status, usage = fetch_api(f"{base}/usage", cookie_str)
    spend_status, spend = fetch_api(f"{base}/overage_spend_limit", cookie_str)
    credits_status, credits_data = fetch_api(f"{base}/prepaid/credits", cookie_str)

    # Circuit breaker on auth/rate errors
    for label, status in [("usage", usage_status), ("spend", spend_status), ("credits", credits_status)]:
        if status in (401, 403):
            return None, f"http_{status}_{label}"
        if status == 429:
            return None, f"rate_limited_{label}"

    if usage is None:
        return None, None  # transient network error

    # Schema validation
    schema_err = validate_usage(usage)
    if schema_err:
        return None, f"schema_invalid:{schema_err}"

    return {
        "org_id": org_id,
        "usage": usage,
        "spend": spend,
        "credits": credits_data,
    }, None


def main():
    parser = argparse.ArgumentParser(description="Claude.ai usage poller via Chrome cookie bridge")
    parser.add_argument("--interval", type=int, default=120, help="Poll interval seconds (default: 120)")
    parser.add_argument("--high-threshold", type=int, default=80, help="Utilization %% for fast polling (default: 80)")
    parser.add_argument("--fast-interval", type=int, default=30, help="Fast poll interval seconds (default: 30)")
    parser.add_argument("--once", action="store_true", help="Poll once and exit")
    args = parser.parse_args()

    print(f"[poller] Starting claude.ai usage poller (interval={args.interval}s, high={args.high_threshold}%→{args.fast_interval}s)")

    initial_org_id = None
    consecutive_errors = 0
    last_resets_at = _load_last_resets_at()
    last_utilization = 0.0
    rate_limited_signaled = False

    if last_resets_at:
        print(f"[poller] Recovered last_resets_at from snapshot: {last_resets_at}")

    while True:
        data, breaker_reason = poll_once(initial_org_id)

        if breaker_reason:
            print(f"[poller] CIRCUIT BREAKER: {breaker_reason}", file=sys.stderr)
            write_breaker(breaker_reason)
            sys.exit(2)

        if data is None:
            consecutive_errors += 1
            print(f"[poller] Transient error ({consecutive_errors}/2)", file=sys.stderr)
            if consecutive_errors >= 2:
                print("[poller] CIRCUIT BREAKER: 2 consecutive errors", file=sys.stderr)
                write_breaker("consecutive_errors")
                sys.exit(2)
        else:
            consecutive_errors = 0
            if initial_org_id is None:
                initial_org_id = data.get("org_id")
                print(f"[poller] Locked org_id: {initial_org_id}")

            fh_pct = data.get("usage", {}).get("five_hour", {}).get("utilization", 0)
            sd_pct = data.get("usage", {}).get("seven_day", {}).get("utilization", 0)

            # Check for 5h window boundary
            new_resets_at, is_boundary = check_boundary(data, last_resets_at, last_utilization)
            if is_boundary:
                rate_limited_signaled = False  # reset rate limit flag on new window
            if new_resets_at:
                last_resets_at = new_resets_at
            last_utilization = fh_pct

            # Check for rate limit
            rate_limited_signaled = check_rate_limited(data, rate_limited_signaled)

            # Store last_resets_at in snapshot for restart recovery [RT-F10]
            data["last_resets_at"] = last_resets_at
            write_snapshot(data)
            write_stream(data)

            print(f"[poller] OK five_hour={fh_pct}% seven_day={sd_pct}%")

        if args.once:
            break

        # Adaptive interval
        current_pct = 0
        if data:
            current_pct = data.get("usage", {}).get("five_hour", {}).get("utilization", 0)
        interval = args.fast_interval if current_pct > args.high_threshold else args.interval
        time.sleep(interval)


if __name__ == "__main__":
    main()
