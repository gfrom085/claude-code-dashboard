#!/usr/bin/env python3
"""
Cache Invalidation Audit System for Claude Code sessions.

Parses transcript JSONL files to detect, classify, and report cache invalidation
events. Anthropic's cache is prefix-based: any change in the prefix forces a
rewrite of the suffix. Extended (1h) rewrites are expensive.

Usage:
    python3 scripts/cache_audit.py                          # all active sessions
    python3 scripts/cache_audit.py --session <id>           # one session
    python3 scripts/cache_audit.py --project <name>         # one project
    python3 scripts/cache_audit.py --human                  # human-readable output

Output: ~/.claude/state/cache_audit_results.jsonl
"""

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Pricing tables (USD per MTok)
# ---------------------------------------------------------------------------
PRICES: dict[str, dict[str, float]] = {
    "claude-sonnet-4-6": {
        "input": 3.00,
        "cache_read": 0.30,
        "cache_write_5m": 3.75,
        "cache_write_1h": 6.00,
    },
    "claude-sonnet-4-5-20250514": {
        "input": 3.00,
        "cache_read": 0.30,
        "cache_write_5m": 3.75,
        "cache_write_1h": 6.00,
    },
    "claude-opus-4-6": {
        "input": 15.00,
        "cache_read": 1.50,
        "cache_write_5m": 18.75,
        "cache_write_1h": 30.00,
    },
}
DEFAULT_MODEL = "claude-sonnet-4-6"

DROP_THRESHOLD = 0.50  # cache_read drop > 50% triggers detection

PROJECTS_DIR = Path.home() / ".claude" / "projects"
OUTPUT_FILE = Path.home() / ".claude" / "state" / "cache_audit_results.jsonl"

# TTL boundary (seconds) — above this delta, classify as TTL_EXPIRED
TTL_BOUNDARY = 3600  # 1 hour


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------
@dataclass
class TurnUsage:
    """Represents a single turn's cache usage extracted from the transcript.

    A turn may contain multiple API calls (message groups). The first group
    reflects the cache state at the START of the turn (cold, before tool use
    rebuilds the cache). The last group reflects the peak state.

    For drop detection: compare first_cache_read across consecutive turns.
    For metrics: use (last) cache_read / cache_creation.
    """
    turn_number: int
    line_start: int  # first JSONL line of the turn's assistant response
    line_end: int     # last JSONL line of the turn's assistant response
    timestamp: str    # ISO 8601 from the transcript
    timestamp_epoch: float
    # First message group usage (cache state at turn start — for drop detection)
    first_cache_read: int
    first_cache_creation: int
    # Last message group usage (peak state — for metrics and cost)
    cache_read: int
    cache_creation: int
    cache_5m: int
    cache_1h: int
    input_tokens: int
    output_tokens: int
    model: str


@dataclass
class InterveningMessage:
    """A message that appeared between two turns."""
    msg_type: str        # e.g. "user", "system:compact_boundary"
    is_meta: bool
    line: int
    preview: str         # first 120 chars of content


@dataclass
class CacheEvent:
    """A detected cache invalidation event."""
    session_id: str
    project: str
    model: str
    turn_before: int
    turn_after: int
    classification: str
    cache_read_before: int
    cache_read_after: int
    cache_creation_after: int
    drop_pct: float
    rewrite_cost_usd: float
    delta_seconds: float
    intervening_messages: list[dict[str, Any]]
    transcript_file: str
    line_range: list[int]
    probable_cause: str


@dataclass
class SessionMetrics:
    """Aggregate metrics for a session."""
    session_id: str
    project: str
    model: str
    total_turns: int
    total_invalidations: int
    invalidation_rate: float
    tokens_wasted: int
    wasted_cost_usd: float
    counterfactual_savings_usd: float
    efficiency_pct: float


# ---------------------------------------------------------------------------
# Transcript parsing
# ---------------------------------------------------------------------------
def parse_transcript(filepath: Path) -> tuple[str, str, list[dict]]:
    """Parse a transcript JSONL into structured lines.

    Returns (session_id, project_dir_name, lines_as_dicts).
    """
    raw_lines = filepath.read_text().strip().split("\n")
    parsed = []
    session_id = filepath.stem
    project_dir = filepath.parent.name

    for line in raw_lines:
        try:
            obj = json.loads(line)
            parsed.append(obj)
        except json.JSONDecodeError:
            parsed.append({})  # preserve line numbering

    # Extract session_id from first line
    if parsed and parsed[0].get("sessionId"):
        session_id = parsed[0]["sessionId"]

    return session_id, project_dir, parsed


