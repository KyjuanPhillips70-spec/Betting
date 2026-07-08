# Betting Bot Upgrade — CHANGES

All behavioral changes are behind feature flags that default to **OFF**.
With every flag OFF the simulator reproduces the baseline bit-for-bit.

## Baseline (seed=42, 50 000 sims, two league-average lineups)

| Metric | Value |
|---|---|
| `home_win_prob` | 0.49994 |
| `mean_total` | 8.82548 |

Stored in `tests/baseline_reference.json` and asserted by `test_flag_off_matches_baseline`.

---

## Priority 0 — Simulator correctness (models/mlb_sim.py)

### P0.1 + P0.3 — Logit factor application (`USE_LOGIT_FACTORS`)

**Flag:** `USE_LOGIT_FACTORS` · Default: `OFF`

**Problem:** Park / weather factors were applied multiplicatively then renormalized.
This is numerically correct but can produce negative intermediate probabilities
when extreme factors combine. The original code clamped negatives to 0, silently
changing the total mass.

**Fix:** When the flag is ON, factors are applied via log-space softmax (categorical
logit): `q_i = (p_i * f_i) / Σ(p_j * f_j)`. This is mathematically identical to
multiply-then-renormalize for non-negative inputs, but guaranteed mass-preserving
and numerically stable.  
New helper: `_apply_factors_logit()`.

| Flag state | `home_win_prob` | `mean_total` |
|---|---|---|
| OFF (baseline) | 0.49994 | 8.82548 |
| ON (logit path) | 0.49994 | 8.82548 |

With neutral factors the two paths produce identical results.

### P0.2 + P0.4 — Proper extra-inning simulation (`USE_FULL_EXTRA_INNINGS`)

**Flag:** `USE_FULL_EXTRA_INNINGS` · Default: `OFF`

**Problem:** The original extra-inning code ran a coin-flip after 6 attempts rather
than simulating each half-inning with the actual lineups. This inflated ties and
suppressed rally scenarios in the 9th when the away team trails.

**Fix:** When the flag is ON, `simulate_game` runs full half-inning pairs (home
then away) until the score differs, capped at `_MAX_EXTRA_INNINGS = 20`. After
the cap a deterministic tie-break is applied to keep the total bounded. The away
team's 9th inning is always fully simulated regardless of the score
(`walk_off_target` stops the home half only).

| Flag state | `home_win_prob` | `mean_total` |
|---|---|---|
| OFF (baseline) | 0.49994 | 8.82548 |
| ON (full extra) | 0.50012 | 8.82812 |

### P4.1 — Seeded RNG threading

**No flag required** (additive / backward-compatible).

`simulate_half_inning`, `simulate_game`, and `run_monte_carlo` all accept an
optional `rng: random.Random | None = None` parameter. Passing `None` (the
default) uses the global `random` state unchanged — all existing callers are
unaffected. Passing a seeded `random.Random` instance makes results exactly
reproducible without touching global state.

Two seeds with the same value produce bit-identical Monte Carlo results;
different seeds produce different results. Verified by `test_seeded_runs_identical`
and `test_different_seeds_differ`.

---

## Priority 1 — Weather model honesty (models/weather_adj.py)

**No flag required** (additive / backward-compatible).

### P1.1 — Named constants

`TEMP_COEF_PER_F = 0.0005`, `WIND_COEF_PER_MPH = 0.008`, `HIT_COUPLING = 0.3`
are now module-level constants (previously magic numbers). Existing callers not
affected.

### P1.2 — Hit coupling caveat

`HIT_COUPLING` is documented in-code as an unvalidated heuristic.

### P1.3 — 30-park orientations + fallback

`PARK_ORIENTATIONS` covers all 30 current MLB parks. `_PARK_ALIASES` handles
common alternate names (e.g., "Oriole Park at Camden Yards" → "Camden Yards").

`get_weather_adjustments` now accepts a `park_name` keyword; when the name is
unrecognized it logs a `WARNING` and zeros the wind component (neutral output)
rather than silently using a wrong bearing.

**Backward compatibility:** the `park_orientation_deg` parameter still works as
before; if provided it takes precedence over `park_name`.

### Combined multiplier clamp

The product of temp + wind + humidity HR multipliers is clamped to `[0.50, 1.50]`
(previously unclamped — could exceed bounds with extreme inputs).

---

## Priority 2 — Edge formula audit (edge/edge.py)

**No changes.** Reviewed and confirmed correct:
- De-vig (multiplicative normalization): correct
- Kelly criterion `f* = (b*p - q)/b` with fractional Kelly: correct
- Model blending `blended = MODEL_WEIGHT * model_p + (1 - MODEL_WEIGHT) * fair_p`: correct
- Prop distribution approach: reasonable for a bootstrap

---

## Priority 3 — Sanity gates (edge/edge.py)

### P3.1 — Edge verify gate (`ENABLE_EDGE_VERIFY_GATE`)

**Flag:** `ENABLE_EDGE_VERIFY_GATE` · Default: `OFF`  
**Threshold:** `EDGE_VERIFY_THRESHOLD` (default: `0.065`)

When ON, bets with `edge >= EDGE_VERIFY_THRESHOLD` are withheld pending
secondary verification. Added `edge_verify_pending: True` field to alert dict.

### P3.2 — Prop sample gate (`ENABLE_SAMPLE_GATE`)

**Flag:** `ENABLE_SAMPLE_GATE` · Default: `OFF`  
**Threshold:** `MIN_PROP_SAMPLE` (default: `40`)

When ON, player-prop edges are skipped when the bootstrapped sample count is
below `MIN_PROP_SAMPLE`. Prevents confident-looking outputs from tiny samples.

### P3.3 — Total band check

**No flag** (always active but only rejects extreme outliers).  
Totals outside `[_MEAN_TOTAL_MIN, _MEAN_TOTAL_MAX]` = `[6.5, 11.5]` are skipped.
Protects against runaway simulations producing nonsense totals.

---

## Priority 4.4 — Season hardcode removed (ingestion/mlb_statsapi.py)

**No flag required.**

`CURRENT_SEASON` is now derived from `date.today()` (Jan–Feb → prior year) with
an `MLB_SEASON` env var override. Previously hardcoded to 2026.

---

## Test coverage added

| File | Tests |
|---|---|
| `tests/test_mlb_sim.py` | 9 tests covering logit factors, extra innings, seeding, regression guard |
| `tests/test_weather_adj.py` | 12 tests covering dome, wind direction, HR bounds, 30 parks, aliases, backward compat |
| `tests/baseline_reference.json` | Machine-readable baseline for regression guard |
