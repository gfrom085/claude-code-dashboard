# Claude Code Dashboard

Real-time token cache dashboard and cache audit system for Claude Code sessions.

## What it does

- **Token dashboard** — live HTTP server showing token usage, cache hit rates, and session metrics
- **Cache auditor** — analyzes cache invalidation patterns (prefix mutations, server evictions, TTL expiry) and estimates wasted cost
- **Langfuse hook** — PostToolUse hook that traces metrics to a Langfuse instance

## Structure

```
server/dashboard_server.py   # HTTP server: sidecar API + cache audit endpoint
server/token-dashboard.html  # Dashboard frontend
hooks/langfuse_hook.py       # Stop hook: trace to Langfuse + sidecar metrics
scripts/cache_audit.py       # Standalone cache invalidation auditor
```

## Usage

```bash
# Start the dashboard server
python3 server/dashboard_server.py

# Run a cache audit
python3 scripts/cache_audit.py --session <ID> --human
```
