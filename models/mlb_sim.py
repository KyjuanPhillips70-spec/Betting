"""
MLB Monte Carlo simulator.
Simulates games plate-appearance by plate-appearance using the log5 / odds-ratio
method to combine batter and pitcher rates, then applies park + weather factors.
"""
from __future__ import annotations
import math
import random
from dataclasses import dataclass, field
import numpy as np
from loguru import logger

# League-average PA event rates (2023-24 approximations)
LEAGUE_RATES: dict[str, float] = {
    "K_rate":   0.224,
    "BB_rate":  0.085,
    "HBP_rate": 0.010,
    "1B_rate":  0.148,
    "2B_rate":  0.047,
    "3B_rate":  0.004,
    "HR_rate":  0.034,
    "out_rate": 0.448,
}

OUTCOMES     = ["K", "BB", "HBP", "1B", "2B", "3B", "HR", "out"]
OUTCOME_KEYS = [f"{o}_rate" for o in OUTCOMES]


@dataclass
class PlayerProfile:
    player_id: str
    name:      str
    hand:      str          # "L" or "R"
    rates:     dict[str, float] = field(default_factory=dict)


def log5_odds_ratio(batter_rate: float, pitcher_rate: float,
                    league_rate: float, eps: float = 1e-9) -> float:
    """
    Combine batter and pitcher rates via the odds-ratio (log5) method.
    P = (b*p/l) / (b*p/l + (1-b)*(1-p)/(1-l))
    """
    if league_rate <= 0 or league_rate >= 1:
        return batter_rate
    b, p, l = batter_rate, pitcher_rate, league_rate
    num = (b * p) / (l + eps)
    den = num + ((1 - b) * (1 - p)) / (1 - l + eps)
    return num / (den + eps)


def compute_pa_probs(batter: PlayerProfile, pitcher: PlayerProfile,
                     park_factors: dict[str, float] | None = None,
                     weather_adj: dict[str, float] | None = None) -> dict[str, float]:
    """Compute plate-appearance outcome probs via log5, then apply park/weather."""
    pf = park_factors or {}
    wa = weather_adj or {}
    probs: dict[str, float] = {}

    for outcome in OUTCOMES:
        key = f"{outcome}_rate"
        b = batter.rates.get(key, LEAGUE_RATES[key])
        p = pitcher.rates.get(key, LEAGUE_RATES[key])
        l = LEAGUE_RATES[key]
        prob = log5_odds_ratio(b, p, l)
        prob *= pf.get(outcome, 1.0)
        prob *= wa.get(outcome, 1.0)
        probs[key] = max(0.0, prob)

    total = sum(probs.values())
    if total > 0:
        probs = {k: v / total for k, v in probs.items()}
    return probs


def _sample_outcome(probs: dict[str, float]) -> str:
    r = random.random()
    cumulative = 0.0
    for key in OUTCOME_KEYS:
        cumulative += probs.get(key, 0.0)
        if r < cumulative:
            return key.replace("_rate", "")
    return "out"


def _advance_bases(runners: list[int], outcome: str) -> tuple[list[int], int]:
    """
    Simplified base advancement model.
    runners: [on_1st, on_2nd, on_3rd] (1 = runner present)
    Returns (new_runners, runs_scored).
    """
    runs = 0

    if outcome in ("K", "out"):
        return runners[:], 0

    if outcome in ("BB", "HBP"):
        # Force-advance only if bases are loaded in sequence
        r1, r2, r3 = runners
        if r1 and r2 and r3:
            runs = 1
            return [1, 1, 1], runs
        elif r1 and r2:
            return [1, 1, 1], 0
        elif r1:
            return [1, 1, r3], 0
        else:
            return [1, r2, r3], 0

    if outcome == "1B":
        r1, r2, r3 = runners
        runs += r3
        return [1, r1, r2], runs

    if outcome == "2B":
        r1, r2, r3 = runners
        runs += r2 + r3
        return [0, 1, r1], runs

    if outcome == "3B":
        r1, r2, r3 = runners
        runs += r1 + r2 + r3
        return [0, 0, 1], runs

    if outcome == "HR":
        runs = 1 + runners[0] + runners[1] + runners[2]
        return [0, 0, 0], runs

    return runners[:], 0


