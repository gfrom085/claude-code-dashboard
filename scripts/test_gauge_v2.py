#!/usr/bin/env python3
"""
test_gauge_v2.py — Test the gauge with different token volumes and patterns.

Sends fake samples to /tmp/token-metrics-stream.jsonl (the SSE source).
The dashboard server tails this file and pushes samples to the gauge via SSE.

Usage:
    python3 scripts/test_gauge_v2.py [scenario]

Scenarios:
    idle        — No samples, gauge should stay at 0
    single      — One API call (60K cache_read), observe ramp+decay
    burst       — 10 calls in 2s, observe polyphonic stacking
    sustained   — Steady 1 call/s for 30s, observe sustained rate
    spike       — Sustained then sudden 10x burst, observe scale-up
    rampup      — Linear ramp 0→100 calls/s over 20s
    decay       — 10 calls then silence, observe decay half-life (should halve every 4s)
    mixed       — Realistic mix: varying cache_read + occasional big calls
    stress      — 100 calls/s for 10s (simulates 100 agents)
"""
import json
import os
import sys
import time

METRICS_FILE = "/tmp/token-metrics-stream.jsonl"


def write_sample(cache_read=60000, cache_creation=0, cache_5m=0, cache_1h=0,
                 input_tokens=3, output_tokens=500, thinking_chars=0):
    """Write one v2 sample to the stream file."""
    sample = {
        "v": 2,
        "ts": time.time(),
        "model": "claude-opus-4-6",
        "cache_read": cache_read,
        "cache_creation": cache_creation,
        "cache_5m": cache_5m,
        "cache_1h": cache_1h,
        "input": input_tokens,
        "output": output_tokens,
        "thinking_chars": thinking_chars,
        "service_tier": "standard",
    }
    line = (json.dumps(sample, separators=(",", ":")) + "\n").encode()
    fd = os.open(METRICS_FILE, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)


def scenario_idle():
    """No samples — gauge should stay at 0."""
    print("=== IDLE: No samples for 10s. Gauge should be at 0. ===")
    time.sleep(10)
    print("Done. Gauge should show 0.")


def scenario_single():
    """One call with 60K cache_read. Observe ramp-up (1.5s) then decay (half-life 4s)."""
    print("=== SINGLE: 1 call × 60K cache_read ===")
    print("Expected: ramp to ~0.06 M tok/min in ~1.5s, halve every 4s, gone in ~20s")
    write_sample(cache_read=60000)
    print("Sample sent. Watch the gauge...")
    time.sleep(25)
    print("Done. Gauge should be back to 0.")


def scenario_burst():
    """10 calls in 2s. Polyphonic stacking."""
    print("=== BURST: 10 calls × 60K in 2s ===")
    print("Expected: ramp to ~0.6 M tok/min (10 × 0.06), then decay")
    for i in range(10):
        write_sample(cache_read=60000)
        time.sleep(0.2)
    print("10 samples sent. Peak should be ~0.6 M tok/min.")
    time.sleep(25)
    print("Done.")


def scenario_sustained():
    """Steady 1 call/s for 30s. Sustained rate."""
    print("=== SUSTAINED: 1 call/s × 30s (60K each) ===")
    print("Expected: ramp up to steady ~0.06 M tok/min, stable plateau")
    for i in range(30):
        write_sample(cache_read=60000)
        sys.stdout.write(f"\r  {i+1}/30 calls sent")
        sys.stdout.flush()
        time.sleep(1.0)
    print("\nStopped. Watch decay over next 20s...")
    time.sleep(20)
    print("Done.")


def scenario_spike():
    """Sustained then 10x burst — test auto-scaling."""
    print("=== SPIKE: 10s sustained then 10x burst ===")
    print("Phase 1: 1 call/s for 10s (baseline)")
    for i in range(10):
        write_sample(cache_read=60000)
        time.sleep(1.0)
    print("Phase 2: 10 calls/s for 3s (SPIKE — watch GAUGE_MAX auto-scale)")
    for i in range(30):
        write_sample(cache_read=60000)
        time.sleep(0.1)
    print("Phase 3: silence — watch decay + scale-down after 6 ticks")
    time.sleep(30)
    print("Done.")


