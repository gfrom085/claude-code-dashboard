# S2 — Cost Model & Metrics Accuracy Audit

**Date:** 2026-03-25
**Scope:** `hooks/langfuse_hook.py`, `scripts/cache_audit.py`, `server/dashboard_server.py`, `server/token-dashboard.html`
**Auditor:** automated subagent (a0d1efe38aafd2fd0)

---

## 1. Pricing Tables Consistency

### Tables found

| File | Model | input | cache_read | cache_write_5m | cache_write_1h |
|------|-------|-------|------------|----------------|----------------|
| `langfuse_hook.py` (constants) | sonnet only | $3.00 | $0.30 | $3.75 | $6.00 |
| `cache_audit.py` PRICES dict | claude-sonnet-4-6 | $3.00 | $0.30 | $3.75 | $6.00 |
| `cache_audit.py` PRICES dict | claude-sonnet-4-5-20250514 | $3.00 | $0.30 | $3.75 | $6.00 |
| `cache_audit.py` PRICES dict | claude-opus-4-6 | $15.00 | $1.50 | $18.75 | $30.00 |
| `CLAUDE.md` (docs) | Sonnet | $3.00 | $0.30 | $3.75 | $6.00 |

### Assessment

**Values are internally consistent across all files for Sonnet.** No drift detected.

**Issues:**

1. **`langfuse_hook.py` has no per-model lookup.** It uses flat constants (`CACHE_BASE_PRICE_PER_TOKEN`, etc.) hardcoded to Sonnet rates. If an Opus session is processed, `savings` and `surcharge` will be calculated at Sonnet prices (5× undercount for Opus). The model name is extracted from the transcript but never used for pricing in this file.

2. **`langfuse_hook.py` is missing `claude-opus-4-6` in its cost logic.** `cache_audit.py` correctly has a per-model `PRICES` dict and a `get_prices(model)` fallback. The hook only has the four flat constants. This is a design gap, not a value error.

3. **`cache_write_1h` for Opus ($30.00):** The ratio vs. input is 2.0× (same as Sonnet ratio). The Sonnet ratio is also 2.0× ($6.00 / $3.00). Consistent in structure. Values appear correct per Anthropic's published pricing for extended cache (1h = 2× input rate).

4. **No `output_tokens` pricing anywhere.** Output is tracked in the sidecar but no cost is assigned to it. This is a scoping decision (the project focuses on cache cost delta), but it means total session cost is not available from the dashboard.

**Verdict:** Values are correct and consistent for Sonnet. The hook's flat-constant approach is a silent bug for Opus sessions.

---

## 2. Gauge Calculation Accuracy

### Data flow

```
sidecar JSON (per-session, per-turn: {cache_read, ts, ...})
  → updateGauge() [HTML line ~471]
    → totalCacheRead = sum of all active sessions' all-turns cache_read
    → delta = totalCacheRead - prevCacheTotal (cumulative diff)
    → dtMin = (tsNow - prevCacheTs) / 60000   (poll interval in minutes)
    → rawRate = max(0, delta / dtMin / 1e6)   (M tok/min)
  → targetRate = rawRate
  → gaugeAnimFrame() → displayRate (animated decay)
    → displayRate * 0.30 → gaugeCostMin
```

### Math verification

- `delta / dtMin / 1e6` — correct conversion from tokens/min to M tok/min.
- `dtMin = (tsNow - prevCacheTs) / 60000` — `Date.now()` returns ms, dividing by 60000 gives minutes. **Correct.**
- The poll interval is 5000 ms → `dtMin ≈ 0.0833`. If 1M new tokens appear between polls: `1_000_000 / 0.0833 / 1e6 = 12 M tok/min`. Sanity check passes.

### `displayRate * 0.30` — "Coût cache/min"

Line 455–457:
```javascript
const costMin = displayRate * 0.30;
document.getElementById('gaugeCostMin').textContent =
  costMin > 0.005 ? `$${costMin.toFixed(2)}/min` : '$0';
```

**What this does:** Multiplies M tok/min by `0.30` to get $/min.

**What it should do:** `cache_read` rate is $0.30/MTok. So 1 M tok/min × $0.30/MTok = $0.30/min. The formula `displayRate * 0.30` is **numerically correct**.

**However, `displayRate` measures the rate of change of `cache_read` tokens only.** It does not include cache_creation tokens (which are more expensive). The label "Coût cache/min" therefore represents cache *read* cost only, not total cache activity cost. This is not documented in the UI.

**Issue:** The label is misleading. It should be labeled "Coût read/min" or qualified. A full cache cost rate would need to weight write tokens at $3.75–$6.00/MTok, which would require separate per-token-type rate tracking.

### Peak session rate (line 503–509)