def extract_project_name(dir_name: str) -> str:
    """Convert project dir name to human-readable name.

    Dir names look like: -home-pc-active-projects-cherie-point
    """
    parts = dir_name.split("-")
    # Skip leading empty + path components, take last meaningful segment(s)
    if len(parts) > 3:
        return "-".join(parts[3:])
    return dir_name


def is_tool_result(obj: dict) -> bool:
    """Check if a message is a tool_result."""
    content = obj.get("message", {}).get("content", [])
    if isinstance(content, list):
        return any(
            isinstance(item, dict) and item.get("type") == "tool_result"
            for item in content
        )
    return False


def extract_turns(lines: list[dict]) -> list[TurnUsage]:
    """Extract turns with usage data from parsed transcript lines.

    A turn = user message (non-tool-result, non-isMeta) followed by
    assistant message(s) with usage data. Multiple streaming fragments
    with the same message.id are grouped. Multiple message groups per
    turn are handled.

    Tracks BOTH first and last usage per turn:
    - first_cache_read/first_cache_creation: from the first API call (cold
      cache state at turn start — used for drop detection)
    - cache_read/cache_creation: from the last API call (peak state after
      tool use rebuilt the cache — used for metrics)
    """
    turns: list[TurnUsage] = []
    turn_number = 0
    in_user_turn = False
    has_assistant = False

    # Track current message group
    current_msg_id: str | None = None
    current_group_usage: dict | None = None

    # Track first and last usage per turn
    first_usage: dict | None = None      # first message group with output_tokens > 0
    last_usage: dict | None = None       # last message group with output_tokens > 0
    last_usage_line_end: int = -1
    last_timestamp: str = ""
    last_model: str = DEFAULT_MODEL
    turn_first_assistant_line: int = -1

    def finalize_group() -> None:
        """Save current message group's usage."""
        nonlocal first_usage, last_usage, last_usage_line_end
        if current_group_usage and current_group_usage.get("output_tokens", 0) > 0:
            if first_usage is None:
                first_usage = current_group_usage
            last_usage = current_group_usage

    def finalize_turn() -> None:
        """Create a TurnUsage from accumulated data."""
        nonlocal turn_number
        if not last_usage:
            return
        turn_number += 1
        cache_detail = last_usage.get("cache_creation", {})
        fu = first_usage or last_usage
        ts_str = last_timestamp or ""
        ts_epoch = 0.0
        if ts_str:
            try:
                ts_epoch = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
            except (ValueError, TypeError):
                pass

        turns.append(TurnUsage(
            turn_number=turn_number,
            line_start=turn_first_assistant_line,
            line_end=last_usage_line_end,
            timestamp=ts_str,
            timestamp_epoch=ts_epoch,
            first_cache_read=fu.get("cache_read_input_tokens", 0),
            first_cache_creation=fu.get("cache_creation_input_tokens", 0),
            cache_read=last_usage.get("cache_read_input_tokens", 0),
            cache_creation=last_usage.get("cache_creation_input_tokens", 0),
            cache_5m=cache_detail.get("ephemeral_5m_input_tokens", 0),
            cache_1h=cache_detail.get("ephemeral_1h_input_tokens", 0),
            input_tokens=last_usage.get("input_tokens", 0),
            output_tokens=last_usage.get("output_tokens", 0),
            model=last_model,
        ))

    for i, obj in enumerate(lines):
        msg_type = obj.get("type", "")
        is_meta = obj.get("isMeta", False)

        if msg_type == "user" and not is_meta:
            # Skip tool_result messages
            if is_tool_result(obj):
                continue

            # New real user message — finalize previous turn
            if current_msg_id:
                finalize_group()
                current_msg_id = None
                current_group_usage = None

            if in_user_turn and has_assistant and last_usage:
                finalize_turn()

            # Reset for new turn
            in_user_turn = True
            has_assistant = False
            first_usage = None
            last_usage = None
            last_usage_line_end = -1
            last_timestamp = ""
            last_model = DEFAULT_MODEL
            turn_first_assistant_line = -1
            current_msg_id = None
            current_group_usage = None

        elif msg_type == "assistant":
            has_assistant = True
            msg = obj.get("message", {})
            msg_id = msg.get("id")
            usage = msg.get("usage")
            ts = obj.get("timestamp", "")
            model = msg.get("model", DEFAULT_MODEL)

            if turn_first_assistant_line == -1:
                turn_first_assistant_line = i

            if msg_id and msg_id != current_msg_id:
                # New message group — finalize previous
                if current_msg_id:
                    finalize_group()
                current_msg_id = msg_id
                current_group_usage = usage if usage else None
                pass  # group start tracked via current_msg_id
            elif msg_id == current_msg_id:
                # Same group — update usage if present
                if usage:
                    current_group_usage = usage
            elif not msg_id:
                if usage:
                    current_group_usage = usage

            # Always update timestamp/model from latest fragment
            if ts:
                last_timestamp = ts
            if model and model != DEFAULT_MODEL:
                last_model = model

            # Track last line
            if last_usage_line_end < i:
                last_usage_line_end = i

    # Finalize last turn
    if current_msg_id:
        finalize_group()
    if in_user_turn and has_assistant and last_usage:
        finalize_turn()

    return turns


