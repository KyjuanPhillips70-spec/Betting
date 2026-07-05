"""
Edge detection: compare model probabilities against de-vigged market lines.
Produces BetAlert objects for bets that clear the minimum-edge threshold.

Market blending: our model probability is blended with the market's devigged
fair probability before the edge is calculated. MODEL_WEIGHT=0.40 means our
model contributes 40% of the final probability, the market supplies the other
60%. This anchors picks to market reality — the model only overrides the
market when it has a strong, specific reason to disagree.

Practical effect: a raw model-vs-market disagreement of at least
  MIN_EDGE / MODEL_WEIGHT = 0.03 / 0.40 = 7.5%
is required before any pick surfaces, which eliminates noise from dummy
lineups and static pre-tournament soccer ratings.
"""
from __future__ import annotations
import difflib
import os
import numpy as np
from loguru import logger

from edge.odds_math import american_to_decimal, devig_two_way, devig_multi_way, expected_value
from edge.kelly import stake_units as kelly_units
from alerting.telegram_alerts import BetAlert

MIN_EDGE     = float(os.getenv("MIN_EDGE_THRESHOLD", "0.03"))
KELLY_FRAC   = float(os.getenv("MAX_KELLY_FRACTION", "0.20"))
BANKROLL_U   = float(os.getenv("BANKROLL_UNITS",     "100.0"))
MODEL_WEIGHT = float(os.getenv("MODEL_WEIGHT",       "0.40"))

_SANITY_DISAGREEMENT = 0.30

# Player prop market keys (Odds API) → sim distribution stat keys
BATTER_PROP_MARKETS: dict[str, str] = {
    "batter_hits":        "hits",
    "batter_total_bases": "tb",
    "batter_home_runs":   "hr",
    "batter_rbis":        "rbi",
    "batter_walks":       "bb",
    "batter_strikeouts":  "k",
}
PITCHER_PROP_MARKETS: dict[str, str] = {
    "pitcher_strikeouts":   "k",
    "pitcher_outs":         "outs",
    "pitcher_hits_allowed": "hits",
    "pitcher_walks":        "bb",
    "pitcher_earned_runs":  "er",
}


def _blend(model_p: float, fair_p: float) -> float:
    return MODEL_WEIGHT * model_p + (1.0 - MODEL_WEIGHT) * fair_p


def _make_alert(sport: str, event: str, market_label: str,
                model_p: float, fair_p: float,
                best_snap: dict) -> BetAlert | None:
    blended_p = _blend(model_p, fair_p)
    edge = blended_p - fair_p
    if edge < MIN_EDGE:
        return None

    raw_diff = abs(model_p - fair_p)
    if raw_diff > _SANITY_DISAGREEMENT:
        logger.warning(
            "Large model-market gap ({:.1%}) for {} | {} — verify inputs.",
            raw_diff, event, market_label
        )

    dec = american_to_decimal(best_snap["price"])
    units = kelly_units(blended_p, dec, BANKROLL_U, KELLY_FRAC)
    if units <= 0:
        logger.debug("Zero-stake bet suppressed: {} | {}", event, market_label)
        return None

    logger.info("Edge found: {} | {} | model={:.1%} fair={:.1%} edge={:.1%} units={:.2f}",
                event, market_label, model_p, fair_p, edge, units)
    return BetAlert(
        sport=sport,
        event=event,
        market=market_label,
        book=best_snap.get("book", "?"),
        line=f"{int(best_snap['price']):+d}",
        model_prob=blended_p,
        fair_prob=fair_p,
        stake_units=units,
    )


def _best_by_outcome(snapshots: list[dict], outcome_substr: str) -> dict | None:
    """Return the snapshot with the best (highest decimal) price for an outcome side."""
    candidates = [s for s in snapshots
                  if outcome_substr.lower() in s.get("outcome", "").lower()]
    if not candidates:
        return None
    return max(candidates, key=lambda s: american_to_decimal(s["price"]))


def find_mlb_edges(game: dict, sim: dict, snapshots: list[dict]) -> list[BetAlert]:
    """
    Compare MLB Monte Carlo results against sportsbook odds.
    game:      dict from mlb_statsapi.assemble_pregame_bundle
    sim:       dict from mlb_sim.run_monte_carlo
    snapshots: list of odds snapshots for this game
    """
    alerts: list[BetAlert] = []
    event = f"{game.get('away_team','?')} @ {game.get('home_team','?')}"

    h2h = [s for s in snapshots if s.get("market") == "h2h"]
    best_home = _best_by_outcome(h2h, game.get("home_team", "home"))
    best_away = _best_by_outcome(h2h, game.get("away_team", "away"))

    if best_home and best_away:
        fair_h, fair_a = devig_two_way(best_home["price"], best_away["price"])
        for label, model_p, fair_p, snap in [
            (f"{game.get('home_team','Home')} ML", sim["home_win_prob"], fair_h, best_home),
            (f"{game.get('away_team','Away')} ML", sim["away_win_prob"], fair_a, best_away),
        ]:
            a = _make_alert("MLB", event, label, model_p, fair_p, snap)
            if a:
                alerts.append(a)

    alerts.extend(_check_totals("MLB", event, snapshots, sim))
    alerts.extend(_check_run_line(event, snapshots, sim,
                                  game.get("home_team", "home"),
                                  game.get("away_team", "away")))
    return alerts