```javascript
const dt = (last.ts - prev.ts) / 60;   // seconds→minutes
const r = (last.cache_read || 0) / dt / 1e6;
```

**Bug:** `last.ts` and `prev.ts` are Unix timestamps in **seconds** (set by `file_mtime` in the hook). Dividing by 60 gives minutes — correct for this context. **But `r` is the cache_read of the last turn only divided by the inter-turn interval.** This is the rate of cache tokens *consumed in the last turn* divided by time since the previous turn — not a rate of change of cumulative tokens. This differs from the gauge's `delta/dtMin` which measures cumulative growth rate across the poll window. The two "M/min" metrics are methodologically inconsistent.

---

## 3. Cache Audit Metrics

### 3.1 `DROP_THRESHOLD = 0.50`

**Definition:** a cache invalidation event is triggered when `curr.first_cache_read < prev.cache_read * 0.50`.

**Calibration analysis:**

- A 50% threshold means a session must lose more than half its cache reads to be flagged. Gradual degradation (e.g., 10% per turn over 5 turns) accumulates to 41% loss — never exceeds the threshold in any single turn. This is a known limitation of single-step detection.
- **False negatives:** gradual cache erosion from context growth (new tools added each turn increasing prefix length) will not be detected. The threshold only catches abrupt drops.
- **False positives:** unlikely at 50%. A legitimate /compact halving the context would fire, but `CONTEXT_PRUNING` detection via `compact_boundary` messages happens first and returns early.
- The threshold is conservative (avoids false positives) at the cost of missing gradual degradation. This is a deliberate trade-off, but it is undocumented.

**Recommendation:** Consider adding a rolling trend metric (e.g., slope of `first_cache_read` over last N turns) as a complementary signal.

### 3.2 `compute_rewrite_cost` — cache_write_1h for all events

`compute_rewrite_cost` (line ~454–459) correctly uses `turn.cache_5m` and `turn.cache_1h` with their respective rates:
```python
cost_5m = turn.cache_5m * prices["cache_write_5m"] / 1_000_000
cost_1h = turn.cache_1h * prices["cache_write_1h"] / 1_000_000
```
This function is **correct** — it uses the actual breakdown.

**However**, the inline `rewrite_cost` in the event detection loop (line ~567–570) is different:
```python
rewrite_cost = round(
    curr.first_cache_creation * prices["cache_write_1h"] / 1_000_000, 6
)
```
This uses `first_cache_creation` (total creation tokens at turn start) multiplied **entirely at the 1h rate**, ignoring the 5m/1h split. This is intentionally conservative ("assume worst case = 1h") but is documented only in a comment. It can overstate event cost by up to 37% if the rewrite was entirely 5m tokens ($3.75 vs $6.00).

**Result:** `CacheEvent.rewrite_cost_usd` systematically overstates cost. `SessionMetrics.wasted_cost_usd` aggregates from `CacheEvent.rewrite_cost_usd`, so it is also overstated. The `compute_rewrite_cost` function (correct split-rate version) is never called in the hot path — it exists but is not used.

**Bug severity:** Medium. Reported costs are real money but consistently higher than actual.

### 3.3 `counterfactual_savings` math

```python
read_rate = prices["cache_read"] / 1_000_000      # $/token
write_rate = prices["cache_write_1h"] / 1_000_000  # $/token (conservative)
counterfactual_savings = tokens_wasted * (write_rate - read_rate)
```

For Sonnet: `tokens_wasted * (6.00 - 0.30) / 1_000_000 = tokens_wasted * 5.70 / 1_000_000`

**Interpretation:** "If these tokens had been cache-read instead of cache-written (1h), we would have saved this much per token." The formula is `(write_cost - read_cost) per token × tokens`, which equals the extra cost paid for writing vs reading. This is semantically a cost of the invalidation, not an opportunity saving.

**Issue:** The variable is named `counterfactual_savings` but it computes "extra cost paid due to invalidation" (= overpayment). In the human output it is labeled "Counterfactual savings" which is confusing — a high value means more was wasted, not more was saved. The sign and semantics are correct (positive = cost of invalidation), but the naming implies savings were missed rather than costs were incurred.

**Minor semantic issue:** The `write_rate` uses `cache_write_1h` (conservative). The actual write could have been 5m at $3.75. This overstates by up to 37%, consistent with the event cost overstatement above.

### 3.4 `efficiency_pct` formula

```python
efficiency = (total_read / total_cache * 100) if total_cache > 0 else 100.0
# where total_cache = total_read + total_creation
```

**Interpretation:** "Of all cache-touched tokens (reads + writes), what fraction were reads (cheap)?"

This metric makes sense as a cache hit rate indicator: 100% means every cache-written token was subsequently read (perfect reuse), 0% means all tokens were written but never read from cache.

