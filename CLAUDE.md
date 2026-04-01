# Claude Code Dashboard

Token metrics dashboard + cache audit system for Claude Code sessions.

## Structure

```
server/dashboard_server.py   # HTTP server: sidecar API + cache audit endpoint
hooks/langfuse_hook.py       # Stop hook: trace to Langfuse + sidecar metrics
scripts/cache_audit.py       # Standalone cache invalidation auditor
```

## Cache Audit Workflow

When the user asks for a cache audit:

1. Run: `python3 scripts/cache_audit.py [--session ID] [--project NAME] [--human]`
2. Read the output (JSONL or human-readable)
3. For each `PREFIX_MUTATION`:
   a. Read the `line_range` lines from the transcript (Read tool)
   b. Identify the exact cause in `intervening_messages`
   c. Propose a corrective action if applicable
4. For each `SERVER_EVICTION`:
   a. Launch an Explore agent on the transcript to find patterns
   b. Check if other sessions in the same workspace had the same issue
5. Summarize: invalidation rate, total wasted cost, main causes
6. Ignore: `SESSION_START`, `CONTEXT_PRUNING`, `TTL_EXPIRED` (legitimate)

## Classifications

| Classification | Meaning | Action |
|---|---|---|
| `PREFIX_MUTATION` | Prefix changed (rename, skill load, etc.) | Investigate cause, possibly avoidable |
| `CONTEXT_PRUNING` | /compact reduced context | Legitimate, no action |
| `TTL_EXPIRED` | >1h gap between turns | Legitimate, no action |
| `SERVER_EVICTION` | Unexplained drop within TTL | Monitor for patterns |
| `SESSION_START` | First turn of session | Excluded from analysis |

## API

- `GET /api/cache-audit?session=<id>&project=<name>` - Run audit, returns JSON
- `GET /api/sidecar` - Current sidecar metrics
- `GET /api/task-counts` - Task tool usage per session
- `GET /api/health` - Server health check

## Pricing (Sonnet)

| Type | Rate ($/MTok) |
|---|---|
| Input | $3.00 |
| Cache read | $0.30 (0.1x) |
| Cache write 5m | $3.75 (1.25x) |
| Cache write 1h | $6.00 (2.0x) |


<!-- atlas:start:claude-code-dashboard -->
## Atlas — projet claude-code-dashboard
<!-- generated: 2026-03-30T19:16:48Z — regenerer via: python3 generate_atlas.py --project claude-code-dashboard -->
<!-- 15 doc(s) exclus (parse errors) — carte potentiellement incomplete -->

[[plans/plan-realtime-gauge-mitmproxy-sse]] (active) "Real-time gauge via mitmproxy SSE — flux temps réel proxy → dashboard EMA smooth"
  |-- [[handoffs/handoff-dashboard-audit-gauge-fix-2026-03-25]] (active) "Audit multi-agent 11 bugs fixés + gauge animation fixée + plan realtime SSE p..."
<!-- atlas:end:claude-code-dashboard -->
