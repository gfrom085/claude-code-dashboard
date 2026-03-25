# S1 — Architecture & Data Flow Audit

**Date:** 2026-03-25
**Scope:** `hooks/langfuse_hook.py`, `server/dashboard_server.py`, `server/token-dashboard.html`, `scripts/cache_audit.py`, `CLAUDE.md`
**Auditor:** subagent ab27d91bd686ae597

---

## 1. Architecture Overview

### Component Map

```
Claude Code (Stop hook fires after each assistant response)
    │
    ▼
hooks/langfuse_hook.py
    ├── Reads: ~/.claude/projects/**/*.jsonl  (transcripts)
    ├── Reads: ~/.claude/state/langfuse_state.json  (incremental cursor)
    ├── Reads: /tmp/langfuse-token-metrics.json  (sidecar, for fork reuse calc)
    ├── Writes: /tmp/langfuse-token-metrics.json  (sidecar, via lock+atomic rename)
    ├── Writes: ~/.claude/state/langfuse_state.json  (cursor update)
    ├── Writes: ~/.claude/state/pending_traces.jsonl  (offline queue)
    └── Sends: Langfuse API (traces, spans, generations)

/tmp/langfuse-token-metrics.json  (sidecar — shared state file)
    │
    ▼
server/dashboard_server.py  (HTTP server, port 8765, localhost only)
    ├── GET /          → serves token-dashboard.html
    ├── GET /api/sidecar     → reads+returns sidecar JSON as-is
    ├── GET /api/task-counts → re-reads sidecar + scans transcripts
    ├── GET /api/cache-audit → subprocess: scripts/cache_audit.py --no-write
    └── GET /api/health      → {"ok": true, "sidecar_exists": bool}
    │
    ▼
server/token-dashboard.html  (frontend, single-page, polls every 5s)
    ├── Fetches /api/sidecar         (session-level token metrics)
    ├── Fetches /api/task-counts     (Task tool usage overlaid on session cards)
    └── Fetches /api/cache-audit     (on-demand, not auto-polled)

scripts/cache_audit.py  (standalone CLI + called as subprocess by server)
    └── Reads: ~/.claude/projects/**/*.jsonl  (transcripts, independently)
```

### Data Flow Narrative

1. After each Claude response, Claude Code fires the Stop hook pointing to `langfuse_hook.py`.
2. The hook scans all `.jsonl` transcripts modified since the last cursor, extracts per-turn usage data, updates the sidecar file (protected by `fcntl.LOCK_EX` on a `.lock` file), and sends traces to Langfuse.
3. The dashboard server polls the sidecar from the frontend every 5 seconds via `/api/sidecar`. The server does not cache the file; it re-reads it on every request.
4. `/api/task-counts` additionally scans live transcript files to count `Task` tool calls. This is a heavier read path executed on every frontend poll.
5. `/api/cache-audit` is a separate code path invoked on-demand: it spawns `cache_audit.py` as a subprocess, parses its JSONL stdout, and returns structured JSON.

---

## 2. Data Contract Validation

### 2a. Hook → Sidecar (write side)

The hook writes sidecar entries via `update_sidecar()`. Schema per session entry:

```json
{
  "<session_id>": {
    "type": "main|subagent|fork|team_agent",
    "project": "<string>",
    "parent_session": "<string|null>",
    "turns": [
      {
        "n": <int>,
        "ts": <float epoch>,
        "input": <int>,
        "output": <int>,
        "cache_read": <int>,
        "cache_creation": <int>,
        "cache_5m": <int>,
        "cache_1h": <int>,
        "cache_savings_usd": <float>,
        "cache_surcharge_usd": <float>,
        "fork_cache_reuse": <float|null>
      }
    ],
    "last_seen": <float epoch>
  }
}
```

### 2b. Server → Frontend (read side)

`/api/sidecar` returns the sidecar dict **verbatim** with no transformation. The frontend consumes the following fields per session and per turn:

