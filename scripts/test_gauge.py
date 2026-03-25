#!/usr/bin/env python3
"""Smooth gauge test — high-frequency injection matching poll cadence.

Poll=5s, half-life=8s. Inject every 500ms so each poll sees gradual growth.
3 phases: ramp up (0→60), sustain (60), decay (idle).
"""

import fcntl
import json
import math
import time
from pathlib import Path

SIDECAR_FILE = Path("/tmp/langfuse-token-metrics.json")
SIDECAR_LOCK = SIDECAR_FILE.with_suffix(".lock")
SID = "test-gauge-phantom-0000"


def locked_write(turn_n, cumulative):
    lf = open(SIDECAR_LOCK, "w")
    try:
        fcntl.flock(lf, fcntl.LOCK_EX)
        sidecar = {}
        if SIDECAR_FILE.exists():
            try:
                sidecar = json.loads(SIDECAR_FILE.read_text())
            except (json.JSONDecodeError, IOError):
                pass

        now = time.time()
        if SID not in sidecar:
            sidecar[SID] = {
                "type": "main", "project": "gauge-test",
                "parent": None, "last_seen": now, "turns": [],
            }
        sidecar[SID]["last_seen"] = now
        sidecar[SID]["turns"].append({
            "n": turn_n, "ts": now,
            "input": 2000, "output": 1000,
            "cache_read": cumulative, "cache_creation": 50000,
            "cache_5m": 30000, "cache_1h": 20000,
            "cache_savings_usd": 0, "cache_surcharge_usd": 0,
            "fork_cache_reuse": None,
        })
        tmp = SIDECAR_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(sidecar))
        tmp.rename(SIDECAR_FILE)
    finally:
        fcntl.flock(lf, fcntl.LOCK_UN)
        lf.close()


def cleanup():
    lf = open(SIDECAR_LOCK, "w")
    try:
        fcntl.flock(lf, fcntl.LOCK_EX)
        if SIDECAR_FILE.exists():
            sidecar = json.loads(SIDECAR_FILE.read_text())
            sidecar.pop(SID, None)
            tmp = SIDECAR_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(sidecar))
            tmp.rename(SIDECAR_FILE)
    finally:
        fcntl.flock(lf, fcntl.LOCK_UN)
        lf.close()


def main():
    # Target: 60 M tok/min at peak
    # rawRate = delta_per_poll / dt_minutes / 1e6
    # At poll=5s: dt=5/60 min → need delta = 60 * (5/60) * 1e6 = 5M per 5s
    # Inject every 0.5s → 10 injects per poll → 500K per inject at peak

    TICK = 0.5  # inject every 500ms
    TICKS_PER_POLL = 10  # ~5s per poll

    # Phase 1: Ramp 0→60 over 15s (30 ticks) — sine curve for smooth accel
    # Phase 2: Sustain 60 for 10s (20 ticks)
    # Phase 3: Idle 20s — decay

    ramp_ticks = 30
    sustain_ticks = 20

    print("Smooth gauge test — watch the dial!\n")
    print("  Phase 1: ramp 0 → 60 M tok/min (15s)")
    print("  Phase 2: sustain 60 (10s)")
    print("  Phase 3: idle decay (20s)\n")

    cumulative = 0
    turn = 0

    try:
        # Phase 1: Ramp — sine ease-in (smooth acceleration)
        for i in range(ramp_ticks):
            t = i / ramp_ticks
            # sine ease-in: slow start, accelerating
            rate_frac = math.sin(t * math.pi / 2)  # 0 → 1
            tokens_per_tick = int(rate_frac * 500_000)
            cumulative += tokens_per_tick
            turn += 1
            locked_write(turn, cumulative)

            if i % TICKS_PER_POLL == 0:
                target = rate_frac * 60
                print(f"  {i*TICK:5.1f}s  cumul={cumulative/1e6:6.1f}M  target≈{target:.0f} M/min")

            time.sleep(TICK)

        # Phase 2: Sustain at peak
        print()
        for i in range(sustain_ticks):
            cumulative += 500_000  # full rate
            turn += 1
            locked_write(turn, cumulative)

            if i % TICKS_PER_POLL == 0:
                print(f"  {(ramp_ticks + i)*TICK:5.1f}s  cumul={cumulative/1e6:6.1f}M  sustain 60 M/min")

            time.sleep(TICK)

        # Phase 3: Idle
        print(f"\n  --- Idle 20s: decay half-life=8s ---")
        print(f"  Expected: 60 → 30 (8s) → 15 (16s) → ~7 (20s)\n")
        time.sleep(20)

    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        cleanup()
        print("Cleaned up.")


if __name__ == "__main__":
    main()