def simulate_half_inning(lineup: list[PlayerProfile], pitcher: PlayerProfile,
                          pos: int, park_factors: dict | None = None,
                          weather_adj: dict | None = None) -> tuple[int, int]:
    """Simulate one half-inning. Returns (runs_scored, new_lineup_position)."""
    outs, runs, runners = 0, 0, [0, 0, 0]
    while outs < 3:
        batter = lineup[pos % len(lineup)]
        probs  = compute_pa_probs(batter, pitcher, park_factors, weather_adj)
        outcome = _sample_outcome(probs)
        if outcome in ("K", "out"):
            outs += 1
        else:
            runners, scored = _advance_bases(runners, outcome)
            runs += scored
        pos += 1
    return runs, pos % len(lineup)


def simulate_game(home_lineup: list[PlayerProfile], away_lineup: list[PlayerProfile],
                   home_pitcher: PlayerProfile, away_pitcher: PlayerProfile,
                   park_factors: dict | None = None, weather_adj: dict | None = None,
                   innings: int = 9) -> dict:
    """Simulate a full game. Returns home_runs, away_runs, winner, total."""
    home_runs, away_runs = 0, 0
    home_pos, away_pos   = 0, 0

    for inning in range(innings):
        r, away_pos = simulate_half_inning(away_lineup, home_pitcher, away_pos,
                                            park_factors, weather_adj)
        away_runs += r
        # Walk-off: home already winning entering bottom of 9th
        if inning == innings - 1 and home_runs > away_runs:
            break
        r, home_pos = simulate_half_inning(home_lineup, away_pitcher, home_pos,
                                            park_factors, weather_adj)
        home_runs += r
        if inning == innings - 1 and home_runs > away_runs:
            break   # walk-off hit

    # Extra innings (runner-on-second rule omitted for simplicity)
    extra = 0
    while home_runs == away_runs and extra < 6:
        r, away_pos = simulate_half_inning(away_lineup, home_pitcher, away_pos,
                                            park_factors, weather_adj)
        away_runs += r
        r, home_pos = simulate_half_inning(home_lineup, away_pitcher, home_pos,
                                            park_factors, weather_adj)
        home_runs += r
        extra += 1

    return {
        "home_runs": home_runs,
        "away_runs": away_runs,
        "winner":    "home" if home_runs > away_runs else "away",
        "total":     home_runs + away_runs,
    }


def run_monte_carlo(home_lineup: list[PlayerProfile], away_lineup: list[PlayerProfile],
                    home_pitcher: PlayerProfile, away_pitcher: PlayerProfile,
                    park_factors: dict | None = None, weather_adj: dict | None = None,
                    n_sims: int = 10_000) -> dict:
    """
    Run n_sims game simulations. Returns win probabilities and run distribution stats.
    """
    home_wins = 0
    totals: list[int] = []
    home_runs_list: list[int] = []
    away_runs_list: list[int] = []

    for _ in range(n_sims):
        result = simulate_game(home_lineup, away_lineup, home_pitcher, away_pitcher,
                                park_factors, weather_adj)
        if result["winner"] == "home":
            home_wins += 1
        totals.append(result["total"])
        home_runs_list.append(result["home_runs"])
        away_runs_list.append(result["away_runs"])

    t = np.array(totals)
    h = np.array(home_runs_list)
    a = np.array(away_runs_list)

    return {
        "home_win_prob":             home_wins / n_sims,
        "away_win_prob":             1 - home_wins / n_sims,
        "mean_total":                float(t.mean()),
        "over_7_5":                  float((t > 7.5).mean()),
        "over_8_5":                  float((t > 8.5).mean()),
        "over_9_5":                  float((t > 9.5).mean()),
        "run_line_home_minus_1_5":   float((h - a > 1.5).mean()),
        "run_line_away_plus_1_5":    float((a - h > -1.5).mean()),
        "mean_home_runs":            float(h.mean()),
        "mean_away_runs":            float(a.mean()),
        "n_sims":                    n_sims,
    }


def build_dummy_lineup(n: int = 9) -> list[PlayerProfile]:
    """League-average lineup for testing / games with unknown lineups."""
    return [
        PlayerProfile(f"dummy_{i}", f"Player {i+1}", "R", LEAGUE_RATES.copy())
        for i in range(n)
    ]
