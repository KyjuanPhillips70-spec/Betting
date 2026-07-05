"""
MLB Monte Carlo simulator.
Simulates games plate-appearance by plate-appearance using the log5 / odds-ratio
method to combine batter and pitcher rates, then applies park + weather factors.

Pitching model:
  Innings 1-5  → starting pitcher profile
  Innings 6-9  → bullpen profile (team pitching aggregate)
  Extra innings → bullpen profile
If no bullpen profile is supplied the starter continues (backward-compatible).

Prop tracking (track_props=True):
  Accumulates per-player hit/TB/HR/RBI/BB/K counts for batters and
  K/outs/hits-allowed/BB/ER counts for pitchers across all simulations.
  Returns prop_distributions and player_names in the output dict so
  edge.edge.find_player_prop_edges() can compare against sportsbook lines.
"""
from __future__ import annotations
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

# All MLB totals lines we cover: 5.5, 6.0, 6.5, ..., 15.0
_MLB_TOTAL_LINES: list[float] = [x / 2 for x in range(11, 31)]

# Approximate inning when a starter is replaced by the bullpen
_STARTER_INNINGS = 5

# Probability a runner on 2nd scores on a single (league average)
_P_SCORE_2ND_ON_1B = 0.65


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
        if r2 and random.random() < _P_SCORE_2ND_ON_1B:
            runs += 1
            return [1, r1, 0], runs
        else:
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


# ---------------------------------------------------------------------------
# Per-player stat tracking helpers (used when track_props=True)
# ---------------------------------------------------------------------------

_BATTER_STAT_KEYS  = ("hits", "tb", "hr", "rbi", "bb", "k")
_PITCHER_STAT_KEYS = ("k", "outs", "hits", "bb", "er")


def _update_batter_stats(sim_counts: dict, player_id: str,
                          outcome: str, rbi: int = 0) -> None:
    if player_id not in sim_counts:
        sim_counts[player_id] = {s: 0 for s in _BATTER_STAT_KEYS}
    c = sim_counts[player_id]
    if outcome == "1B":
        c["hits"] += 1; c["tb"] += 1; c["rbi"] += rbi
    elif outcome == "2B":
        c["hits"] += 1; c["tb"] += 2; c["rbi"] += rbi
    elif outcome == "3B":
        c["hits"] += 1; c["tb"] += 3; c["rbi"] += rbi
    elif outcome == "HR":
        c["hits"] += 1; c["tb"] += 4; c["hr"] += 1; c["rbi"] += rbi
    elif outcome == "BB":
        c["bb"] += 1; c["rbi"] += rbi
    elif outcome == "K":
        c["k"] += 1
    else:  # groundout, flyout, etc. can still drive in runs
        c["rbi"] += rbi


def _update_pitcher_stats(sim_counts: dict, player_id: str,
                           outcome: str, runs_allowed: int = 0) -> None:
    if player_id not in sim_counts:
        sim_counts[player_id] = {s: 0 for s in _PITCHER_STAT_KEYS}
    c = sim_counts[player_id]
    if outcome == "K":
        c["k"] += 1; c["outs"] += 1
    elif outcome == "out":
        c["outs"] += 1
    elif outcome in ("1B", "2B", "3B", "HR"):
        c["hits"] += 1
    elif outcome in ("BB", "HBP"):
        c["bb"] += 1
    c["er"] += runs_allowed


# ---------------------------------------------------------------------------
# Simulation core
# ---------------------------------------------------------------------------