def extract_intervening_messages(
    lines: list[dict],
    line_start: int,
    line_end: int,
) -> list[InterveningMessage]:
    """Extract messages between two line ranges that could cause cache invalidation."""
    messages: list[InterveningMessage] = []

    for i in range(line_start, min(line_end + 1, len(lines))):
        obj = lines[i]
        msg_type = obj.get("type", "")
        subtype = obj.get("subtype", "")
        is_meta = obj.get("isMeta", False)

        # Skip assistant fragments and progress — they don't cause invalidation
        if msg_type in ("assistant", "progress", "file-history-snapshot", "custom-title"):
            continue

        # Skip system messages that don't affect cache
        if msg_type == "system" and subtype in ("turn_duration", "stop_hook_summary"):
            continue

        # Build preview
        preview = ""
        if msg_type == "user":
            content = obj.get("message", {}).get("content", "")
            if isinstance(content, str):
                preview = content[:120]
            elif isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        preview = item.get("text", "")[:120]
                        break
                    elif isinstance(item, str):
                        preview = item[:120]
                        break

        full_type = f"{msg_type}:{subtype}" if subtype else msg_type

        messages.append(InterveningMessage(
            msg_type=full_type,
            is_meta=is_meta,
            line=i,
            preview=preview,
        ))

    return messages


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------
def classify_event(
    prev: TurnUsage,
    curr: TurnUsage,
    intervening: list[InterveningMessage],
    is_first_turn: bool,
) -> tuple[str, str]:
    """Classify a cache drop event.

    Returns (classification, probable_cause).
    """
    if is_first_turn:
        return "SESSION_START", "First turn — cross-session cache baseline"

    delta_s = curr.timestamp_epoch - prev.timestamp_epoch
    # Use first_cache_read for drop detection: reflects cold cache state at turn start
    # (before tool-use API calls rebuild the cache within the turn)
    read_dropped = curr.first_cache_read < prev.cache_read * DROP_THRESHOLD
    write_up = curr.first_cache_creation > prev.first_cache_creation * 0.5  # significant rewrite
    write_down = curr.first_cache_creation < prev.first_cache_creation * 0.5

    # Check for compact boundary
    has_compact = any(m.msg_type == "system:compact_boundary" for m in intervening)
    if has_compact:
        return "CONTEXT_PRUNING", "/compact command"

    # Check for local commands (rename, etc.)
    local_cmds = [m for m in intervening if m.msg_type == "system:local_command"]
    meta_users = [m for m in intervening if m.msg_type == "user" and m.is_meta]

    # Check for user messages that insert content (isMeta caveat, skill loads, etc.)
    has_local_command = len(local_cmds) > 0
    has_meta_injection = len(meta_users) > 0

    if delta_s >= TTL_BOUNDARY:
        return "TTL_EXPIRED", f"TTL expiry ({delta_s / 3600:.1f}h gap)"

    if read_dropped and write_up and (has_local_command or has_meta_injection):
        # Identify which command
        cause_parts = []
        for m in local_cmds:
            cause_parts.append(f"local-command (line {m.line})")
        for m in meta_users:
            cause_parts.append(f"isMeta injection (line {m.line}: {m.preview[:60]})")
        cause = "; ".join(cause_parts) if cause_parts else "local command / meta injection"
        return "PREFIX_MUTATION", cause

    if read_dropped and write_down:
        return "CONTEXT_PRUNING", "Context reduced (read and write both down)"

    if read_dropped and write_up:
        # No intervening messages, within TTL
        if not intervening or all(m.msg_type in ("queue-operation",) for m in intervening):
            return "SERVER_EVICTION", f"No cause found, delta={delta_s:.0f}s < TTL"
        # Has some intervening messages we couldn't classify
        cause_types = [m.msg_type for m in intervening]
        return "PREFIX_MUTATION", f"Intervening: {', '.join(cause_types)}"

    return "UNKNOWN", "Unclassified drop pattern"


