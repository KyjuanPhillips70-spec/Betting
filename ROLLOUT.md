# Rollout Guide — Incremental Flag Enablement

Enable upgrades one flag at a time. Each step is independent and reversible.
Default state: all flags OFF — identical to pre-upgrade behavior.

## Flags summary

| Env var | Default | What it enables |
|---|---|---|
| `USE_LOGIT_FACTORS` | `0` | Numerically stable log-softmax factor application (P0.1/P0.3) |
| `USE_FULL_EXTRA_INNINGS` | `0` | Full half-inning extra-inning simulation (P0.2/P0.4) |
| `ENABLE_EDGE_VERIFY_GATE` | `0` | Withhold bets with edge ≥ EDGE_VERIFY_THRESHOLD (P3.1) |
| `EDGE_VERIFY_THRESHOLD` | `0.065` | Edge threshold for verify gate |
| `ENABLE_SAMPLE_GATE` | `0` | Skip props with sample < MIN_PROP_SAMPLE (P3.2) |
| `MIN_PROP_SAMPLE` | `40` | Minimum bootstrap sample size for props |
| `MLB_SEASON` | _(auto)_ | Override auto-derived MLB season year |

---

## Step 1 — Enable logit factors

```bash
export USE_LOGIT_FACTORS=1
```

Expected change: none visible in most runs (identical result to flag-OFF when
weather/park factors are within normal range). Guards against numerical edge
cases with extreme combined factors.

**Rollback:**
```bash
unset USE_LOGIT_FACTORS
# or
export USE_LOGIT_FACTORS=0
```

---

## Step 2 — Enable proper extra innings

```bash
export USE_FULL_EXTRA_INNINGS=1
```

Expected change: `home_win_prob` shifts ≤ 0.002, `mean_total` shifts ≤ 0.01
(reference: 0.49994 → 0.50012, 8.82548 → 8.82812). Slight increase in
simulation wall time for tied games.

**Rollback:**
```bash
unset USE_FULL_EXTRA_INNINGS
```

---

## Step 3 — Enable prop sample gate

```bash
export ENABLE_SAMPLE_GATE=1
# Optionally tighten the threshold (default 40):
export MIN_PROP_SAMPLE=50
```

Expected change: some player-prop bets suppressed in early season when sample
sizes are small. No change to game-line outputs.

**Rollback:**
```bash
unset ENABLE_SAMPLE_GATE
```

---

## Step 4 — Enable edge verify gate

```bash
export ENABLE_EDGE_VERIFY_GATE=1
# Optionally adjust threshold (default 6.5%):
export EDGE_VERIFY_THRESHOLD=0.07
```

Expected change: bets with edge ≥ 6.5% gain `edge_verify_pending: True` in the
alert dict and are withheld from output. Review these manually before enabling.

**Rollback:**
```bash
unset ENABLE_EDGE_VERIFY_GATE
```

---

## Recommended rollout order

1. `USE_LOGIT_FACTORS=1` — zero risk, silent correctness improvement
2. `USE_FULL_EXTRA_INNINGS=1` — small model improvement, watch for runtime change
3. `ENABLE_SAMPLE_GATE=1` — reduces noise in prop output
4. `ENABLE_EDGE_VERIFY_GATE=1` — enables manual review workflow for outlier edges

## GitHub Actions / CI

Set env vars in the repository **Secrets or Variables** (Settings → Secrets →
Actions variables):

```yaml
# In .github/workflows/daily_card.yml:
env:
  USE_LOGIT_FACTORS:      ${{ vars.USE_LOGIT_FACTORS }}
  USE_FULL_EXTRA_INNINGS: ${{ vars.USE_FULL_EXTRA_INNINGS }}
  ENABLE_SAMPLE_GATE:     ${{ vars.ENABLE_SAMPLE_GATE }}
  MIN_PROP_SAMPLE:        ${{ vars.MIN_PROP_SAMPLE }}
  ENABLE_EDGE_VERIFY_GATE:   ${{ vars.ENABLE_EDGE_VERIFY_GATE }}
  EDGE_VERIFY_THRESHOLD:     ${{ vars.EDGE_VERIFY_THRESHOLD }}
```

Leave the repository variable unset (or set to `0`) to keep the flag OFF in
production while testing locally with it ON.