**However:** The metric conflates write and read in a single denominator, making it time-order dependent. A session with 100K writes on turn 1 followed by 500K reads on turns 2–5 shows 83% efficiency. A session with the same tokens but interleaved writes and reads shows the same 83%. But the first session is healthy (write once, read many) while the second might be less efficient if writes repeat. The metric is a reasonable approximation but cannot distinguish between "write-then-reuse" vs "write-repeatedly".

**Edge case:** A session with 0 reads and 0 writes returns `efficiency_pct = 100.0` (the default in the empty-session case). A session with only writes (0 reads) correctly returns 0%. Division by zero is guarded.

---

## 4. Turn Counting Consistency

### `dashboard_server.py:scan_task_counts()` vs `cache_audit.py:extract_turns()`

Both implement the same conceptual turn model: **a turn = non-tool-result user message followed by at least one assistant message**.

#### `scan_task_counts()` (server, lines 66–113)

```python
if role == "user":
    is_tool_result = ...
    if not is_tool_result:
        if current_user and has_assistant:
            turn_n += 1
        current_user = True
        has_assistant = False
elif role == "assistant":
    has_assistant = True
# Finalize last: if current_user and has_assistant: turn_n += 1
```

- Counts `turn_n` as a simple integer.
- Assigns Task tool calls to `turn_n + 1` (prospective).
- Uses `obj.get("type") or obj.get("message", {}).get("role", "")` for role detection.

#### `extract_turns()` (cache_audit, lines 187–331)

```python
if msg_type == "user" and not is_meta:
    if is_tool_result(obj): continue
    if in_user_turn and has_assistant and last_usage:
        finalize_turn()  # turn_number += 1 inside
    in_user_turn = True; has_assistant = False
elif msg_type == "assistant":
    has_assistant = True
    # ... complex group tracking
```

- Guards on `isMeta` flag (ignores meta injections).
- Guards on `last_usage` (requires at least one usage-bearing assistant message).
- Groups streaming fragments by `message.id`.

#### Divergence risks

1. **`isMeta` filter:** `extract_turns()` skips messages where `obj.get("isMeta", False)` is True. `scan_task_counts()` does **not** check `isMeta`. If a meta user message appears between two real user messages, `scan_task_counts()` may count an extra turn that `extract_turns()` ignores.

2. **`last_usage` requirement:** `extract_turns()` only finalizes a turn if `last_usage` is not None (assistant had output_tokens > 0). `scan_task_counts()` only requires `has_assistant = True`. A turn with an assistant message that has no usage data would be counted by `scan_task_counts()` but not by `extract_turns()`.

3. **Prospective turn assignment:** `scan_task_counts()` uses `cur_turn = turn_n + 1` (prospective) when assigning Task calls, meaning the Task seen on the nth user message is labeled as turn n+1. `extract_turns()` increments `turn_number` only when a user message arrives after a completed assistant response, so T1 is the first completed turn. These are off by one in the same direction — both count the turn as the finalized completed exchange, but the prospective labeling in the server means a Task call on the current (incomplete) turn gets labeled as if it were the next completed turn. If both counts match at session end, labels still align.

