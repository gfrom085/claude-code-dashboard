# Claude Code Dashboard

Token metrics dashboard + cache audit system for Claude Code sessions.

## Structure

```
server/dashboard_server.py   # HTTP server: sidecar API + cache audit + claude-usage endpoint
server/token-dashboard.html  # Frontend: gauges tokens + panel Claude.ai Usage
lib/claude_ai_poller.py      # Daemon: poll claude.ai usage via Chrome cookie bridge
lib/sdk_metrics.py           # Helper: dual-write SDK metrics (JSONL + stream)
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
- `GET /api/claude-usage` - Claude.ai usage data (five_hour, seven_day, overage, credits)
- `GET /api/health` - Server health check (inclut `claude_ai_bridge` status)

## Claude.ai Usage Poller — Maintenance

### Architecture

```
Chrome Cookies DB → claude_ai_poller.py → /tmp/claude-ai-usage.json → dashboard_server.py → frontend
     (AES v11)         (HTTP GET)              + stream JSONL              (REST + SSE)
```

Le poller lit `sessionKey` depuis Chrome (GNOME Keyring decrypt), poll l'API claude.ai, et écrit dans deux fichiers :
- `/tmp/claude-ai-usage.json` — snapshot REST pour `/api/claude-usage`
- `/tmp/token-metrics-stream.jsonl` — ligne SSE pour le gauge temps réel (`source: "claude-ai-usage"`)

ADR : [[adrs/adr-chrome-cookie-bridge-over-mcp]]

### Démarrage

```bash
# Poller (daemon, doit tourner en permanence)
nohup python3 -u lib/claude_ai_poller.py > /tmp/claude-ai-poller.log 2>&1 &

# Vérifier
curl -s http://localhost:8765/api/claude-usage | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'ok={d[\"bridge_ok\"]} five_hour={d[\"usage\"][\"five_hour\"][\"utilization\"]}%')"
```

### Circuit Breaker

Le poller se coupe automatiquement (exit code 2) sur :

| Signal | Cause | Action |
|--------|-------|--------|
| `http_401/403` | Session expirée | Se reconnecter à claude.ai dans Chrome, relancer poller |
| `http_429` | Rate limited | Attendre, relancer poller |
| `org_changed` | Compte switché dans Chrome | Relancer poller (il re-lock le nouvel orgId) |
| `schema_invalid` | API claude.ai a changé | Investiguer la réponse, adapter le code |
| `consecutive_errors` | 2 erreurs réseau consécutives | Vérifier la connexion, relancer poller |
| `no_session_cookie` | Pas connecté à claude.ai | Se connecter dans Chrome |

**Quand le breaker trip :** le fichier `/tmp/claude-ai-usage.json` contient `"breaker": "tripped"` avec la raison. Le dashboard affiche un message FR dans le panel.

### Diagnostic

```bash
# Log du poller
tail -f /tmp/claude-ai-poller.log

# Snapshot actuel
cat /tmp/claude-ai-usage.json | python3 -m json.tool

# Breaker tripped ?
python3 -c "import json; d=json.load(open('/tmp/claude-ai-usage.json')); print(d['breaker'], d.get('reason',''))"

# Le poller tourne ?
pgrep -f claude_ai_poller || echo "POLLER DOWN"

# Relancer après breaker
python3 -u lib/claude_ai_poller.py --once  # test one-shot d'abord
nohup python3 -u lib/claude_ai_poller.py > /tmp/claude-ai-poller.log 2>&1 &
```

### Polling adaptatif

- `five_hour.utilization <= 80%` → poll toutes les **120s**
- `five_hour.utilization > 80%` → poll toutes les **30s** (mode fast)
- Configurable : `--interval 120 --high-threshold 80 --fast-interval 30`

### Dépendances runtime

- **Chrome** doit être ouvert (pour que la DB cookies soit à jour)
- **GNOME Keyring** déverrouillé (automatique après login desktop)
- **Python système** avec `gi.Secret` (PyGObject) et `cryptography` — préinstallés sur Ubuntu
- **Pas besoin** d'un tab claude.ai ouvert ni de l'extension Chrome MCP

## Pricing (Sonnet)

| Type | Rate ($/MTok) |
|---|---|
| Input | $3.00 |
| Cache read | $0.30 (0.1x) |
| Cache write 5m | $3.75 (1.25x) |
| Cache write 1h | $6.00 (2.0x) |


<!-- ⚠ Tout le contenu CI-DESSUS est maintenu manuellement. L'atlas ne régénère que le bloc ci-dessous. -->
<!-- atlas:start:claude-code-dashboard -->
## Atlas — projet claude-code-dashboard
<!-- generated: 2026-03-30T19:16:48Z — regenerer via: python3 generate_atlas.py --project claude-code-dashboard -->
<!-- 15 doc(s) exclus (parse errors) — carte potentiellement incomplete -->

[[plans/plan-realtime-gauge-mitmproxy-sse]] (active) "Real-time gauge via mitmproxy SSE — flux temps réel proxy → dashboard EMA smooth"
  |-- [[handoffs/handoff-dashboard-audit-gauge-fix-2026-03-25]] (active) "Audit multi-agent 11 bugs fixés + gauge animation fixée + plan realtime SSE p..."
<!-- atlas:end:claude-code-dashboard -->