def _check_totals(sport: str, event: str, snapshots: list[dict],
                  sim: dict) -> list[BetAlert]:
    """Scan over/under markets for edge."""
    alerts: list[BetAlert] = []
    total_snaps = [s for s in snapshots if s.get("market") == "totals"]
    seen_lines: set[float] = set()

    for snap in total_snaps:
        line = snap.get("point")
        if line is None or line in seen_lines:
            continue
        seen_lines.add(line)

        line_snaps = [s for s in total_snaps if s.get("point") == line]
        best_over  = _best_by_outcome(line_snaps, "over")
        best_under = _best_by_outcome(line_snaps, "under")
        if not best_over or not best_under:
            continue

        key_over = f"over_{str(line).replace('.', '_')}"
        model_over = sim.get(key_over)
        if model_over is None:
            logger.debug("No model prob for {} total line {} ({}); skipping.",
                         sport, line, key_over)
            continue
        model_under = 1.0 - model_over
        fair_over, fair_under = devig_two_way(best_over["price"], best_under["price"])

        for side, model_p, fair_p, s in [
            (f"Over {line}",  model_over,  fair_over,  best_over),
            (f"Under {line}", model_under, fair_under, best_under),
        ]:
            a = _make_alert(sport, event, side, model_p, fair_p, s)
            if a:
                alerts.append(a)
    return alerts


def _check_run_line(event: str, snapshots: list[dict], sim: dict,
                    home_team: str, away_team: str) -> list[BetAlert]:
    """
    Detect +EV on the MLB run line (spread).
    Only ±1.5 is modeled in the Monte Carlo output; other alternate lines are
    silently skipped.
    """
    alerts: list[BetAlert] = []
    spread_snaps = [s for s in snapshots if s.get("market") == "spreads"]
    if not spread_snaps:
        return alerts

    abs_lines: set[float] = set()
    for s in spread_snaps:
        pt = s.get("point")
        if pt is not None:
            abs_lines.add(abs(pt))

    for line in abs_lines:
        if line != 1.5:
            continue

        # Case A: home at -1.5
        home_neg = [s for s in spread_snaps
                    if s.get("point") == -line
                    and home_team.lower() in s.get("outcome", "").lower()]
        away_pos  = [s for s in spread_snaps
                    if s.get("point") == line
                    and away_team.lower() in s.get("outcome", "").lower()]

        if home_neg and away_pos:
            bh = max(home_neg, key=lambda s: american_to_decimal(s["price"]))
            ba = max(away_pos,  key=lambda s: american_to_decimal(s["price"]))
            fh, fa = devig_two_way(bh["price"], ba["price"])
            for label, mp, fp, snap in [
                (f"{home_team} -{line:.1f}", sim.get("run_line_home_minus_1_5", 0.0), fh, bh),
                (f"{away_team} +{line:.1f}", sim.get("run_line_away_plus_1_5",  0.0), fa, ba),
            ]:
                a = _make_alert("MLB", event, label, mp, fp, snap)
                if a:
                    alerts.append(a)

        # Case B: away at -1.5
        away_neg = [s for s in spread_snaps
                    if s.get("point") == -line
                    and away_team.lower() in s.get("outcome", "").lower()]
        home_pos  = [s for s in spread_snaps
                    if s.get("point") == line
                    and home_team.lower() in s.get("outcome", "").lower()]

        if away_neg and home_pos:
            ba = max(away_neg, key=lambda s: american_to_decimal(s["price"]))
            bh = max(home_pos,  key=lambda s: american_to_decimal(s["price"]))
            fa, fh = devig_two_way(ba["price"], bh["price"])
            for label, mp, fp, snap in [
                (f"{away_team} -{line:.1f}", sim.get("run_line_away_minus_1_5", 0.0), fa, ba),
                (f"{home_team} +{line:.1f}", sim.get("run_line_home_plus_1_5",  0.0), fh, bh),
            ]:
                a = _make_alert("MLB", event, label, mp, fp, snap)
                if a:
                    alerts.append(a)

    return alerts


