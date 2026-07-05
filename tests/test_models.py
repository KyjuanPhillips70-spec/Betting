"""Unit tests for simulation models."""
import pytest
from models.mlb_sim import (
    log5_odds_ratio, compute_pa_probs, simulate_half_inning,
    run_monte_carlo, build_dummy_lineup, PlayerProfile, LEAGUE_RATES,
)
from models.weather_adj import get_weather_adjustments
from config.park_factors import get_park_factors, park_factors_to_pa_adjustments


def _pitcher():
    return PlayerProfile("p1", "Pitcher", "R", LEAGUE_RATES.copy())


def test_log5_league_vs_league():
    k = LEAGUE_RATES["K_rate"]
    assert abs(log5_odds_ratio(k, k, k) - k) < 0.001


def test_log5_high_k_pitcher_raises_k():
    k = LEAGUE_RATES["K_rate"]
    assert log5_odds_ratio(k, k * 1.5, k) > k


def test_pa_probs_sum_to_one():
    batter = PlayerProfile("b1", "Batter", "R", LEAGUE_RATES.copy())
    probs = compute_pa_probs(batter, _pitcher())
    assert abs(sum(probs.values()) - 1.0) < 0.001


def test_half_inning_nonneg_runs():
    runs, pos = simulate_half_inning(build_dummy_lineup(), _pitcher(), 0)
    assert runs >= 0
    assert 0 <= pos < 9


def test_monte_carlo_basic():
    home = build_dummy_lineup()
    away = build_dummy_lineup()
    p = _pitcher()
    res = run_monte_carlo(home, away, p, p, n_sims=500)
    assert 0.0 <= res["home_win_prob"] <= 1.0
    assert res["mean_total"] > 0


def test_monte_carlo_symmetric():
    """Without park/weather edge, home win should be near 50% (±10%)."""
    home = build_dummy_lineup()
    away = build_dummy_lineup()
    p = _pitcher()
    res = run_monte_carlo(home, away, p, p, n_sims=2000)
    assert 0.38 <= res["home_win_prob"] <= 0.62


def test_dome_no_weather_effect():
    adj = get_weather_adjustments(40.0, 20.0, 180.0, 0.0, 50.0, is_dome=True)
    assert all(v == 1.0 for v in adj.values())


def test_coors_field_hr_factor():
    pf = get_park_factors("Coors Field")
    assert pf["HR_factor"] > 1.2


def test_unknown_park_neutral():
    pf = get_park_factors("Nonexistent Stadium XYZ")
    assert pf["HR_factor"] == 1.0