def simulate_half_inning(lineup: list[PlayerProfile], pitcher: PlayerProfile,
                          pos: int, park_factors: dict | None = None,
                          weather_adj: dict | None = None,
                          batter_sim_counts: dict | None = None,
                          pitcher_sim_counts: dict | None = None) -> tuple[int, int]:
    """Simulate one half-inning. Returns (runs_scored, new_lineup_position)."""
    outs, runs, runners = 0, 0, [0, 0, 0]
    while outs < 3:
        batter  = lineup[pos % len(lineup)]
        probs   = compute_pa_probs(batter, pitcher, park_factors, weather_adj)
        outcome = _sample_outcome(probs)
        if outcome in ("K", "out"):
            outs += 1
            if batter_sim_counts is not None:
                _update_batter_stats(batter_sim_counts, batter.player_id, outcome)
            if pitcher_sim_counts is not None:
                _update_pitcher_stats(pitcher_sim_counts, pitcher.player_id, outcome)
        else:
            runners, scored = _advance_bases(runners, outcome)
            runs += scored
            if batter_sim_counts is not None:
                _update_batter_stats(batter_sim_counts, batter.player_id, outcome, rbi=scored)
            if pitcher_sim_counts is not None:
                _update_pitcher_stats(pitcher_sim_counts, pitcher.player_id, outcome,
                                      runs_allowed=scored)
        pos += 1
    return runs, pos % len(lineup)


def simulate_game(home_lineup: list[PlayerProfile],
                  away_lineup: list[PlayerProfile],
                  home_pitcher: PlayerProfile,
                  away_pitcher: PlayerProfile,
                  park_factors: dict | None = None,
                  weather_adj: dict | None = None,
                  innings: int = 9,
                  home_bullpen: PlayerProfile | None = None,
                  away_bullpen: PlayerProfile | None = None,
                  batter_sim_counts: dict | None = None,
                  pitcher_sim_counts: dict | None = None) -> dict:
    """
    Simulate a full game.
    Innings 1-_STARTER_INNINGS use the starter; remaining innings use the
    bullpen profile (falls back to starter if none supplied).
    batter_sim_counts / pitcher_sim_counts accumulate per-player stats when provided.
    """
    home_runs, away_runs = 0, 0
    home_pos, away_pos   = 0, 0

    for inning in range(innings):
        home_p = home_pitcher if inning < _STARTER_INNINGS else (home_bullpen or home_pitcher)
        away_p = away_pitcher if inning < _STARTER_INNINGS else (away_bullpen or away_pitcher)

        r, away_pos = simulate_half_inning(away_lineup, home_p, away_pos,
                                            park_factors, weather_adj,
                                            batter_sim_counts, pitcher_sim_counts)
        away_runs += r
        if inning == innings - 1 and home_runs > away_runs:
            break
        r, home_pos = simulate_half_inning(home_lineup, away_p, home_pos,
                                            park_factors, weather_adj,
                                            batter_sim_counts, pitcher_sim_counts)
        home_runs += r
        if inning == innings - 1 and home_runs > away_runs:
            break

    home_xp = home_bullpen or home_pitcher
    away_xp = away_bullpen or away_pitcher
    extra = 0
    while home_runs == away_runs and extra < 6:
        r, away_pos = simulate_half_inning(away_lineup, home_xp, away_pos,
                                            park_factors, weather_adj,
                                            batter_sim_counts, pitcher_sim_counts)
        away_runs += r
        r, home_pos = simulate_half_inning(home_lineup, away_xp, home_pos,
                                            park_factors, weather_adj,
                                            batter_sim_counts, pitcher_sim_counts)
        home_runs += r
        extra += 1

    if home_runs == away_runs:
        if random.random() < 0.5:
            home_runs += 1
        else:
            away_runs += 1

    return {
        "home_runs": home_runs,
        "away_runs": away_runs,
        "winner":    "home" if home_runs > away_runs else "away",
        "total":     home_runs + away_runs,
    }