def scenario_rampup():
    """Linear ramp from 0 to 100 calls in 20s."""
    print("=== RAMPUP: 0→50 calls/s over 20s ===")
    print("Expected: smooth acceleration, auto-scale triggers at 70% of GAUGE_MAX")
    start = time.time()
    total = 0
    for second in range(20):
        calls_this_second = int((second + 1) * 2.5)  # 2.5 → 50 calls/s
        for _ in range(calls_this_second):
            write_sample(cache_read=60000)
            total += 1
        sys.stdout.write(f"\r  t={second+1}s: {calls_this_second} calls/s ({total} total)")
        sys.stdout.flush()
        elapsed = time.time() - start
        target = second + 1
        if target > elapsed:
            time.sleep(target - elapsed)
    print(f"\nDone. {total} calls sent. Watch decay...")
    time.sleep(25)
    print("Done.")


def scenario_decay():
    """10 calls then silence — measure half-life visually."""
    print("=== DECAY: 10 calls then silence ===")
    print("Expected peak: ~0.6 M tok/min")
    print("Watch: should halve every 4s → 0.3 at +4s, 0.15 at +8s, 0.075 at +12s")
    for _ in range(10):
        write_sample(cache_read=60000)
    print("10 samples sent at t=0. Timing half-life:")
    for t in [0, 2, 4, 6, 8, 12, 16, 20]:
        if t > 0:
            time.sleep(2 if t <= 2 else (t - [0, 2, 4, 6, 8, 12, 16, 20][[0, 2, 4, 6, 8, 12, 16, 20].index(t) - 1]))
        print(f"  t+{t}s — check gauge value now")
    print("Done.")


def scenario_mixed():
    """Realistic mix: varying cache_read sizes + occasional big calls."""
    print("=== MIXED: Realistic pattern for 30s ===")
    import random
    random.seed(42)
    for i in range(30):
        # 1-3 small calls per second
        n_calls = random.randint(1, 3)
        for _ in range(n_calls):
            cr = random.randint(10000, 80000)
            write_sample(cache_read=cr, output_tokens=random.randint(100, 2000))
        # Occasional big call (10% chance)
        if random.random() < 0.1:
            write_sample(cache_read=500000, output_tokens=5000, thinking_chars=10000)
            sys.stdout.write("!")
        else:
            sys.stdout.write(".")
        sys.stdout.flush()
        time.sleep(1.0)
    print("\nDone. Watch decay...")
    time.sleep(20)


def scenario_stress():
    """100 calls/s for 10s — simulates 100 agents."""
    print("=== STRESS: 100 calls/s × 10s (1000 calls total) ===")
    print("Expected: very high rate, GAUGE_MAX should auto-scale aggressively")
    start = time.time()
    total = 0
    for second in range(10):
        for _ in range(100):
            write_sample(cache_read=60000)
            total += 1
        sys.stdout.write(f"\r  {total}/1000 calls")
        sys.stdout.flush()
        elapsed = time.time() - start
        target = second + 1
        if target > elapsed:
            time.sleep(target - elapsed)
    print(f"\n{total} calls sent. Watch decay + scale-down...")
    time.sleep(30)
    print("Done.")


def scenario_12m_slow():
    """12M tokens delivered in large chunks over 60s (1 call/s × 200K each)."""
    print("=== 12M SLOW: 60 calls × 200K cache_read (1 call/s) ===")
    print("Expected: steady ~0.2 M tok/min, smooth plateau")
    total = 0
    for i in range(60):
        write_sample(cache_read=200000)
        total += 200000
        sys.stdout.write(f"\r  {total/1e6:.1f}M / 12M tokens")
        sys.stdout.flush()
        time.sleep(1.0)
    print(f"\n12M tokens delivered over 60s. Watch decay...")
    time.sleep(20)


def scenario_12m_medium():
    """12M tokens delivered in 20s (10 calls/s × 60K each)."""
    print("=== 12M MEDIUM: 200 calls × 60K cache_read (10 calls/s) ===")
    print("Expected: high rate ~0.6 M tok/min, auto-scale triggers")
    total = 0
    start = time.time()
    for second in range(20):
        for _ in range(10):
            write_sample(cache_read=60000)
            total += 60000
        sys.stdout.write(f"\r  {total/1e6:.1f}M / 12M tokens")
        sys.stdout.flush()
        elapsed = time.time() - start
        target = second + 1
        if target > elapsed:
            time.sleep(target - elapsed)
    print(f"\n12M tokens in 20s. Watch decay + scale-down...")
    time.sleep(30)


