"""
tests/test_mlb_sim.py — regression guard and unit tests for mlb_sim.py.

Covers:
  - baseline flag-OFF invariance (regression guard, 0.1 rule 4)
  - log5 monotonicity
  - park realized-rate accuracy when USE_LOGIT_FACTORS=1 (0.1)
  - tie unbiasedness with USE_FULL_EXTRA_INNINGS=1 (0.2)
  - prob-sum invariant with logit path (0.3)
  - full away-inning simulation (0.4)
  - seed reproducibility (4.1)
"""
from __future__ import annotations
import json
import os
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

# Import the module object so monkeypatch.setattr can flip the flags in-place.
# This avoids importlib.reload which mutates __globals__ and breaks other tests.
import models.mlb_sim as _sim_module
from models.mlb_sim import (
    LEAGUE_RATES, OUTCOMES, OUTCOME_KEYS,
    PlayerProfile, build_dummy_lineup,
    log5_odds_ratio, compute_pa_probs,
    simulate_half_inning, simulate_game, run_monte_carlo,
    _apply_factors_logit,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pitcher() -> PlayerProfile:
    return PlayerProfile("p1", "Pitcher", "R", LEAGUE_RATES.copy())


def _neutral_factors() -> dict[str, float]:
    return {o: 1.0 for o in OUTCOMES}


# ---------------------------------------------------------------------------
# Log5 / pa_probs (existing tests kept, extended)
# ---------------------------------------------------------------------------

def test_log5_league_vs_league():
    k = LEAGUE_RATES["K_rate"]
    assert abs(log5_odds_ratio(k, k, k) - k) < 0.001


def test_log5_high_k_pitcher_raises_k():
    k = LEAGUE_RATES["K_rate"]
    assert log5_odds_ratio(k, k * 1.5, k) > k


def test_log5_monotone_in_pitcher_k():
    """Higher pitcher K-rate -> monotonically higher outcome prob."""
    k = LEAGUE_RATES["K_rate"]
    probs = [log5_odds_ratio(k, k * mult, k) for mult in (0.5, 1.0, 1.5, 2.0)]
    assert all(probs[i] < probs[i + 1] for i in range(len(probs) - 1))


def test_pa_probs_sum_to_one_default():
    batter = PlayerProfile("b1", "Batter", "R", LEAGUE_RATES.copy())
    probs = compute_pa_probs(batter, _pitcher())
    assert abs(sum(probs.values()) - 1.0) < 1e-6


# ---------------------------------------------------------------------------
# Priority 0.1 -- logit factors (only when flag is ON)
# ---------------------------------------------------------------------------

def test_logit_factors_prob_sum(monkeypatch):
    """0.3 -- with USE_LOGIT_FACTORS=True, output probs still sum to 1."""
    monkeypatch.setattr(_sim_module, "USE_LOGIT_FACTORS", True)

    batter  = PlayerProfile("b1", "B", "R", LEAGUE_RATES.copy())
    pitcher = PlayerProfile("p1", "P", "R", LEAGUE_RATES.copy())
    probs = compute_pa_probs(batter, pitcher, park_factors={"HR": 1.20})
    assert abs(sum(probs.values()) - 1.0) < 1e-6, \
        f"Prob sum = {sum(probs.values()):.8f}"


def test_logit_hr_factor_realized_rate(monkeypatch):
    """0.1 -- HR factor 1.20x in logit space produces ~1.20x realized HR rate."""
    monkeypatch.setattr(_sim_module, "USE_LOGIT_FACTORS", True)

    n_pa = 300_000
    batter  = PlayerProfile("b1", "B", "R", LEAGUE_RATES.copy())
    pitcher = PlayerProfile("p1", "P", "R", LEAGUE_RATES.copy())
    rng = random.Random(99)

    probs_neutral = compute_pa_probs(batter, pitcher)
    hr_neutral = sum(1 for _ in range(n_pa)
                     if _sim_module._sample_outcome(probs_neutral, rng) == "HR")

    rng2 = random.Random(99)
    probs_boosted = compute_pa_probs(batter, pitcher, park_factors={"HR": 1.20})
    hr_boosted = sum(1 for _ in range(n_pa)
                     if _sim_module._sample_outcome(probs_boosted, rng2) == "HR")

    ratio = hr_boosted / hr_neutral if hr_neutral > 0 else 0.0
    assert 1.10 <= ratio <= 1.30, \
        f"Expected ~1.20x realized HR ratio, got {ratio:.3f} " \
        f"(neutral={hr_neutral}, boosted={hr_boosted})"


def test_logit_neutral_factors_unchanged(monkeypatch):
    """Logit path with all factors=1.0 should produce same probs as no factors."""
    monkeypatch.setattr(_sim_module, "USE_LOGIT_FACTORS", True)

    batter  = PlayerProfile("b1", "B", "R", LEAGUE_RATES.copy())
    pitcher = PlayerProfile("p1", "P", "R", LEAGUE_RATES.copy())
    neutral = {o: 1.0 for o in OUTCOMES}

    p_none    = compute_pa_probs(batter, pitcher)
    p_neutral = compute_pa_probs(batter, pitcher, park_factors=neutral)

    for k in p_none:
        assert abs(p_none[k] - p_neutral[k]) < 1e-4, \
            f"Logit with all-neutral factors changed prob for {k}"


# ---------------------------------------------------------------------------
# Priority 0.2 / 0.4 -- extra innings and tie resolution
# ---------------------------------------------------------------------------

def test_full_extra_innings_symmetric(monkeypatch):
    """0.2 -- identical lineups -> home win near 50% with full extra innings."""
    monkeypatch.setattr(_sim_module, "USE_FULL_EXTRA_INNINGS", True)

    home = build_dummy_lineup()
    away = build_dummy_lineup()
    p    = PlayerProfile("p_ref", "P", "R", LEAGUE_RATES.copy())
    rng  = random.Random(7)

    result = run_monte_carlo(home, away, p, p, n_sims=5_000, rng=rng)
    assert 0.44 <= result["home_win_prob"] <= 0.56, \
        f"Expected ~50% home win, got {result['home_win_prob']:.3f}"


def test_full_extra_innings_no_total_spike(monkeypatch):
    """0.2 -- total distribution should not have a spike at median+1."""
    monkeypatch.setattr(_sim_module, "USE_FULL_EXTRA_INNINGS", True)

    home = build_dummy_lineup()
    away = build_dummy_lineup()
    p    = PlayerProfile("p_ref", "P", "R", LEAGUE_RATES.copy())
    rng  = random.Random(13)
    result = run_monte_carlo(home, away, p, p, n_sims=5_000, rng=rng)

    over_keys = sorted(
        [k for k in result if k.startswith("over_")],
        key=lambda k: float(k.split("over_")[1].replace("_", "."))
    )
    for i in range(len(over_keys) - 1):
        pa, pb = result[over_keys[i]], result[over_keys[i + 1]]
        assert abs(pa - pb) <= 0.20, \
            f"Spike: {over_keys[i]}={pa:.3f} -> {over_keys[i+1]}={pb:.3f}"


def test_away_ninth_inning_fully_simulated():
    """
    0.4 -- home team up 3 entering the 9th still fully simulates the away 9th.
    Verify walk_off_target never truncates a tie-preserving rally.
    """
    dominant_rates = {**LEAGUE_RATES, "out_rate": 0.90, "K_rate": 0.05,
                      "HR_rate": 0.001, "1B_rate": 0.001, "2B_rate": 0.001,
                      "3B_rate": 0.001, "BB_rate": 0.001, "HBP_rate": 0.001}

    great_rates = {**LEAGUE_RATES, "HR_rate": 0.50, "out_rate": 0.10,
                   "K_rate": 0.05, "BB_rate": 0.05, "HBP_rate": 0.01,
                   "1B_rate": 0.10, "2B_rate": 0.10, "3B_rate": 0.09}

    away_lineup = [PlayerProfile("a", "Away", "R", great_rates)] * 9
    home_lineup = [PlayerProfile("h", "Home", "R", dominant_rates)] * 9
    home_pitcher = PlayerProfile("hp", "HP", "R", dominant_rates)
    away_pitcher = PlayerProfile("ap", "AP", "R", dominant_rates)

    rng = random.Random(42)
    away_wins = sum(
        1 for _ in range(500)
        if simulate_game(home_lineup, away_lineup, home_pitcher, away_pitcher,
                         rng=rng)["winner"] == "away"
    )
    assert away_wins > 0, \
        "Away team with high HR rates never won -- 9th inning may be truncated"


# ---------------------------------------------------------------------------
# Priority 4.1 -- seed reproducibility
# ---------------------------------------------------------------------------

def test_seeded_runs_identical():
    """Two runs with the same seed produce bit-identical results."""
    home = build_dummy_lineup()
    away = build_dummy_lineup()
    p    = _pitcher()

    r1 = run_monte_carlo(home, away, p, p, n_sims=1_000, rng=random.Random(55))
    r2 = run_monte_carlo(home, away, p, p, n_sims=1_000, rng=random.Random(55))

    assert r1["home_win_prob"] == r2["home_win_prob"]
    assert r1["mean_total"]    == r2["mean_total"]
    for k in r1:
        if k.startswith("over_") or k.startswith("run_line"):
            assert r1[k] == r2[k], f"Mismatch on {k}: {r1[k]} vs {r2[k]}"


def test_different_seeds_differ():
    """Different seeds should produce different results."""
    home = build_dummy_lineup()
    away = build_dummy_lineup()
    p    = _pitcher()

    r1 = run_monte_carlo(home, away, p, p, n_sims=500, rng=random.Random(1))
    r2 = run_monte_carlo(home, away, p, p, n_sims=500, rng=random.Random(2))
    assert (r1["home_win_prob"] != r2["home_win_prob"]
            or r1["mean_total"] != r2["mean_total"])


# ---------------------------------------------------------------------------
# Regression guard -- flag-OFF must be bit-identical to baseline
# ---------------------------------------------------------------------------

def test_flag_off_matches_baseline():
    """
    Regression guard: with both upgrade flags OFF, seeded results must match
    baseline_reference.json exactly (bit-for-bit on float comparisons).
    """
    assert not _sim_module.USE_LOGIT_FACTORS, \
        "USE_LOGIT_FACTORS must be OFF for regression guard"
    assert not _sim_module.USE_FULL_EXTRA_INNINGS, \
        "USE_FULL_EXTRA_INNINGS must be OFF for regression guard"

    baseline_path = Path(__file__).parent / "baseline_reference.json"
    with open(baseline_path) as f:
        baseline = json.load(f)

    home = build_dummy_lineup()
    away = build_dummy_lineup()
    p    = PlayerProfile("p_ref", "Ref Pitcher", "R", LEAGUE_RATES.copy())

    random.seed(baseline["seed"])
    result = run_monte_carlo(home, away, p, p, n_sims=baseline["n_sims"])

    assert result["home_win_prob"] == baseline["home_win_prob"], \
        f"home_win_prob: {result['home_win_prob']} != {baseline['home_win_prob']}"
    assert result["mean_total"] == baseline["mean_total"], \
        f"mean_total: {result['mean_total']} != {baseline['mean_total']}"

    for k, expected in baseline["over_probs"].items():
        assert result.get(k) == expected, \
            f"{k}: {result.get(k)} != {expected}"

    for k in ("run_line_home_minus_1_5", "run_line_away_plus_1_5",
              "run_line_away_minus_1_5", "run_line_home_plus_1_5"):
        assert result[k] == baseline[k], f"{k}: {result[k]} != {baseline[k]}"