# ---------------------------------------------------------------------------
# Cost calculation
# ---------------------------------------------------------------------------
def get_prices(model: str) -> dict[str, float]:
    """Get pricing for a model, with fallback."""
    return PRICES.get(model, PRICES[DEFAULT_MODEL])


def compute_rewrite_cost(turn: TurnUsage) -> float:
    """Compute the cost of a cache rewrite in USD."""
    prices = get_prices(turn.model)
    cost_5m = turn.cache_5m * prices["cache_write_5m"] / 1_000_000
    cost_1h = turn.cache_1h * prices["cache_write_1h"] / 1_000_000
    return round(cost_5m + cost_1h, 6)


def compute_session_metrics(
    session_id: str,
    project: str,
    turns: list[TurnUsage],
    events: list[CacheEvent],
) -> SessionMetrics:
    """Compute aggregate session metrics."""
    if not turns:
        return SessionMetrics(
            session_id=session_id, project=project, model=DEFAULT_MODEL,
            total_turns=0, total_invalidations=0, invalidation_rate=0.0,
            tokens_wasted=0, wasted_cost_usd=0.0,
            counterfactual_savings_usd=0.0, efficiency_pct=100.0,
        )

    model = turns[0].model
    total_turns = len(turns)

    # Count real invalidations (exclude SESSION_START, CONTEXT_PRUNING, TTL_EXPIRED)
    actionable_events = [
        e for e in events
        if e.classification not in ("SESSION_START", "CONTEXT_PRUNING", "TTL_EXPIRED")
    ]
    total_invalidations = len(actionable_events)
    invalidation_rate = total_invalidations / max(total_turns - 1, 1)

    # Tokens wasted = sum of cache_creation on invalidation turns
    tokens_wasted = sum(e.cache_creation_after for e in actionable_events)

    # Cost of rewrites
    wasted_cost = sum(e.rewrite_cost_usd for e in actionable_events)

    # Counterfactual: if those tokens had been read instead of rewritten
    prices = get_prices(model)
    read_rate = prices["cache_read"] / 1_000_000
    write_rate = prices["cache_write_1h"] / 1_000_000  # conservative: assume 1h
    counterfactual_savings = tokens_wasted * (write_rate - read_rate)

    # Efficiency: total cache_read tokens / (total cache_read + total cache_creation)
    total_read = sum(t.cache_read for t in turns)
    total_creation = sum(t.cache_creation for t in turns)
    total_cache = total_read + total_creation
    efficiency = (total_read / total_cache * 100) if total_cache > 0 else 100.0

    return SessionMetrics(
        session_id=session_id,
        project=project,
        model=model,
        total_turns=total_turns,
        total_invalidations=total_invalidations,
        invalidation_rate=round(invalidation_rate, 3),
        tokens_wasted=tokens_wasted,
        wasted_cost_usd=round(wasted_cost, 6),
        counterfactual_savings_usd=round(counterfactual_savings, 6),
        efficiency_pct=round(efficiency, 1),
    )


