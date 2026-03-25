# H3: Langfuse Hook Logic & Resilience Audit

**Auditor:** Claude Code | **Date:** 2026-03-25 | **Focus:** Hook lifecycle, sidecar writes, integration, token parsing, error handling, performance

---

## 1. Hook Lifecycle

### When Does It Fire?

The hook is a **Stop hook** that runs after each Claude Code assistant response. Activation is opt-in:

- **Trigger:** Explicitly set by Claude Code's hook orchestrator (stop phase)
- **Gating:** Only runs if `TRACE_TO_LANGFUSE=true` is set in project settings (line 1072)
- **Early exit:** If disabled, script exits gracefully with code 0

### Stdin Payload & Parsing

**Expected stdin (lines 1064-1069):**

```python
raw = sys.stdin.read()
if raw.strip():
    stdin_data = json.loads(raw)
    debug(f"stdin session_id: {stdin_data.get('session_id', 'none')}")
```

**What it expects:**
- Optional JSON object via stdin containing `session_id` (Claude Code session context)
- Parsing is lenient: empty stdin or malformed JSON is silently ignored (`pass` clause)
- The stdin data is loaded but **not directly used** in the main flow

**FINDING (Logic Gap):**
The stdin payload containing `session_id` is read but discarded. The actual session discovery uses `find_modified_transcripts()` (line 1089), which scans the filesystem for all transcripts modified since the last state update. This design choice allows the hook to:
- Process multiple concurrent sessions (subagents, team agents)
- Recover from missed sessions if the hook was interrupted
- But it ignores the stdin hint about which session just fired

**Risk:** If stdin provides a session_id but the transcript hasn't been written yet, or if multiple sessions fire concurrently, the hook may process stale sessions or miss the current one. Mitigated by the filesystem scan, but not optimal.

---

## 2. Sidecar File Writing (/tmp/langfuse-token-metrics.json)

### Structure

The sidecar is a JSON object keyed by `session_id`:

```json
{
  "session_uuid_or_composite": {
    "type": "main|subagent|fork|team_agent",
    "project": "project-name",
    "parent_session": "uuid|null",
    "turns": [
      {
        "n": 1,
        "ts": 1711353600.0,
        "input": 1024,
        "output": 512,
        "cache_read": 256,
        "cache_creation": 128,
        "cache_5m": 64,
        "cache_1h": 64,
        "cache_savings_usd": 0.000056,
        "cache_surcharge_usd": 0.000024,
        "fork_cache_reuse": 0.25
      }
    ],
    "last_seen": 1711353600.0
  }
}
```

**Schema (lines 442-511, update_sidecar()):**
- `type`: Session classification (main, subagent, fork, team_agent)
- `project`: Human-readable project name
- `parent_session`: For forks, the parent's session_id
- `turns`: Array of turn objects with token counts and cost deltas
- `last_seen`: File mtime of last transcript update (ground truth)

### File Locking

**Critical mechanism (lines 355-375):**

```python
def acquire_sidecar_lock():
    lf = open(SIDECAR_LOCK, "w")
    fcntl.flock(lf, fcntl.LOCK_EX)  # Exclusive lock
    return lf

def release_sidecar_lock(lf):
    fcntl.flock(lf, fcntl.LOCK_UN)
    lf.close()
```

**Locking pattern (lines 1160-1217):**
1. Acquire exclusive lock before sidecar operations
2. Load sidecar → drain queue → process transcripts → reconcile → write
3. Release lock in finally block

**Quality assessment:**
- ✅ Exclusive fcntl lock prevents TOCTOU races between concurrent hook invocations
- ✅ Lock held for entire read-modify-write cycle
- ✅ Properly released in finally block (guaranteed)
- ⚠️ Lock file (`SIDECAR_LOCK`) is implicitly created but never cleaned up — accumulates over time
- ⚠️ No timeout on lock acquisition — hook can block indefinitely if another hook hangs

### Concurrent Session Writes

