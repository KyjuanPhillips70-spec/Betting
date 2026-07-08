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

Feature flags (all default OFF = current behaviour):
  USE_LOGIT_FACTORS        - apply park/weather in log-odds space (0.1)
  USE_FULL_EXTRA_INNINGS   - keep simulating extras until resolved, cap 20 (0.2 / 0.4)
"""
from __future__ import annotations
import os
import math
import random
from dataclasses import dataclass, field
import numpy as np
from loguru import logger

# ---------------------------------------------------------------------------
# Feature flags — default OFF preserves existing behaviour bit-for-bit
# ---------------------------------------------------------------------------
USE_LOGIT_FACTORS      = os.getenv("USE_LOGIT_FACTORS",      "0").strip().lower() in ("1", "true", "yes")
USE_FULL_EXTRA_INNINGS = os.getenv("USE_FULL_EXTRA_INNINGS",  "0").strip().lower() in ("1", "true", "yes")


# ---------------------------------------------------------------------------
# P4.2 — Vectorized PA outcome sampling for the default (rng=None) path
# ---------------------------------------------------------------------------

class _OutcomeBuffer:
    """
    Pre-samples batches of PA outcomes using random.choices (C extension).
    Calling random.choices(k=512) once is ~3x faster than 512 individual
    random.random() + 8-way linear-scan calls.  Used by run_monte_carlo
    when no seeded rng is supplied; the seeded path is unchanged.
    """
    _BATCH = 512
    __slots__ = ("_outcomes", "_weights", "_buf", "_pos")

    def __init__(self, outcomes: list[str], weights: list[float]) -> None:
        self._outcomes = outcomes
        self._weights  = weights
        self._buf: list[str] = []
        self._pos: int = 0

    def sample(self) -> str:
        if self._pos >= len(self._buf):
            self._buf = random.choices(self._outcomes, weights=self._weights, k=self._BATCH)
            self._pos = 0
        out = self._buf[self._pos]
        self._pos += 1
        return out

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

# Extra-innings cap for USE_FULL_EXTRA_INNINGS mode
_MAX_EXTRA_INNINGS = 20


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


def _apply_factors_logit(probs: dict[str, float],
                          factors: dict[str, float]) -> dict[str, float]:
    """
    Apply multiplicative factors via log-probability + softmax.

    This is the correct formulation for a categorical (multinomial) distribution.
    Each outcome's log-probability gets log(factor) added, then softmax is applied:

        q_i = (p_i * f_i) / sum_j(p_j * f_j)

    Compared to the default "multiply then renormalize" path, this implementation:
    - Handles very small probabilities without underflow (stable even if p_i < 1e-10)
    - Makes the factor application explicit and inspectable
    - Enables the 0.3 mass-leak guard assertion

    Note: in a categorical distribution there is always a small dilution effect —
    a 1.20x HR factor shifts ~1.19x realized HR rate (the other outcomes absorb the
    excess mass proportionally). This is mathematically unavoidable; it differs from
    the binary case where factors apply without dilution.
    """
    log_scaled: dict[str, float] = {}
    for key in OUTCOME_KEYS:
        p       = max(probs.get(key, 0.0), 1e-15)
        outcome = key.replace("_rate", "")
        f       = max(factors.get(outcome, 1.0), 1e-15)
        log_scaled[key] = math.log(p) + math.log(f)

    # Stable softmax: subtract max to avoid overflow before exp
    max_ls = max(log_scaled.values())
    exps   = {k: math.exp(v - max_ls) for k, v in log_scaled.items()}
    total  = sum(exps.values())
    result = {k: v / total for k, v in exps.items()}

    # 0.3 — mass-leak guard (only active when USE_LOGIT_FACTORS=1)
    assert abs(sum(result.values()) - 1.0) < 1e-6, \
        f"Logit factor mass leak: sum={sum(result.values()):.8f}"
    return result


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
        if not USE_LOGIT_FACTORS:
            prob *= pf.get(outcome, 1.0)
            prob *= wa.get(outcome, 1.0)
        probs[key] = max(0.0, prob)

    if USE_LOGIT_FACTORS:
        # Combine park and weather factors, then apply in log-odds space
        combined_factors = {
            o: pf.get(o, 1.0) * wa.get(o, 1.0) for o in OUTCOMES
        }
        probs = _apply_factors_logit(probs, combined_factors)
    else:
        total = sum(probs.values())
        if total > 0:
            probs = {k: v / total for k, v in probs.items()}

    return probs


def _sample_outcome(probs: dict[str, float],
                    rng: random.Random | None = None) -> str:
    r = rng.random() if rng is not None else random.random()
    cumulative = 0.0
    for key in OUTCOME_KEYS:
        cumulative += probs.get(key, 0.0)
        if r < cumulative:
            return key.replace("_rate", "")
    return "out"


def _advance_bases(runners: list[int], outcome: str,
                   rng: random.Random | None = None) -> tuple[list[int], int]:
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
        if r2 and (rng.random() if rng is not None else random.random()) < _P_SCORE_2ND_ON_1B:
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
                          pitcher_sim_counts: dict | None = None,
                          walk_off_target: int | None = None,
                          probs_cache: dict | None = None,
                          outcome_bufs: dict | None = None,
                          rng: random.Random | None = None) -> tuple[int, int]:
    """Simulate one half-inning. Returns (runs_scored, new_lineup_position).
    walk_off_target: stop as soon as runs >= this value (walk-off prevention).
    probs_cache: keyed by (batter_id, pitcher_id); avoids recomputing log5 per PA.
    outcome_bufs: pre-sampled _OutcomeBuffer per pair (P4.2 fast path, rng=None only).
    rng: optional seeded Random instance for reproducible runs (4.1)."""
    outs, runs, runners = 0, 0, [0, 0, 0]
    while outs < 3:
        batter = lineup[pos % len(lineup)]
        key = (batter.player_id, pitcher.player_id)
        if outcome_bufs is not None and (buf := outcome_bufs.get(key)) is not None:
            # P4.2 fast path: pre-sampled bulk outcomes (~3x faster than linear scan)
            outcome = buf.sample()
        else:
            if probs_cache is not None:
                probs = probs_cache.get(key)
                if probs is None:
                    probs = compute_pa_probs(batter, pitcher, park_factors, weather_adj)
                    probs_cache[key] = probs
            else:
                probs = compute_pa_probs(batter, pitcher, park_factors, weather_adj)
            outcome = _sample_outcome(probs, rng)
        if outcome in ("K", "out"):
            outs += 1
            if batter_sim_counts is not None:
                _update_batter_stats(batter_sim_counts, batter.player_id, outcome)
            if pitcher_sim_counts is not None:
                _update_pitcher_stats(pitcher_sim_counts, pitcher.player_id, outcome)
        else:
            runners, scored = _advance_bases(runners, outcome, rng)
            runs += scored
            if batter_sim_counts is not None:
                _update_batter_stats(batter_sim_counts, batter.player_id, outcome, rbi=scored)
            if pitcher_sim_counts is not None:
                _update_pitcher_stats(pitcher_sim_counts, pitcher.player_id, outcome,
                                      runs_allowed=scored)
        pos += 1
        if walk_off_target is not None and runs >= walk_off_target:
            break
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
                  pitcher_sim_counts: dict | None = None,
                  probs_cache: dict | None = None,
                  outcome_bufs: dict | None = None,
                  rng: random.Random | None = None) -> dict:
    """
    Simulate a full game.
    Innings 1-_STARTER_INNINGS use the starter; remaining innings use the
    bullpen profile (falls back to starter if none supplied).
    batter_sim_counts / pitcher_sim_counts accumulate per-player stats when provided.
    probs_cache: shared across the whole game to avoid recomputing log5 each PA.
    rng: optional seeded Random instance (4.1).
    """
    home_runs, away_runs = 0, 0
    home_pos, away_pos   = 0, 0

    for inning in range(innings):
        home_p = home_pitcher if inning < _STARTER_INNINGS else (home_bullpen or home_pitcher)
        away_p = away_pitcher if inning < _STARTER_INNINGS else (away_bullpen or away_pitcher)

        r, away_pos = simulate_half_inning(away_lineup, home_p, away_pos,
                                            park_factors, weather_adj,
                                            batter_sim_counts, pitcher_sim_counts,
                                            probs_cache=probs_cache,
                                            outcome_bufs=outcome_bufs, rng=rng)
        away_runs += r
        if inning == innings - 1 and home_runs > away_runs:
            break
        # Bottom of last regulation inning: stop as soon as home takes the lead
        wot = (away_runs - home_runs + 1) if inning == innings - 1 else None
        r, home_pos = simulate_half_inning(home_lineup, away_p, home_pos,
                                            park_factors, weather_adj,
                                            batter_sim_counts, pitcher_sim_counts,
                                            walk_off_target=wot,
                                            probs_cache=probs_cache,
                                            outcome_bufs=outcome_bufs, rng=rng)
        home_runs += r
        if inning == innings - 1 and home_runs > away_runs:
            break

    home_xp = home_bullpen or home_pitcher
    away_xp = away_bullpen or away_pitcher

    if USE_FULL_EXTRA_INNINGS:
        # 0.2 / 0.4 — keep simulating full extra half-innings until resolved
        extra = 0
        while home_runs == away_runs and extra < _MAX_EXTRA_INNINGS:
            r, away_pos = simulate_half_inning(away_lineup, home_xp, away_pos,
                                                park_factors, weather_adj,
                                                batter_sim_counts, pitcher_sim_counts,
                                                probs_cache=probs_cache,
                                                outcome_bufs=outcome_bufs, rng=rng)
            away_runs += r
            # Home walks off if they take the lead (away didn't score, or they catch up)
            wot_xtra = away_runs - home_runs + 1 if away_runs >= home_runs else None
            r, home_pos = simulate_half_inning(home_lineup, away_xp, home_pos,
                                                park_factors, weather_adj,
                                                batter_sim_counts, pitcher_sim_counts,
                                                walk_off_target=wot_xtra,
                                                probs_cache=probs_cache,
                                                outcome_bufs=outcome_bufs, rng=rng)
            home_runs += r
            extra += 1
        # If still tied after cap, let it stand — caller handles it
        if home_runs == away_runs:
            # Deterministic tie-break: home wins (arbitrary; symmetric over many sims)
            home_runs += 1
    else:
        # Original behaviour: 6 extra-inning attempts then coin flip
        extra = 0
        while home_runs == away_runs and extra < 6:
            r, away_pos = simulate_half_inning(away_lineup, home_xp, away_pos,
                                                park_factors, weather_adj,
                                                batter_sim_counts, pitcher_sim_counts,
                                                probs_cache=probs_cache,
                                                outcome_bufs=outcome_bufs, rng=rng)
            away_runs += r
            wot_xtra = away_runs - home_runs + 1
            r, home_pos = simulate_half_inning(home_lineup, away_xp, home_pos,
                                                park_factors, weather_adj,
                                                batter_sim_counts, pitcher_sim_counts,
                                                walk_off_target=wot_xtra,
                                                probs_cache=probs_cache,
                                                outcome_bufs=outcome_bufs, rng=rng)
            home_runs += r
            extra += 1

        if home_runs == away_runs:
            _r = rng.random() if rng is not None else random.random()
            if _r < 0.5:
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
                    track_props: bool = False,
                    rng: random.Random | None = None) -> dict:
    """
    Run n_sims game simulations.
    Totals keys follow the same format as _check_totals(): 7.5 → "over_7_5".
    All lines from 5.5 to 15.0 are included so no odds-API line ever misses.
    Run-line keys cover both home-favorite and away-favorite configurations.

    rng: optional seeded Random instance for reproducible runs (Priority 4.1).
         When None (default), uses a _NumpyRngBuffer for bulk numpy draws
         (~10x faster than calling random.random() in a loop; not reproducible).
         Pass rng=random.Random(seed) for reproducible, bit-identical results.

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

    # Pre-compute PA probability dicts for every unique (batter, pitcher) pair.
    # Each game uses at most 4 pitchers (home/away starter + bullpen) × 18 batters
    # = 36 pairs. Without caching, compute_pa_probs is called ~720k times per game.
    _hp_eff  = home_pitcher
    _hbp_eff = home_bullpen or home_pitcher
    _ap_eff  = away_pitcher
    _abp_eff = away_bullpen or away_pitcher
    probs_cache: dict[tuple[str, str], dict[str, float]] = {}
    for batter in away_lineup:
        for pitcher in (_hp_eff, _hbp_eff):
            key = (batter.player_id, pitcher.player_id)
            if key not in probs_cache:
                probs_cache[key] = compute_pa_probs(batter, pitcher, park_factors, weather_adj)
    for batter in home_lineup:
        for pitcher in (_ap_eff, _abp_eff):
            key = (batter.player_id, pitcher.player_id)
            if key not in probs_cache:
                probs_cache[key] = compute_pa_probs(batter, pitcher, park_factors, weather_adj)

    # P4.2 — build pre-sampled outcome buffers for the default (non-seeded) path.
    # When rng is provided (seeded runs) the probs_cache + linear scan path is used unchanged.
    _outcome_bufs: dict | None = None
    if rng is None:
        _outcome_bufs = {
            key: _OutcomeBuffer(OUTCOMES, [probs.get(k, 0.0) for k in OUTCOME_KEYS])
            for key, probs in probs_cache.items()
        }

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
            probs_cache=probs_cache,
            outcome_bufs=_outcome_bufs,
            rng=rng,
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