# ---------------------------------------------------------------------------
# Main analysis
# ---------------------------------------------------------------------------
def analyze_session(
    filepath: Path,
) -> tuple[list[CacheEvent], SessionMetrics]:
    """Analyze a single session transcript for cache invalidation events."""
    session_id, project_dir, lines = parse_transcript(filepath)
    project = extract_project_name(project_dir)
    turns = extract_turns(lines)

    if not turns:
        metrics = compute_session_metrics(session_id, project, turns, [])
        return [], metrics

    events: list[CacheEvent] = []

    for i in range(len(turns)):
        curr = turns[i]

        if i == 0:
            # T1 is always SESSION_START — skip detection
            continue

        prev = turns[i - 1]

        # Check for cache drop using first_cache_read (cold state at turn start)
        if prev.cache_read == 0:
            continue  # can't compute drop from zero

        if curr.first_cache_read < prev.cache_read * DROP_THRESHOLD:
            # Detected a drop — gather intervening messages
            intervening = extract_intervening_messages(
                lines,
                prev.line_end + 1,
                curr.line_start - 1,
            )

            classification, probable_cause = classify_event(
                prev, curr, intervening, is_first_turn=False,
            )

            delta_s = curr.timestamp_epoch - prev.timestamp_epoch
            drop_pct = round(
                (1 - curr.first_cache_read / prev.cache_read) * 100, 1
            )
            # Cost based on first_cache_creation (the rewrite caused by invalidation)
            rewrite_cost = compute_rewrite_cost(curr)

            events.append(CacheEvent(
                session_id=session_id,
                project=project,
                model=curr.model,
                turn_before=prev.turn_number,
                turn_after=curr.turn_number,
                classification=classification,
                cache_read_before=prev.cache_read,
                cache_read_after=curr.first_cache_read,
                cache_creation_after=curr.first_cache_creation,
                drop_pct=drop_pct,
                rewrite_cost_usd=rewrite_cost,
                delta_seconds=round(delta_s, 1),
                intervening_messages=[
                    {
                        "type": m.msg_type,
                        "isMeta": m.is_meta,
                        "line": m.line,
                        "preview": m.preview,
                    }
                    for m in intervening
                ],
                transcript_file=str(filepath),
                line_range=[prev.line_end, curr.line_start],
                probable_cause=probable_cause,
            ))

    metrics = compute_session_metrics(session_id, project, turns, events)
    return events, metrics


# ---------------------------------------------------------------------------
# Session discovery
# ---------------------------------------------------------------------------
def find_transcripts(
    session_filter: str | None = None,
    project_filter: str | None = None,
) -> list[Path]:
    """Find transcript JSONL files matching filters."""
    if not PROJECTS_DIR.exists():
        return []

    results: list[tuple[float, Path]] = []

    for project_dir in PROJECTS_DIR.iterdir():
        if not project_dir.is_dir():
            continue

        project_name = extract_project_name(project_dir.name)

        if project_filter and project_filter.lower() not in project_name.lower():
            continue

        for tf in project_dir.glob("*.jsonl"):
            # Skip agent transcripts (subagent sessions)
            if tf.name.startswith("agent-"):
                continue

            if session_filter:
                # Check if session_id matches (file stem or first-line sessionId)
                if session_filter in tf.stem:
                    results.append((tf.stat().st_mtime, tf))
                else:
                    try:
                        first = json.loads(tf.read_text().split("\n")[0])
                        sid = first.get("sessionId", "")
                        if session_filter in sid:
                            results.append((tf.stat().st_mtime, tf))
                    except (json.JSONDecodeError, IOError, IndexError):
                        pass
            else:
                results.append((tf.stat().st_mtime, tf))

    # Sort by mtime descending (most recent first)
    results.sort(key=lambda x: x[0], reverse=True)
    return [path for _, path in results]


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------
def event_to_dict(event: CacheEvent) -> dict:
    """Convert a CacheEvent to a JSON-serializable dict."""
    return {
        "session_id": event.session_id,
        "project": event.project,
        "model": event.model,
        "turn_before": event.turn_before,
        "turn_after": event.turn_after,
        "classification": event.classification,
        "cache_read_before": event.cache_read_before,
        "cache_read_after": event.cache_read_after,
        "cache_creation_after": event.cache_creation_after,
        "drop_pct": event.drop_pct,
        "rewrite_cost_usd": event.rewrite_cost_usd,
        "delta_seconds": event.delta_seconds,
        "intervening_messages": event.intervening_messages,
        "transcript_file": event.transcript_file,
        "line_range": event.line_range,
        "probable_cause": event.probable_cause,
    }


def metrics_to_dict(m: SessionMetrics) -> dict:
    """Convert SessionMetrics to a JSON-serializable dict."""
    return {
        "session_id": m.session_id,
        "project": m.project,
        "model": m.model,
        "total_turns": m.total_turns,
        "total_invalidations": m.total_invalidations,
        "invalidation_rate": m.invalidation_rate,
        "tokens_wasted": m.tokens_wasted,
        "wasted_cost_usd": m.wasted_cost_usd,
        "counterfactual_savings_usd": m.counterfactual_savings_usd,
        "efficiency_pct": m.efficiency_pct,
    }