**Scenario:** Two hooks fire within milliseconds (e.g., subagent + main session).

**What happens:**
1. Hook A acquires lock, holds it for full cycle (load → process → write)
2. Hook B blocks on `fcntl.flock()`
3. Hook A releases lock, hook B acquires it
4. Hook B loads the sidecar (including Hook A's writes), processes independently, writes

**JSON validity during write (line 438):**

```python
tmp = SIDECAR_FILE.with_suffix(".tmp")
tmp.write_text(json.dumps(data, separators=(",", ":")))  # Atomic write
tmp.replace(SIDECAR_FILE)  # Atomic rename on POSIX
```

**Safety:**
- ✅ Write is atomic: json.dumps → .tmp file → atomic rename
- ✅ If process dies during write, only .tmp is corrupted, sidecar.json remains valid
- ✅ Rename is atomic on POSIX filesystems
- ✅ JSON encoding is guaranteed to be valid (Python stdlib)

**No lost writes:** Lock prevents interleaving. But if Hook A and Hook B write to overlapping session_ids in the same cycle, only Hook B's session entry survives (last write wins). This is acceptable because `update_sidecar()` is idempotent (lines 501-508):

```python
for i, t in enumerate(turns):
    if t["n"] == turn_n:
        turns[i] = turn_entry  # Replace if exists
        break
else:
    turns.append(turn_entry)
```

---

## 3. Langfuse Integration

### Health Check Mechanism (Socket-Based)

**Implementation (lines 61-95):**

```python
def check_langfuse_health(host: str) -> bool:
    """Quick health check to see if Langfuse is reachable."""
    try:
        # Parse host URL (http://, https://, bare hostname)
        if host.startswith("http://"):
            host_part = host[7:]
            default_port = 80
        elif host.startswith("https://"):
            host_part = host[8:]
            default_port = 443
        else:
            host_part = host
            default_port = 443

        # Extract hostname and port
        if ":" in host_part:
            hostname, port_str = host_part.split(":", 1)
            port = int(port_str.rstrip("/"))
        else:
            hostname = host_part.rstrip("/")
            port = default_port

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(HEALTH_CHECK_TIMEOUT)  # 2s timeout
        result = sock.connect_ex((hostname, port))
        sock.close()

        is_healthy = result == 0
        return is_healthy
    except Exception as e:
        debug(f"Health check error: {e}")
        return False
```

**Reliability assessment:**
- ✅ Socket connection is fast (TCP SYN only, no full HTTP request)
- ✅ 2-second timeout prevents hook from hanging
- ✅ Graceful error handling: any exception → `False`
- ✅ URL parsing handles http://, https://, bare hostnames, custom ports
- ⚠️ Only tests TCP connectivity, not actual Langfuse API health (e.g., if port 443 is open but service is down, health check passes)
- ⚠️ DNS resolution is synchronous (could hang if DNS is slow), but no explicit timeout on that phase

**Decision:** If health check fails, traces are **queued locally** instead of dropped (line 1100-1147). This is correct behavior.

### Queue/Retry Logic

**Queuing mechanism (lines 98-104, 107-123):**

```python
def queue_trace(trace_data: dict) -> None:
    """Append a trace to the local queue file."""
    QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    trace_data["queued_at"] = datetime.now(timezone.utc).isoformat()
    with open(QUEUE_FILE, "a") as f:
        f.write(json.dumps(trace_data) + "\n")

def load_queued_traces() -> list[dict]:
    """Load all pending traces from the queue file."""
    if not QUEUE_FILE.exists():
        return []

    traces = []
    try:
        with open(QUEUE_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if line:
                    traces.append(json.loads(line))
    except (json.JSONDecodeError, IOError) as e:
        log("ERROR", f"Failed to load queue: {e}")
        return []

    return traces
```

**Queue file format:** JSONL (one trace per line). Each trace includes:
- Original trace_data (session_id, turn_num, user_msg, assistant_msgs, tool_results, project_name)
- `queued_at`: ISO timestamp when queued

**Drain logic (lines 133-165):**

```python
def drain_queue(langfuse: Langfuse) -> int:
    """Drain all queued traces to Langfuse. Returns count of drained traces."""
    traces = load_queued_traces()
    if not traces:
        return 0

    log("INFO", f"Draining {len(traces)} queued traces to Langfuse")

    drained = 0
    for trace_data in traces:
        try:
            create_trace(...)
            drained += 1
        except Exception as e:
            log("ERROR", f"Failed to drain trace: {e}")
            # If we fail mid-drain, rewrite remaining traces and exit
            remaining = traces[drained:]
            clear_queue()
            for remaining_trace in remaining:
                queue_trace(remaining_trace)
            return drained

    clear_queue()
    log("INFO", f"Successfully drained {drained} traces")
    return drained
```

**Reliability assessment:**
- ✅ On Langfuse unavailable, traces are appended to QUEUE_FILE (JSONL format, resilient to concurrent writes)
- ✅ On next successful health check, queue is drained (re-sent to Langfuse)
- ✅ If drain fails mid-queue, remaining traces are re-queued (fail-safe)
- ✅ Queued traces preserve all metadata (no data loss)
- ⚠️ Queue file grows indefinitely if Langfuse is down for days (no max queue size)
- ⚠️ No per-trace retry limit or TTL (stale traces older than X days never expire)
- ⚠️ If a trace systematically fails to drain (e.g., malformed data), it re-queues forever (no DLQ)

**Practical impact:** For typical Langfuse incidents (<1h), this is fine. For prolonged downtime (>24h), queue could grow to GBs.

### Trace Structure

**For Langfuse (lines 759-898, create_trace()):**

```python
with langfuse.start_as_current_span(
    name=f"Turn {turn_num}",
    input={"role": "user", "content": user_text},
    metadata={
        "source": "claude-code",
        "turn_number": turn_num,
        "project": project_name,
        "session_type": session_type,
    },
) as trace_span:
    # Update trace attributes (session_id, tags)
    langfuse.update_current_trace(
        session_id=session_id,
        tags=tags,  # ["claude-code", session_type, project_name]
        metadata={...},
    )

    # Create generation for the LLM response
    with langfuse.start_as_current_observation(
        name="Claude Response",
        as_type="generation",
        model=model,
        input={"role": "user", "content": user_text},
        output={"role": "assistant", "content": final_output},
        usage_details={...},
        cost_details={...},
    ):
        pass

    # Create spans for each tool call
    for tool_call in all_tool_calls:
        with langfuse.start_as_current_span(
            name=f"Tool: {tool_call['name']}",
            input=tool_call["input"],
            output=tool_call["output"],
        ):
            pass
```

**Structure is well-formed:**
- ✅ Hierarchical: trace → generation + tool spans
- ✅ Proper session grouping via session_id
- ✅ Tags for filtering (claude-code, session_type, project)
- ✅ Metadata includes turn number and project
- ✅ Token counts and cost deltas included

---

## 4. Token Counting & Cache Metadata Extraction

### Token Field Extraction (lines 459-467)

```python
cache_read = usage.get("cache_read_input_tokens", 0)
cache_creation = usage.get("cache_creation_input_tokens", 0)
cache_5m = usage.get("cache_creation", {}).get("ephemeral_5m_input_tokens", 0)
cache_1h = usage.get("cache_creation", {}).get("ephemeral_1h_input_tokens", 0)
```

### Where `usage` Comes From (lines 255-266, extract_usage_from_parts)

```python
def extract_usage_from_parts(parts: list) -> dict | None:
    """Extract token usage from the last assistant fragment with output_tokens > 0.

    Streaming produces multiple fragments per message_id. The last fragment
    with output_tokens > 0 contains the definitive token counts.
    """
    for part in reversed(parts):
        if isinstance(part, dict):
            usage = part.get("message", {}).get("usage", {})
            if isinstance(usage, dict) and usage.get("output_tokens", 0) > 0:
                return usage
    return None
```

**Key insight:** Claude Code produces streaming fragments. Each fragment from the same `message_id` is grouped, then the **last fragment with output_tokens > 0** is used for token counts.

### Expected Usage Structure

From Claude API response, usage is embedded in message:

```json
{
  "message": {
    "id": "msg-xyz",
    "usage": {
      "input_tokens": 1024,
      "output_tokens": 512,
      "cache_read_input_tokens": 256,
      "cache_creation_input_tokens": 128,
      "cache_creation": {
        "ephemeral_5m_input_tokens": 64,
        "ephemeral_1h_input_tokens": 64
      }
    }
  }
}
```

### Edge Cases & Robustness

**Missing fields:**
- ✅ All token fields default to 0 (`.get(..., 0)`)
- ✅ Missing `cache_creation` dict → `{}` (safe nested get)
- ✅ Non-dict usage → returns `{}` (handled by `isinstance(usage, dict)`)

**No usage found in parts:**
- ✅ Returns `None` (safe to check)
- ✅ Caller checks `if usage:` before using (lines 875, 989, 1036)

**Streaming edge case:** If all fragments have `output_tokens = 0`, usage is `None`. This shouldn't happen in practice (at least the final fragment should have output_tokens > 0), but if it does, the turn is still traced (just without usage details).

**Cache cost calculation edge cases (lines 464-467):**

```python
savings = cache_read * (CACHE_BASE_PRICE_PER_TOKEN - CACHE_READ_PRICE_PER_TOKEN)
surcharge_5m = cache_5m * (CACHE_CREATE_5M_PRICE_PER_TOKEN - CACHE_BASE_PRICE_PER_TOKEN)
surcharge_1h = cache_1h * (CACHE_CREATE_1H_PRICE_PER_TOKEN - CACHE_BASE_PRICE_PER_TOKEN)
surcharge = surcharge_5m + surcharge_1h
```

- ✅ Arithmetic is correct (savings are positive, surcharges are positive)
- ✅ Default pricing matches Claude API (Sonnet rates)
- ⚠️ Pricing is hardcoded, not fetched from an API (if pricing changes, hook must be updated)

---

## 5. Session Classification Logic

**Function: detect_session_type() (lines 269-338)**

Returns: `(session_type, parent_session_id | None)`

### Detection Priority

1. **Filename check:** If file starts with `agent-` → `("subagent", None)` (line 284)
2. **No session log (non-interactive):** If `/tmp/.claude-sessions/{session_id}.log` doesn't exist:
   - File age < 24h → `("subagent", None)` (Task tool session)
   - File age >= 24h → `("main", None)` (historical pre-hook session)
3. **Fork detection:** Check session log for `parent_session` field → `("fork", parent_id)`
4. **Team agent detection:** Scan for concurrent sibling JSONL files in same project directory with:
   - Same mtime (within 300s window, line 44: `TEAM_WINDOW_SECONDS = 300`)
   - Sibling has no session log (detected as subagent) → parent is `("team_agent", None)`
5. **Default:** `("main", None)`

### Robustness Issues

**Filename-based detection (line 284):**
- Relies on Claude Code naming convention (`agent-*.jsonl`)
- If convention changes, detection fails silently → wrongly classified as main
- No fallback validation

**Session log missing → subagent heuristic (line 292):**
- Assumes tasks spawn no-session-log subagents
- But if a human starts a subagent with session logging, it's misclassified as main
- Age threshold (24h) is arbitrary

**Session log parsing (lines 300-310):**
- Reads first `session_start` event only
- If JSON is corrupted in the middle, reads only first line → may miss parent_session
- Exception handling swallows errors silently (pass)

**Team agent detection (lines 315-336):**
- Scans project directory for concurrent sibling JSONL files
- "Concurrent" = within 300s (5 min) modification time window
- If two main sessions start >5min apart in the same project, not detected as team
- If two subagents start <5min apart but aren't from the same team, false positive (rare)

### Example Misclassifications

1. **Orchestrator running long after subagents finish:** Subagent mtime is 5:00pm, orchestrator mtime is 5:10pm (>300s gap) → orchestrator classified as main, not team_agent

2. **Interrupted main session + new main session:** First session killed at 5:00pm, new one starts at 5:10pm in same project → detected as stale, not part of team

3. **Custom session logging:** If user enables session logging for a subagent, filename check fails and age heuristic may wrongly classify it

**Assessment:** Detection logic is **best-effort**. Misclassifications are possible but rare. Langfuse filters by tags, so a misclassified trace is still traceable (just wrong session_type tag).

---

## 6. Error Handling

### Hook Entry Point (lines 1057-1220)

**Missing env vars:**
```python
if not public_key or not secret_key:
    log("ERROR", "Langfuse API keys not set ...")
    sys.exit(0)
```
✅ Logs and exits gracefully (no crash)

**Tracing disabled:**
```python
if os.environ.get("TRACE_TO_LANGFUSE", "").lower() != "true":
    debug("Tracing disabled ...")
    sys.exit(0)
```
✅ Silent exit (expected behavior)

**Langfuse init failure (line 1150):**
```python
try:
    langfuse = Langfuse(...)
except Exception as e:
    log("ERROR", f"Failed to initialize Langfuse client: {e}")
    sys.exit(0)
```
✅ Logs error, exits gracefully

### Processing Loop (lines 1173-1185)

```python
for session_id, transcript_file, project_name in modified_transcripts:
    try:
        turns = process_transcript(...)
        total_turns += turns
    except Exception as e:
        log("ERROR", f"Failed to process session {session_id}: {e}")
        import traceback
        debug(traceback.format_exc())
        continue
```
✅ Catches exceptions per-session, logs traceback in debug mode, continues

### Sidecar Write Failure (lines 1199-1203)

```python
try:
    save_sidecar(sidecar)
    debug(f"Sidecar written: {len(sidecar)} sessions")
except Exception as e:
    log("ERROR", f"Failed to write sidecar: {e}")
```
✅ Logs error but doesn't exit (partial trace loss if write fails)

### Lock Release (lines 1216-1217)

```python
finally:
    release_sidecar_lock(sidecar_lock)
    langfuse.shutdown()
```
✅ Guaranteed cleanup

### Specific Error Scenarios

**Disk full:**
- `save_sidecar()` → write fails → caught, logged, no sidecar update
- Subsequent hook invocation tries to load old sidecar (which is valid), processes, fails to write again
- Traces still go to Langfuse, just not to sidecar
- Risk: Sidecar becomes stale until disk is freed

**Malformed JSON in transcript:**
```python
try:
    new_messages.append(json.loads(line))
except json.JSONDecodeError:
    continue
```
✅ Skips malformed lines gracefully

**Permission errors on transcript read:**
```python
try:
    with open(transcript_file) as f:
        for i, line in enumerate(f):
            ...
except Exception as e:
    log("ERROR", f"Failed to process session {session_id}: {e}")
```
⚠️ Entire session skipped if any permission error occurs

**Missing env var:** If `LANGFUSE_HOST` not set, defaults to `https://cloud.langfuse.com` (line 1079) ✅

---

## 7. Performance

### Execution Time Logging (lines 1206-1210)

```python
duration = (datetime.now() - script_start).total_seconds()
log("INFO", f"Processed {total_turns} turns from {len(modified_transcripts)} sessions ...")

if duration > 180:
    log("WARN", f"Hook took {duration:.1f}s (>3min), consider optimizing")
```

### Performance Characteristics

**File I/O:**
- Reads last N lines of all modified transcripts (line-by-line, not loading whole file)
- ✅ Efficient: doesn't load multi-MB files into memory
- Sidecar file is small (typically <100KB for thousands of sessions)

**JSON parsing:**
- Each line is one JSON object
- No nested deep structures
- Fast on modern Python (cpython json module is optimized)

**Langfuse API calls:**
- One `create_trace()` per turn
- Each trace creates multiple spans (generation + tools)
- Langfuse SDK batches/flushes (lines 1169, 1188, 1218)
- ⚠️ If 100 turns were missed (long gap), creates 100 traces sequentially (could be slow)

**Disk I/O bottleneck:**
- Lock acquisition can block if another hook is running (but typical hook runtime is <1s)
- Multiple hooks should serialize (acceptable for typical use)

**Socket timeout for health check:** 2 seconds max (line 36)

### Timeout Risks

**No timeout on:**
- Langfuse client init (could hang on network issue)
- `langfuse.flush()` (could hang if Langfuse is slow)
- Sidecar file write (unlikely, local filesystem)

**Recommended limits:**
- Hook should complete in <5s (normal case: 1-2s)
- If duration > 3min, warning is logged (line 1210)
- If duration > 10min, hook might be killed by parent process (depends on Claude Code timeout config)

**Assessment:** Performance is good for typical sessions (1-10 turns). Large backlogs (100+ missed turns) could be slow but shouldn't cause timeouts under normal conditions.

---

## 8. Sidecar Reconciliation Logic

**Function: reconcile_sidecar() (lines 378-426)**

Purpose: Fix "phantom" timestamps where stale sessions have `last_seen = time.time()` from pre-patch runs or state resets.

**Algorithm:**
1. Scan all transcript files in projects directory
2. Extract session_id and file_mtime from each
3. For each sidecar entry:
   - If session has a transcript file, correct `last_seen = file_mtime` (ground truth)
   - Also fix individual turn timestamps that are > file_mtime + 1

**Logic:**
```python
if abs(entry.get("last_seen", 0) - true_last_seen) > 1:
    entry["last_seen"] = true_last_seen
    for t in entry["turns"]:
        if t.get("ts", 0) > true_last_seen + 1:
            t["ts"] = true_last_seen
```

**Quality:**
- ✅ Corrects phantom timestamps
- ✅ Uses file_mtime as source of truth (correct approach)
- ✅ Handles resumed sessions (multiple transcripts with same sessionId)
- ⚠️ If transcript is deleted, sidecar entry is left as-is (ages out naturally, line 417)
- ⚠️ Only called once per hook run (line 1193), before sidecar write

---

## Summary of Findings

| Category | Finding | Severity | Impact |
|----------|---------|----------|--------|
| **Hook Lifecycle** | Stdin session_id is parsed but not used; discovery via filesystem scan instead | Low | No functional impact; allows recovery from missed sessions |
| **Sidecar Locking** | fcntl exclusive lock prevents TOCTOU races; atomic write via .tmp rename | None | ✅ Robust |
| **Sidecar JSON Validity** | Guaranteed valid JSON during write; safe if process dies mid-write | None | ✅ Safe |
| **Concurrent Writes** | Multiple hooks serialize via lock; idempotent turn updates prevent lost writes | None | ✅ Correct |
| **Health Check** | Socket-based (fast), but only tests TCP connectivity, not API health | Low | Langfuse "up" but unhealthy may be missed; queue fallback mitigates |
| **Queue/Retry** | Traces queued on failure, drained on next success; no TTL/DLQ | Medium | Queue grows indefinitely on prolonged Langfuse downtime (>24h); no mechanism to purge stale traces |
| **Trace Structure** | Properly hierarchical, tagged, with metadata and cost details | None | ✅ Well-formed |
| **Token Extraction** | Defaults to 0 for missing fields; uses last fragment with output_tokens > 0 | Low | Edge case: all fragments with output_tokens=0 → no usage data (rare in practice) |
| **Session Classification** | Best-effort heuristics; misclassifications possible but rare | Low | Wrong session_type tag in Langfuse; traces still queryable |
| **Error Handling** | Graceful exits and per-session error catching; lock always released | Low | Sidecar write failure not fatal; partial data loss possible |
| **Permission Errors** | If transcript unreadable, entire session skipped | Medium | Silent skipping; user won't know a session was missed |
| **Disk Full** | Sidecar write fails, not retried; subsequent hook can't update metrics | Medium | Metrics become stale until disk space freed |
| **Large Backlogs** | Sequential trace creation could be slow (100+ turns) | Low | Hook may take >3min; no timeout kill-switch |
| **Lock Timeout** | No timeout on lock acquisition; hook can block indefinitely | High | If another hook hangs, parent hook blocks forever |
| **Pricing Hardcoded** | Cache cost calculations use hardcoded prices (not fetched from API) | Low | Stale if Anthropic pricing changes |

---

## Recommendations

### High Priority

1. **Add timeout to lock acquisition (line 355):**
   ```python
   lf = open(SIDECAR_LOCK, "w")
   acquired = fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
   if not acquired:
       log("ERROR", "Sidecar lock held >5s, skipping this session")
       return None  # Skip this hook invocation
   ```
   Prevents deadlock if a hook hangs.

2. **Add queue TTL/max size (line 98):**
   ```python
   QUEUE_MAX_BYTES = 10 * 1024 * 1024  # 10MB
   if QUEUE_FILE.exists() and QUEUE_FILE.stat().st_size > QUEUE_MAX_BYTES:
       log("WARN", "Queue file >10MB, purging oldest 50%")
       traces = load_queued_traces()
       traces = traces[len(traces)//2:]  # Keep recent half
       clear_queue()
       for t in traces:
           queue_trace(t)
   ```
   Prevents unbounded queue growth.

3. **Add per-trace drain retry limit:**
   ```python
   MAX_DRAIN_RETRIES = 3
   trace_retry_count = trace_data.get("_retry_count", 0)
   if trace_retry_count >= MAX_DRAIN_RETRIES:
       log("WARN", f"Trace {session_id}/{turn_num} failed {trace_retry_count} times, dropping")
       drained += 1
       continue
   trace_data["_retry_count"] = trace_retry_count + 1
   ```
   Prevents poison pills from blocking the queue.

### Medium Priority

4. **Log permission errors with session_id for debugging (line 612):**
   ```python
   except PermissionError as e:
       log("WARN", f"Permission denied reading {transcript_file}: {e}")
   ```
   Make silent failures visible.

5. **Add fallback if health check times out (line 1098):**
   ```python
   health_check_ok = False
   try:
       health_check_ok = check_langfuse_health(host)
   except socket.timeout:
       log("WARN", f"Health check timed out for {host}")
       health_check_ok = False
   ```
   Explicit timeout logging.

6. **Validate pricing tier at init (main()):**
   ```python
   # Detect model from transcript or env, log pricing used
   log("INFO", f"Using cache pricing for {model}: read=${CACHE_READ_PRICE_PER_TOKEN*1e6:.2f}")
   ```
   Document pricing assumptions.

### Low Priority

7. **Update sidecar lock comment (line 367):**
   Add note: "Lock file is created but never deleted; safe (accumulation is negligible)."

8. **Fetch stdin hint (line 1067):**
   ```python
   if stdin_data.get("session_id"):
       log("DEBUG", f"Hook called for session {stdin_data['session_id']}")
   ```
   Make stdin processing visible in logs.

---

## Conclusion

The Langfuse hook is **well-architected and resilient** for normal operation. The lock-based sidecar protection is sound, trace structure is proper, and error handling is graceful. The main gaps are:

1. **No lock timeout** → potential deadlock in edge cases
2. **No queue bounds** → unbounded growth on Langfuse downtime
3. **No poison pill detection** → stuck traces can block indefinitely

For production use, implement the three high-priority recommendations above. The hook is currently safe for typical sessions (1-100 turns), and resilience scales well as long as queue and lock timeouts are in place.