| Field | Used In |
|-------|---------|
| `s.type` | badge class (`badge-main`, `badge-subagent`, etc.) |
| `s.project` | project filter, grouping, card display |
| `s.parent_session` | fork display (fork arrow) |
| `s.last_seen` | active window filter, sort order |
| `s.turns[].n` | chart labels, turn count display |
| `s.turns[].ts` | time label on turn bars |
| `s.turns[].input` | turn bar proportions, chart |
| `s.turns[].cache_read` | gauge rate, chart, cost display |
| `s.turns[].cache_creation` | chart |
| `s.turns[].cache_5m` | chart (5m write layer) |
| `s.turns[].cache_1h` | chart (1h write = cache_1h - cache_5m) |
| `s.turns[].cache_savings_usd` | cost display |
| `s.turns[].fork_cache_reuse` | fork reuse % display |

**Contract validation result: PASS.** All fields written by the hook are consumed by the frontend under the same names. No renaming or transformation occurs at the server layer.

### 2c. Task-Counts Contract

`/api/task-counts` returns:
```json
{
  "<session_id>": {
    "<turn_n_string>": {
      "count": <int>,
      "agents": [{"desc": "<string>", "type": "<string>"}]
    }
  }
}
```

Frontend accesses `taskCounts[sid][String(t.n)]` — turn key is stringified. The server produces string keys (`str(cur_turn)`) matching this. **PASS.**

### 2d. Schema Drift Risk

One structural inconsistency was found:

**`cache_1h` field semantics differ between hook and frontend.**

- Hook `update_sidecar()` sets `cache_1h` = raw `ephemeral_1h_input_tokens` value from API usage.
- Frontend chart renders `cache_1h` layer as `Math.max(0, (t.cache_1h||0) - (t.cache_5m||0))`.

This subtraction is done only in the chart, not in the tooltip or cost calculations. The tooltip at `renderSessionCard > ttData` sends `w1h: (t.cache_1h||0)-(t.cache_5m||0)` — correctly computed inline. But the gauge cost calculation at line ~456:
```js
const costMin = displayRate * 0.30;
```
uses `cache_read` tokens only (ignoring write cost), which is a simplified approximation rather than a schema error. Acceptable but undocumented.

**No field name mismatches between hook output and server/frontend consumption.**

---

## 3. Single Points of Failure

### 3a. Sidecar file does not exist

- **`/api/sidecar`**: Returns `{}` (empty object). Frontend renders "Aucune session active". Graceful degradation. PASS.
- **`scan_task_counts()` in server**: Returns `{}` immediately if sidecar missing. PASS.
- **Hook `load_sidecar()`**: Returns `{}`. On first run, the sidecar is created fresh. PASS.
- **Hook `reconcile_sidecar()`**: Returns 0 corrected entries if sidecar missing. PASS.

### 3b. Langfuse is down

The hook implements a local queue (`pending_traces.jsonl`). When the TCP health check fails (`check_langfuse_health`), the hook queues all turns locally and exits 0. On the next hook invocation when Langfuse is reachable, `drain_queue()` replays them.

**Gap:** The sidecar is only updated in the `langfuse_available = True` branch (inside the `acquire_sidecar_lock()` block, lines 1162–1190). When Langfuse is down, the queuing path (lines 1100–1147) does NOT call `update_sidecar()`. This means the dashboard will not show live token metrics during a Langfuse outage, even though the data is locally queued. The sidecar update and the Langfuse trace are unnecessarily coupled.

**Severity: Medium.** The dashboard goes blind during Langfuse outages even though all data is locally available.

### 3c. Transcript format changes

The hook and server both parse the same JSONL transcript format. Key assumptions:

- First line contains `sessionId` field (used for session identification everywhere).
- Assistant messages have `message.usage` with `cache_read_input_tokens`, `cache_creation_input_tokens`, `cache_creation.ephemeral_5m_input_tokens`, `cache_creation.ephemeral_1h_input_tokens`.
- User messages have `type == "user"` and `message.content` is a list or string.

If Anthropic changes the transcript schema (e.g., usage field structure), both the hook and `cache_audit.py` fail silently — they default to 0 for missing fields. The dashboard would show zeros rather than crashing. This is resilient behavior.

**Gap:** No schema version check exists. A silent format change would produce misleading zero data rather than an explicit error.

---

## 4. Concurrency & File Locking

### 4a. Locking Strategy