def find_soccer_edges(fixture: dict, model: dict, snapshots: list[dict]) -> list[BetAlert]:
    """
    Compare soccer model probabilities against sportsbook odds.
    fixture: dict with home_team, away_team
    model:   dict from soccer_model.predict (home_win, draw, away_win, over_*, btts)
    snapshots: list of odds snapshots for this game
    """
    alerts: list[BetAlert] = []
    event = f"{fixture.get('home_team','?')} vs {fixture.get('away_team','?')}"

    h2h = [s for s in snapshots if s.get("market") == "h2h"]
    bh = _best_by_outcome(h2h, fixture.get("home_team", "home"))
    bd = _best_by_outcome(h2h, "draw")
    ba = _best_by_outcome(h2h, fixture.get("away_team", "away"))

    if bh and bd and ba:
        fair_h, fair_d, fair_a = devig_multi_way([bh["price"], bd["price"], ba["price"]])
        for label, mp, fp, snap in [
            ("Home Win",  model.get("home_win", 0), fair_h, bh),
            ("Draw",      model.get("draw",     0), fair_d, bd),
            ("Away Win",  model.get("away_win", 0), fair_a, ba),
        ]:
            a = _make_alert("Soccer", event, label, mp, fp, snap)
            if a:
                alerts.append(a)

    alerts.extend(_check_totals("Soccer", event, snapshots, {
        "over_0_5": model.get("over_0_5", 0),
        "over_1_5": model.get("over_1_5", 0),
        "over_2_5": model.get("over_2_5", 0),
        "over_3_5": model.get("over_3_5", 0),
    }))

    btts_snaps = [s for s in snapshots if s.get("market") == "btts"]
    best_yes = _best_by_outcome(btts_snaps, "yes")
    best_no  = _best_by_outcome(btts_snaps, "no")
    if best_yes and best_no:
        fair_yes, fair_no = devig_two_way(best_yes["price"], best_no["price"])
        btts_p = model.get("btts", 0.0)
        for label, mp, fp, snap in [
            ("BTTS Yes", btts_p,         fair_yes, best_yes),
            ("BTTS No",  1.0 - btts_p,   fair_no,  best_no),
        ]:
            a = _make_alert("Soccer", event, label, mp, fp, snap)
            if a:
                alerts.append(a)

    return alerts


# ---------------------------------------------------------------------------
# Player prop edge detection
# ---------------------------------------------------------------------------

def _find_player_by_name(name: str, player_names: dict[str, str]) -> str | None:
    """Fuzzy-match a player name from the odds API to a sim player_id."""
    name_lo = name.lower()
    for pid, pname in player_names.items():
        if pname.lower() == name_lo:
            return pid
    names = list(player_names.values())
    pids  = list(player_names.keys())
    hits  = difflib.get_close_matches(name, names, n=1, cutoff=0.75)
    if hits:
        return pids[names.index(hits[0])]
    return None


def find_player_prop_edges(game: dict, sim: dict,
                            prop_snapshots: list[dict]) -> list[BetAlert]:
    """
    Find +EV bets across all player prop markets.
    sim must have been run with track_props=True so it contains
    'prop_distributions' (player_id → stat → np.ndarray) and
    'player_names' (player_id → full name).
    prop_snapshots should be filtered to batter_* / pitcher_* market keys.
    """
    alerts: list[BetAlert] = []
    distributions = sim.get("prop_distributions", {})
    player_names  = sim.get("player_names", {})
    if not distributions or not player_names:
        return alerts

    event = f"{game.get('away_team','?')} @ {game.get('home_team','?')}"
    all_prop_markets = {**BATTER_PROP_MARKETS, **PITCHER_PROP_MARKETS}

    for market_key, stat_key in all_prop_markets.items():
        market_snaps = [s for s in prop_snapshots if s.get("market") == market_key]
        if not market_snaps:
            continue

        # Group snapshots by player name stored in the description field
        player_snaps: dict[str, list] = {}
        for snap in market_snaps:
            desc = snap.get("description", "").strip()
            if desc:
                player_snaps.setdefault(desc, []).append(snap)

        for player_name, snaps in player_snaps.items():
            pid = _find_player_by_name(player_name, player_names)
            if pid is None:
                continue
            stat_dist = distributions.get(pid, {}).get(stat_key)
            if stat_dist is None or len(stat_dist) == 0:
                continue

            stat_arr = np.asarray(stat_dist)
            lines_seen: set[float] = set()
            for snap in snaps:
                pt = snap.get("point")
                if pt is not None:
                    lines_seen.add(float(pt))

            for line in lines_seen:
                line_snaps = [
                    s for s in snaps
                    if s.get("point") is not None
                    and abs(float(s["point"]) - line) < 1e-6
                ]
                best_over  = _best_by_outcome(line_snaps, "over")
                best_under = _best_by_outcome(line_snaps, "under")
                if not best_over or not best_under:
                    continue

                model_over  = float((stat_arr > line).mean())
                model_under = 1.0 - model_over
                fair_over, fair_under = devig_two_way(best_over["price"],
                                                       best_under["price"])
                mkt_label = market_key.replace("_", " ").title()
                for side, mp, fp, snap in [
                    (f"{player_name} {mkt_label} O{line}", model_over,  fair_over,  best_over),
                    (f"{player_name} {mkt_label} U{line}", model_under, fair_under, best_under),
                ]:
                    a = _make_alert("MLB", event, side, mp, fp, snap)
                    if a:
                        alerts.append(a)

    return alerts
