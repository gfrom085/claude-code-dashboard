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
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

USAGE_JSON = Path("/tmp/claude-ai-usage.json")
STREAM_JSONL = Path("/tmp/token-metrics-stream.jsonl")
CHROME_COOKIES_DB = Path.home() / ".config/google-chrome/Default/Cookies"
BASE_URL = "https://claude.ai"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"


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
    cipher = Cipher(algorithms.AES(aes_key), modes.CBC(iv))
    raw = cipher.decryptor().update(ct) + cipher.decryptor().finalize()

    # PKCS7 unpad
    dec = Cipher(algorithms.AES(aes_key), modes.CBC(iv)).decryptor()
    raw = dec.update(ct) + dec.finalize()
    pad = raw[-1]
    if 1 <= pad <= 16 and all(b == pad for b in raw[-pad:]):
        raw = raw[:-pad]

    text = raw.decode("latin1")
    # First block is IV-XOR garbage. Find first run of printable ASCII (>= 8 chars).
    m = re.search(r"[a-zA-Z0-9%._\-]{8,}", text)
    return m.group(0) if m else text


def read_chrome_cookies(names: list[str]) -> dict[str, str]:
    """Read and decrypt specific cookies for claude.ai from Chrome's SQLite DB."""
    if not CHROME_COOKIES_DB.exists():
        raise FileNotFoundError(f"Chrome cookies DB not found: {CHROME_COOKIES_DB}")

    aes_key = _get_chrome_aes_key()

    # Copy DB to avoid locking issues with running Chrome
    tmp_db = tempfile.mktemp(suffix=".db")
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
    except (URLError, json.JSONDecodeError, OSError):
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
        fd = os.open(str(STREAM_JSONL), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
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

            write_snapshot(data)
            write_stream(data)

            fh_pct = data.get("usage", {}).get("five_hour", {}).get("utilization", 0)
            sd_pct = data.get("usage", {}).get("seven_day", {}).get("utilization", 0)
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