def run_monte_carlo(home_lineup: list[PlayerProfile],
                    away_lineup: list[PlayerProfile],
                    home_pitcher: PlayerProfile,
                    away_pitcher: PlayerProfile,
                    park_factors: dict | None = None,
                    weather_adj: dict | None = None,
                    n_sims: int = 10_000,
                    home_bullpen: PlayerProfile | None = None,
                    away_bullpen: PlayerProfile | None = None,
                    track_props: bool = False) -> dict:
    """
    Run n_sims game simulations.
    Totals keys follow the same format as _check_totals(): 7.5 → "over_7_5".
    All lines from 5.5 to 15.0 are included so no odds-API line ever misses.
    Run-line keys cover both home-favorite and away-favorite configurations.

    When track_props=True the output also includes:
      prop_distributions: {player_id: {stat_key: np.ndarray(n_sims)}}
      player_names:       {player_id: full_name}
    """
    home_wins = 0
    totals: list[int] = []
    home_runs_list: list[int] = []
    away_runs_list: list[int] = []

    # Build player name registry once before the loop
    if track_props:
        player_names: dict[str, str] = {}
        for p in home_lineup + away_lineup + [home_pitcher, away_pitcher]:
            player_names[p.player_id] = p.name
        batter_dist: dict[str, dict[str, list]] = {}
        pitcher_dist: dict[str, dict[str, list]] = {}

    for _ in range(n_sims):
        b_counts: dict | None = {} if track_props else None
        p_counts: dict | None = {} if track_props else None

        result = simulate_game(
            home_lineup, away_lineup,
            home_pitcher, away_pitcher,
            park_factors, weather_adj,
            home_bullpen=home_bullpen,
            away_bullpen=away_bullpen,
            batter_sim_counts=b_counts,
            pitcher_sim_counts=p_counts,
        )
        if result["winner"] == "home":
            home_wins += 1
        totals.append(result["total"])
        home_runs_list.append(result["home_runs"])
        away_runs_list.append(result["away_runs"])

        if track_props:
            for pid, counts in b_counts.items():
                if pid not in batter_dist:
                    batter_dist[pid] = {s: [] for s in _BATTER_STAT_KEYS}
                for s in _BATTER_STAT_KEYS:
                    batter_dist[pid][s].append(counts.get(s, 0))
            for pid, counts in p_counts.items():
                if pid not in pitcher_dist:
                    pitcher_dist[pid] = {s: [] for s in _PITCHER_STAT_KEYS}
                for s in _PITCHER_STAT_KEYS:
                    pitcher_dist[pid][s].append(counts.get(s, 0))

    t = np.array(totals)
    h = np.array(home_runs_list)
    a = np.array(away_runs_list)

    over_probs = {
        f"over_{str(line).replace('.', '_')}": float((t > line).mean())
        for line in _MLB_TOTAL_LINES
    }

    result_dict: dict = {
        "home_win_prob":           home_wins / n_sims,
        "away_win_prob":           1 - home_wins / n_sims,
        "mean_total":              float(t.mean()),
        "mean_home_runs":          float(h.mean()),
        "mean_away_runs":          float(a.mean()),
        "n_sims":                  n_sims,
        "run_line_home_minus_1_5": float((h - a > 1.5).mean()),
        "run_line_away_plus_1_5":  float((a - h > -1.5).mean()),
        "run_line_away_minus_1_5": float((a - h > 1.5).mean()),
        "run_line_home_plus_1_5":  float((h - a > -1.5).mean()),
        **over_probs,
    }

    if track_props:
        prop_distributions: dict[str, dict[str, np.ndarray]] = {}
        for pid, dist in batter_dist.items():
            prop_distributions[pid] = {s: np.array(v) for s, v in dist.items()}
        for pid, dist in pitcher_dist.items():
            prop_distributions[pid] = {s: np.array(v) for s, v in dist.items()}
        result_dict["prop_distributions"] = prop_distributions
        result_dict["player_names"]        = player_names

    return result_dict


def build_dummy_lineup(n: int = 9) -> list[PlayerProfile]:
    """League-average lineup for testing / games with unknown lineups."""
    return [
        PlayerProfile(f"dummy_{i}", f"Player {i+1}", "R", LEAGUE_RATES.copy())
        for i in range(n)
    ]