def scenario_12m_fast():
    """12M tokens delivered in 5s (40 calls/s × 60K each)."""
    print("=== 12M FAST: 200 calls × 60K in 5s (40 calls/s) ===")
    print("Expected: very high spike, aggressive auto-scale")
    total = 0
    start = time.time()
    for second in range(5):
        for _ in range(40):
            write_sample(cache_read=60000)
            total += 60000
        sys.stdout.write(f"\r  {total/1e6:.1f}M / 12M tokens")
        sys.stdout.flush()
        elapsed = time.time() - start
        target = second + 1
        if target > elapsed:
            time.sleep(target - elapsed)
    print(f"\n12M tokens in 5s. Watch decay...")
    time.sleep(30)


def scenario_12m_burst():
    """12M tokens in one massive burst (all at once)."""
    print("=== 12M BURST: 12 calls × 1M cache_read (instant) ===")
    print("Expected: massive spike, GAUGE_MAX scales to ~24+")
    for _ in range(12):
        write_sample(cache_read=1000000)
    print("12M tokens dumped instantly. Watch ramp + decay...")
    time.sleep(30)


def scenario_12m_waves():
    """25M tokens in 6 waves of ~4.2M with 5s gaps."""
    print("=== 25M WAVES: 6 × 4.2M tokens with 5s gaps ===")
    print("Expected: 6 spikes with accumulation (5s ≈ 1.25 half-lives → 42% residual)")
    print("Peaks should converge around ~7.2 M tok/min")
    total = 0
    for wave in range(6):
        wave_tokens = 42 * 100000
        print(f"\n  Wave {wave+1}/6 — 42 calls × 100K ({wave_tokens/1e6:.1f}M)")
        for _ in range(42):
            write_sample(cache_read=100000)
            time.sleep(0.05)
        total += wave_tokens
        print(f"  Total: {total/1e6:.1f}M tokens")
        if wave < 5:
            print(f"  Pause 5s — watch half-life (should keep ~42% residual)")
            time.sleep(5)
    print(f"\nAll 6 waves done. {total/1e6:.1f}M total. Final decay...")
    time.sleep(25)


def scenario_12m_realistic():
    """12M tokens simulating real multi-agent work — varying sizes, irregular timing."""
    import random
    random.seed(123)
    print("=== 12M REALISTIC: Variable sizes, irregular timing, 45s ===")
    print("Simulates: 5 agents working at different speeds")
    total = 0
    start = time.time()
    while total < 12_000_000 and (time.time() - start) < 45:
        # Each "agent" fires at random intervals
        n_agents_active = random.randint(2, 5)
        for _ in range(n_agents_active):
            cr = random.choice([15000, 30000, 60000, 120000, 250000])
            write_sample(cache_read=cr, output_tokens=random.randint(200, 3000))
            total += cr
        sys.stdout.write(f"\r  {total/1e6:.1f}M / 12M ({n_agents_active} agents)")
        sys.stdout.flush()
        time.sleep(random.uniform(0.1, 0.8))
    print(f"\n{total/1e6:.1f}M tokens in {time.time()-start:.0f}s. Watch decay...")
    time.sleep(25)


SCENARIOS = {
    "idle": scenario_idle,
    "single": scenario_single,
    "burst": scenario_burst,
    "sustained": scenario_sustained,
    "spike": scenario_spike,
    "rampup": scenario_rampup,
    "decay": scenario_decay,
    "mixed": scenario_mixed,
    "stress": scenario_stress,
    "12m-slow": scenario_12m_slow,
    "12m-medium": scenario_12m_medium,
    "12m-fast": scenario_12m_fast,
    "12m-burst": scenario_12m_burst,
    "12m-waves": scenario_12m_waves,
    "12m-realistic": scenario_12m_realistic,
}

if __name__ == "__main__":
    scenario = sys.argv[1] if len(sys.argv) > 1 else "burst"
    if scenario == "all":
        for name, fn in SCENARIOS.items():
            fn()
            print()
    elif scenario in SCENARIOS:
        SCENARIOS[scenario]()
    else:
        print(f"Unknown scenario: {scenario}")
        print(f"Available: {', '.join(SCENARIOS.keys())}, all")
        sys.exit(1)
