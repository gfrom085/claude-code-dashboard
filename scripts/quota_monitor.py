#!/usr/bin/env python3
"""
quota_monitor.py — Detect Claude Max quota windows (429 rate limits).

Watches /tmp/token-metrics-skips for 429 events from mitmproxy.
Logs quota windows (start/end/duration) to data/quota-windows.jsonl.

Usage:
    python3 scripts/quota_monitor.py [--poll 5]

Output (data/quota-windows.jsonl):
    {"event":"start", "ts":1775010000, "iso":"2026-03-31T23:00:00"}
    {"event":"end",   "ts":1775010300, "iso":"2026-03-31T23:05:00", "duration_s":300, "duration_h":"0:05:00"}
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

SKIPS_FILE = Path("/tmp/token-metrics-skips")
DATA_DIR = Path.home() / "active-projects" / "claude-code-dashboard" / "data"
QUOTA_LOG = DATA_DIR / "quota-windows.jsonl"

# State
in_quota = False
quota_start_ts = 0
last_degraded_ts = 0
last_seen_skip_ts = 0  # track last processed skip timestamp to avoid re-triggers

# How long without a 429 before we consider the window ended (seconds)
RECOVERY_THRESHOLD = 120  # 2 min of no 429s = quota recovered


def log_event(event: str, **extra):
    """Append an event to quota-windows.jsonl."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    entry = {
        "event": event,
        "ts": time.time(),
        "iso": datetime.now().isoformat(timespec="seconds"),
        **extra,
    }
    line = (json.dumps(entry, separators=(",", ":")) + "\n").encode()
    fd = os.open(str(QUOTA_LOG), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)
    return entry


def read_skips() -> dict:
    """Read current skips state."""
    if not SKIPS_FILE.exists():
        return {}
    try:
        return json.loads(SKIPS_FILE.read_text())
    except (json.JSONDecodeError, IOError):
        return {}


def format_duration(seconds: float) -> str:
    """Format seconds as H:MM:SS."""
    td = timedelta(seconds=int(seconds))
    return str(td)


def monitor(poll_interval: int = 5):
    """Main monitoring loop."""
    global in_quota, quota_start_ts, last_degraded_ts, last_seen_skip_ts

    print(f"[quota_monitor] Watching {SKIPS_FILE}")
    print(f"[quota_monitor] Logging to {QUOTA_LOG}")
    print(f"[quota_monitor] Poll interval: {poll_interval}s, recovery threshold: {RECOVERY_THRESHOLD}s")
    print()

    # Seed last_seen_skip_ts with current value to avoid triggering on stale data
    initial = read_skips()
    last_seen_skip_ts = initial.get("ts", 0)
    if last_seen_skip_ts > 0:
        print(f"[quota_monitor] Existing skip ts={last_seen_skip_ts:.0f} — ignoring stale state")

    while True:
        try:
            skips = read_skips()
            degraded = skips.get("degraded", 0)
            skip_ts = skips.get("ts", 0)
            now = time.time()

            # Only react to NEW 429 events (skip_ts changed since last poll)
            if degraded > 0 and skip_ts > 0 and skip_ts > last_seen_skip_ts:
                last_seen_skip_ts = skip_ts
                last_degraded_ts = skip_ts

                if not in_quota:
                    # Quota window starts
                    in_quota = True
                    quota_start_ts = skip_ts
                    entry = log_event("start")
                    print(f"[{entry['iso']}] ⚠ QUOTA HIT — 429 detected, window started")

            if in_quota:
                # Check if recovered
                since_last_429 = now - last_degraded_ts
                if since_last_429 > RECOVERY_THRESHOLD:
                    # Quota window ended
                    duration = last_degraded_ts - quota_start_ts + RECOVERY_THRESHOLD
                    entry = log_event(
                        "end",
                        duration_s=round(duration),
                        duration_h=format_duration(duration),
                        start_ts=quota_start_ts,
                    )
                    in_quota = False
                    print(f"[{entry['iso']}] ✓ QUOTA RECOVERED — window: {format_duration(duration)}")
                else:
                    # Still in quota, show status
                    elapsed = now - quota_start_ts
                    sys.stdout.write(
                        f"\r  ⏳ In quota window: {format_duration(elapsed)} "
                        f"(last 429: {int(since_last_429)}s ago, "
                        f"recovery in {int(RECOVERY_THRESHOLD - since_last_429)}s)"
                    )
                    sys.stdout.flush()

            time.sleep(poll_interval)

        except KeyboardInterrupt:
            if in_quota:
                duration = time.time() - quota_start_ts
                log_event("interrupted", duration_s=round(duration), duration_h=format_duration(duration))
                print(f"\n[interrupted] Window was open for {format_duration(duration)}")
            print("\n[quota_monitor] Stopped.")
            break


def show_history():
    """Print quota window history."""
    if not QUOTA_LOG.exists():
        print("No quota history found.")
        return

    print(f"{'Event':<12} {'Timestamp':<22} {'Duration':<12}")
    print("─" * 50)
    for line in QUOTA_LOG.read_text().strip().split("\n"):
        if not line:
            continue
        try:
            e = json.loads(line)
            event = e.get("event", "?")
            iso = e.get("iso", "?")
            dur = e.get("duration_h", "")
            print(f"{event:<12} {iso:<22} {dur:<12}")
        except json.JSONDecodeError:
            continue


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Monitor Claude Max quota windows (429s)")
    parser.add_argument("--poll", type=int, default=5, help="Poll interval in seconds (default: 5)")
    parser.add_argument("--history", action="store_true", help="Show quota window history and exit")
    args = parser.parse_args()

    if args.history:
        show_history()
    else:
        monitor(args.poll)