The hook uses `fcntl.LOCK_EX` (exclusive lock) on `/tmp/langfuse-token-metrics.lock` for the full read-modify-write cycle of the sidecar:

```python
lock_fd = acquire_sidecar_lock()
try:
    sidecar = load_sidecar()
    # ... process all transcripts, modify sidecar in-place ...
    save_sidecar(sidecar)
finally:
    release_sidecar_lock(lock_fd)
```

`save_sidecar()` writes atomically via `tmp.replace(sidecar_file)` (rename syscall = atomic on Linux for same filesystem).

**Hook-to-hook concurrency: PROTECTED.** Multiple Stop hooks firing concurrently (e.g., in a team agent scenario) will serialize correctly via the lock.

### 4b. Server Read vs. Hook Write Race

The server reads the sidecar with `json.loads(SIDECAR_FILE.read_text())` without acquiring the lock. This creates a potential TOCTOU window:

1. Server calls `read_text()` — partially reads file during a rename.
2. On Linux, `rename()` is atomic at the filesystem level. A reader either sees the old file or the new file, never a partial write. The server's `read_text()` opens the file descriptor before the rename completes and reads the old inode, or opens after and reads the new inode.

**Assessment:** The atomic rename makes partial-read corruption impossible. However, if the server holds an open file descriptor across a rename, it reads the old data. On the next 5s poll it reads the new data. This is acceptable — the dashboard is eventually consistent with a worst-case lag of one poll interval.

**Verdict: ADEQUATE.** The locking strategy is correct for the write path. The read path is safe due to atomic rename semantics. No race condition that would corrupt data.

### 4c. Langfuse Down Path — Sidecar Not Locked

When Langfuse is unavailable (lines 1100–1147), the hook reads and updates `state` (langfuse_state.json) but never touches the sidecar. This path bypasses the lock entirely, which is correct since it doesn't modify the sidecar.

---

## 5. Security Review

### 5a. Input Validation on Cache-Audit Endpoint

The `_handle_cache_audit()` handler validates `session` and `project` query parameters against:

```python
SAFE_PARAM = re.compile(r"^[a-zA-Z0-9._\-]{1,128}$")
```

This regex is defined **inside** `_handle_cache_audit()` on every request (minor inefficiency, not a security issue). It correctly blocks:
- Path separators (`/`, `\`)
- Shell metacharacters (`;`, `|`, `` ` ``, `$`, `&`, etc.)
- Unicode and null bytes
- Excessively long inputs (128 char max)

These values are passed to `cache_audit.py` via `cmd.extend(["--session", val])` using `subprocess.run()` with a list (not shell=True). **No shell injection is possible.** PASS.

### 5b. Path Traversal in Cache-Audit Subprocess

The script path is computed as:
```python
script = Path(__file__).parent.parent / "scripts" / "cache_audit.py"
```
This is a fixed relative path from the server file's location. No user input influences this path. PASS.

`cache_audit.py` itself uses `PROJECTS_DIR = Path.home() / ".claude" / "projects"` — hardcoded, not user-influenced. PASS.

### 5c. CORS Policy

The server binds to `127.0.0.1` only (line 228: `HTTPServer(("127.0.0.1", PORT), Handler)`). No `Access-Control-Allow-Origin` header is sent. The comment in `send_json()` correctly notes: "No CORS header — dashboard is served from same origin (localhost:PORT)."

**Assessment:** Since the dashboard HTML is served from the same server on the same port, all API calls are same-origin. No CORS header is needed. However, if the dashboard is ever opened as a local file (`file://`) or from a different port, the API calls will be blocked by browsers. This is an intentional design constraint, not a vulnerability.

### 5d. No Authentication

The server has no authentication. It is localhost-only which limits exposure, but any local process or browser tab can query `/api/sidecar` (which contains full session token data) or trigger `/api/cache-audit` (which reads all local transcripts). This is acceptable for a local developer tool but should be noted.

### 5e. Subprocess Timeout

`cache_audit.py` is invoked with `timeout=30`. On a machine with thousands of transcript files, this may not be sufficient if the default limit of 20 sessions is hit with very large files. The 504 timeout response is handled gracefully. PASS.