4. **Subagent sessions:** `scan_task_counts()` uses a composite key `{raw_sid}::{stem}` for subagent files (lines 48–50). `extract_turns()` does not handle subagent files (it processes a single filepath at a time and doesn't use composite keys). The sidecar uses composite keys for subagent sessions. The `taskCounts` API result uses composite keys too. This is consistent.

**Summary:** The two counters can diverge on sessions containing `isMeta` user messages or usage-free assistant messages. For normal interactive sessions these edge cases are rare. The discrepancy would manifest as Task-count labels appearing on the wrong turn in the dashboard UI.

---

## 5. Edge Cases in Metrics

### Division by zero

| Location | Guard | Status |
|----------|-------|--------|
| `efficiency_pct` | `if total_cache > 0 else 100.0` | Protected |
| `invalidation_rate` | `max(total_turns - 1, 1)` | Protected |
| `compute_rewrite_cost` | No division; multiplication only | Safe |
| `counterfactual_savings` | No division; multiplication only | Safe |
| `updateGauge()` HTML | `if (dtMin > 0.01)` | Protected |
| `turnBar()` HTML | `const tot = ...; const pR = tot ? ... : 0` | Protected |
| Peak session rate HTML | No guard on `dt > 0` | **Unprotected** — if `last.ts === prev.ts`, `dt = 0`, `r = Infinity/NaN` |

**Bug (low severity):** In `updateGauge()`, the per-session peak rate computation (line ~506–508) divides by `dt` without checking `dt > 0`. If two turns have the same timestamp (possible when `ts = file_mtime` is identical for consecutive turns in the same second), `r = Infinity`. This would display "Inf M/min" in the gauge stat row.

### Negative values

- `cache_savings_usd` in the hook: computed as `cache_read * (BASE - READ_PRICE)`. `BASE > READ_PRICE` always ($3.00 > $0.30), so savings are always ≥ 0. Safe.
- `cache_surcharge_usd`: `cache_5m * (5m_rate - BASE)` + `cache_1h * (1h_rate - BASE)`. Both `5m_rate > BASE` ($3.75 > $3.00) and `1h_rate > BASE` ($6.00 > $3.00). Always ≥ 0. Safe.
- `counterfactual_savings`: `tokens_wasted * (write_rate - read_rate)`. `write_rate > read_rate` always. Always ≥ 0. Safe.
- `net` in cost section (HTML): `savings - surcharge`. Can be negative if cache write surcharge exceeds read savings. UI correctly shows in red. Safe by design.
- `delta` in gauge: `Math.max(0, delta / dtMin / 1e6)` guards against negative delta (cache_read counter could theoretically decrease if sessions age out of the active window). Protected.

### Overflow

- Token counts are JavaScript `Number` (float64). Max safe integer: ~9×10¹⁵. At 200K tokens/turn × 1000 turns = 2×10⁸ per session × 100 active sessions = 2×10¹⁰ total. Well within safe integer range.
- Python token sums: Python int is arbitrary precision. No overflow possible.
- USD values: rounded to 6 decimal places. No overflow at any realistic scale.

### Empty sessions / single-turn sessions

- **Empty session** (`turns = []`): `compute_session_metrics()` returns early with all-zero metrics and `efficiency_pct = 100.0`. The empty-session early return is at line ~469–475. Safe.
- **Single-turn session** (`turns = [T1]`): The loop in `analyze_session()` starts at `i = 1`, so no event detection occurs (the `i == 0` case continues immediately). `total_turns = 1`, `total_invalidations = 0`, `invalidation_rate = 0 / max(0, 1) = 0`. Safe.
- **Sessions with all zero `cache_read`:** `prev.cache_read == 0` → `continue` (line ~547). Detection is skipped. This means a session that never reads from cache generates zero events, not spurious ones. Safe.

### `cache_1h` chart rendering

In `renderChart()` (HTML line ~831):
```javascript
data: sorted.map(t => Math.max(0, (t.cache_1h||0) - (t.cache_5m||0))),
```
`cache_1h` in the sidecar is `ephemeral_1h_input_tokens` (pure 1h tokens), and `cache_5m` is `ephemeral_5m_input_tokens`. These are **separate counts** — `cache_1h` is NOT a superset of `cache_5m`. The subtraction `cache_1h - cache_5m` is therefore semantically incorrect. If a turn has 50K 5m tokens and 80K 1h tokens, the chart shows -30K (clamped to 0 by `Math.max`). In practice, a turn usually has either 5m OR 1h writes, not both, so the bug may not manifest often — but when it does, 1h write tokens are undercounted or zeroed.

**Bug severity:** Medium. The chart label "Cache 1h Write" is wrong when both 5m and 1h cache writes coexist in the same turn.

---

## Summary of Findings

| # | Severity | File | Finding |
|---|----------|------|---------|
| 1 | Medium | `langfuse_hook.py` | No per-model pricing; Opus sessions billed at Sonnet rates |
| 2 | Medium | `cache_audit.py` | Event `rewrite_cost_usd` always uses `cache_write_1h` rate regardless of actual 5m/1h split — overstates by up to 37% |
| 3 | Medium | `token-dashboard.html` | `cache_1h - cache_5m` subtraction in chart is semantically wrong (they are independent counters) |
| 4 | Low | `token-dashboard.html` | Gauge "Coût cache/min" only measures cache_read cost, not total cache cost — label is misleading |
| 5 | Low | `token-dashboard.html` | Peak session rate divides by `dt` without `dt > 0` guard → potential Infinity display |
| 6 | Low | `cache_audit.py` | `counterfactual_savings` naming is inverted — it measures cost of invalidation, not opportunity savings |
| 7 | Low | `dashboard_server.py` | Turn counter can diverge from `extract_turns()` on sessions with `isMeta` user messages |
| 8 | Info | `cache_audit.py` | `DROP_THRESHOLD = 0.50` misses gradual cache erosion; only detects abrupt drops |
| 9 | Info | `cache_audit.py` | `compute_rewrite_cost()` function (correct split) exists but is never called in the detection hot path |

### Pricing table consistency: PASS (all values match, Sonnet and Opus correct)
### Gauge math: PASS (formula numerically correct, but semantic scope limited to reads)
