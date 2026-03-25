# Fix Plan — Audit Findings

## Groupes de fix (4 Haiku parallèles + 1 Sonnet validation)

### H1 — cache_audit.py : Classification & Cost (bugs #1, #2, #3)

| Bug | Ligne | Fix |
|-----|-------|-----|
| #1 write_up sémantique | L403 | `curr.first_cache_creation > prev.cache_creation * 0.5` (comparer creation vs creation) |
| #2 TTL off-by-one | L419 | `delta_s > TTL_BOUNDARY` → `delta_s >= TTL_BOUNDARY` |
| #3 Event cost flat 1h | L567-570 | Remplacer le calcul inline par appel à `compute_rewrite_cost(curr)` qui utilise déjà le split 5m/1h |

### H2 — langfuse_hook.py : Pricing + Sidecar + Locking (bugs #4, #6, #8, #9)

| Bug | Fix |
|-----|-----|
| #4 Pricing flat Sonnet | Ajouter dict PRICES par modèle (comme cache_audit.py), utiliser model du turn |
| #6 Sidecar stale quand Langfuse down | Déplacer update_sidecar() AVANT le check Langfuse (sidecar = toujours, Langfuse = best-effort) |
| #8 flock sans timeout | Utiliser `LOCK_NB` + retry loop (3 tentatives, 100ms sleep) avec fallback skip |
| #9 Queue non bornée | Ajouter `_retry_count` par trace, drop après 3 retries. Cap queue à 10MB. |

### H3 — token-dashboard.html : Chart + Animation + Fetch (bugs #5, #7, #11)

| Bug | Fix |
|-----|-----|
| #5 chart cache_1h - cache_5m | Les deux sont indépendants — afficher `cache_1h` directement (pas soustraire cache_5m) |
| #7 rAF infini | Ajouter condition d'arrêt : si `displayRate < 0.01` et `targetRate === 0` pendant 2s → cancelAnimationFrame |
| #11 Pas de fetch timeout | Ajouter AbortController avec timeout 8s sur chaque fetch, afficher erreur dans le status dot |

### H4 — Server perf + Feature gap (bugs #10 + cache-audit UI)

| Bug | Fix |
|-----|-----|
| #10 task-counts rescan à chaque poll | Ajouter cache mémoire avec TTL 30s (dict + timestamp, invalidé si stale) |
| Feature gap | Ajouter bouton "Cache Audit" dans le header qui appelle /api/cache-audit et affiche les résultats dans une modale/section |

### S1 — Sonnet Validation (post-fix)

- Relire chaque fichier modifié
- Vérifier : pas de régression, pas de dette technique introduite, cohérence cross-fichiers
- Valider que les pricing tables sont identiques partout après fix
- Vérifier les edge cases des nouveaux comportements
- Runtime test du server + cache_audit