---

## 6. Missing Features / Gaps

### 6a. Documented but Not Implemented

| Feature | Location | Status |
|---------|----------|--------|
| Cache audit triggered from dashboard UI | CLAUDE.md describes interactive workflow (read line_range, identify cause) | No UI trigger in frontend for cache audit; `/api/cache-audit` endpoint exists but the HTML has no button/section to call it |
| "Launch Explore agent on SERVER_EVICTION" | CLAUDE.md step 4b | No agent dispatch from dashboard |

### 6b. Implemented but Not Documented in README/CLAUDE.md

| Feature | File | Note |
|---------|------|------|
| `TRACE_TO_LANGFUSE` opt-in guard | `langfuse_hook.py:1072` | Not mentioned in README |
| Offline queue (`pending_traces.jsonl`) | `langfuse_hook.py:33,98-165` | Not mentioned in README |
| `reconcile_sidecar()` — stale timestamp correction | `langfuse_hook.py:378` | Not documented anywhere |
| `detect_session_type()` heuristics | `langfuse_hook.py:269` | Detection logic undocumented (relies on `/tmp/.claude-sessions/<id>.log`) |
| Session dismiss/archive (localStorage) | `token-dashboard.html:519` | No mention in docs |
| Project grouping toggle | `token-dashboard.html:551` | No mention in docs |
| Gauge VU-meter with exponential decay | `token-dashboard.html:317` | Not documented |
| `--no-write` flag on cache_audit.py when called via server | `dashboard_server.py:187` | Behavior difference from CLI not documented |

### 6c. Functional Gaps

1. **Sidecar decoupled from Langfuse availability (see §3b):** When Langfuse is down, the sidecar is not updated. The dashboard goes blind even though data is locally available.

2. **`/api/task-counts` scans transcripts on every frontend poll (every 5s).** This involves iterating all project directories, reading first lines of `.jsonl` files, and parsing sidecar to filter. On a machine with many sessions, this becomes expensive. There is no caching layer.

3. **No sidecar TTL/eviction from server side.** Old sessions accumulate in the sidecar indefinitely. The frontend filters by `last_seen` window, but the sidecar file grows without bound. The hook never prunes old entries.

4. **`task_count` parameter in `update_sidecar()` signature is accepted but never written to the sidecar entry.** The parameter exists but the value is neither stored in the turn entry nor the session entry, making the parameter dead code.

   ```python
   def update_sidecar(self, ..., task_count: int = 0) -> None:
       # task_count is never used in the function body
   ```

5. **Session type detection depends on `/tmp/.claude-sessions/<id>.log`** — a path that appears to be a convention invented by this hook (or an associated script). No documentation exists for what creates these logs or when they exist. If that convention changes or the logs are never created, all sessions fall back to either `subagent` (< 24h) or `main` (>= 24h), losing `fork` and `team_agent` classification.

6. **No `README.md` instructions for running the dashboard server.** The README describes the cache audit CLI and API endpoints but has no "how to start" section for `dashboard_server.py`.

---

## Summary Table

| Category | Finding | Severity |
|----------|---------|----------|
| Data Contract | All field names match across components | OK |
| Data Contract | `cache_1h` semantics inconsistency (raw vs. net) | Low |
| SPOF | Sidecar not updated when Langfuse is down | Medium |
| SPOF | No transcript schema version guard (silent zeros on format change) | Low |
| Concurrency | Hook-to-hook write serialized correctly via fcntl lock | OK |
| Concurrency | Server reads are safe due to atomic rename | OK |
| Security | Query param validation blocks injection | OK |
| Security | No path traversal risk in subprocess call | OK |
| Security | Same-origin CORS policy is correct | OK |
| Security | No auth (acceptable for localhost-only tool) | Info |
| Gap | Cache audit not wired to frontend UI | Medium |
| Gap | Sidecar grows without bound (no eviction) | Low |
| Gap | `task_count` param in `update_sidecar()` is dead code | Low |
| Gap | Session type detection depends on undocumented `/tmp/.claude-sessions/` | Medium |
| Gap | `/api/task-counts` re-scans disk on every 5s poll (no cache) | Low |