def format_human(events: list[CacheEvent], metrics: SessionMetrics) -> str:
    """Format events and metrics as human-readable text."""
    lines: list[str] = []

    lines.append(f"=== Session: {metrics.session_id[:12]}... ({metrics.project}) ===")
    lines.append(f"Model: {metrics.model}")
    lines.append(f"Turns: {metrics.total_turns} | Invalidations: {metrics.total_invalidations} | Rate: {metrics.invalidation_rate:.1%}")
    lines.append(f"Tokens wasted: {metrics.tokens_wasted:,} | Cost: ${metrics.wasted_cost_usd:.4f}")
    lines.append(f"Counterfactual savings: ${metrics.counterfactual_savings_usd:.4f}")
    lines.append(f"Cache efficiency: {metrics.efficiency_pct:.1f}%")
    lines.append("")

    if not events:
        lines.append("  No cache drops detected.")
    else:
        for e in events:
            symbol = {
                "PREFIX_MUTATION": "!!",
                "CONTEXT_PRUNING": "~~",
                "TTL_EXPIRED": "..",
                "SERVER_EVICTION": "??",
                "SESSION_START": "--",
                "UNKNOWN": "??",
            }.get(e.classification, "??")

            lines.append(f"  {symbol} T{e.turn_before}->T{e.turn_after} [{e.classification}]")
            lines.append(f"     cache_read: {e.cache_read_before:,} -> {e.cache_read_after:,} ({e.drop_pct:.0f}% drop)")
            lines.append(f"     cache_creation: {e.cache_creation_after:,} | cost: ${e.rewrite_cost_usd:.4f}")
            lines.append(f"     delta: {e.delta_seconds:.0f}s | cause: {e.probable_cause}")
            if e.intervening_messages:
                lines.append(f"     intervening ({len(e.intervening_messages)}):")
                for m in e.intervening_messages[:5]:
                    meta = " [isMeta]" if m.get("isMeta") else ""
                    lines.append(f"       L{m['line']}: {m['type']}{meta} {m.get('preview', '')[:60]}")
            lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="Cache invalidation audit for Claude Code sessions")
    parser.add_argument("--session", "-s", help="Session ID (partial match)")
    parser.add_argument("--project", "-p", help="Project name (partial match)")
    parser.add_argument("--human", "-H", action="store_true", help="Human-readable output")
    parser.add_argument("--no-write", action="store_true", help="Don't write to output file")
    parser.add_argument("--limit", type=int, default=20, help="Max sessions to analyze (default 20)")
    args = parser.parse_args()

    transcripts = find_transcripts(
        session_filter=args.session,
        project_filter=args.project,
    )

    if not transcripts:
        print("No transcripts found matching filters.", file=sys.stderr)
        sys.exit(1)

    # Limit to avoid processing hundreds of sessions
    transcripts = transcripts[:args.limit]

    all_events: list[CacheEvent] = []
    all_metrics: list[SessionMetrics] = []

    for tf in transcripts:
        try:
            events, metrics = analyze_session(tf)
            all_events.extend(events)
            all_metrics.append(metrics)
        except Exception as e:
            print(f"Error processing {tf}: {e}", file=sys.stderr)
            continue

    # Output
    if args.human:
        for metrics in all_metrics:
            session_events = [e for e in all_events if e.session_id == metrics.session_id]
            print(format_human(session_events, metrics))
            print()
    else:
        # JSONL output to stdout
        for event in all_events:
            print(json.dumps(event_to_dict(event)))
        # Metrics as final lines with _type marker
        for m in all_metrics:
            d = metrics_to_dict(m)
            d["_type"] = "session_metrics"
            print(json.dumps(d))

    # Write to output file
    if not args.no_write and all_events:
        OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(OUTPUT_FILE, "w") as f:
            for event in all_events:
                f.write(json.dumps(event_to_dict(event)) + "\n")
            for m in all_metrics:
                d = metrics_to_dict(m)
                d["_type"] = "session_metrics"
                f.write(json.dumps(d) + "\n")
        if args.human:
            print(f"\nResults written to: {OUTPUT_FILE}")

    # Summary to stderr
    total_events = len(all_events)
    actionable = len([e for e in all_events if e.classification not in ("SESSION_START", "CONTEXT_PRUNING", "TTL_EXPIRED")])
    total_wasted = sum(m.tokens_wasted for m in all_metrics)
    total_cost = sum(m.wasted_cost_usd for m in all_metrics)
    print(
        f"\nAudited {len(all_metrics)} session(s): "
        f"{total_events} events ({actionable} actionable), "
        f"{total_wasted:,} tokens wasted, ${total_cost:.4f} USD",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
