#!/usr/bin/env python3
"""migrate_sidecar.py — One-shot migration from JSON monolithic to JSONL append-only."""
import json
import os
import sys
from pathlib import Path

OLD = Path("/tmp/langfuse-token-metrics.json")
NEW = Path("/tmp/langfuse-token-metrics.jsonl")

if not OLD.exists():
    print("No old sidecar found, nothing to migrate.")
    sys.exit(0)

data = json.loads(OLD.read_text())
count = 0
fd = os.open(str(NEW), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
try:
    for sid, entry in data.items():
        if not isinstance(entry, dict) or not entry.get("turns"):
            continue
        for turn in entry["turns"]:
            line = json.dumps({
                "sid": sid,
                "type": entry.get("type", "main"),
                "project": entry.get("project", ""),
                "turn": turn.get("n", 0),
                "ts": turn.get("ts", 0),
                "input": turn.get("input", 0),
                "output": turn.get("output", 0),
                "cache_read": turn.get("cache_read", 0),
                "cache_creation": turn.get("cache_creation", 0),
                "cache_5m": turn.get("cache_5m", 0),
                "cache_1h": turn.get("cache_1h", 0),
            }, separators=(",", ":")) + "\n"
            os.write(fd, line.encode())
            count += 1
finally:
    os.close(fd)

# Rename old file (keep as backup)
OLD.rename(OLD.with_suffix(".json.bak"))
print(f"Migrated {count} turn entries from .json to .jsonl")
