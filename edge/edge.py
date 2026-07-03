"""
Edge detection: compare model probabilities against de-vigged market lines.
Produces BetAlert objects for bets that clear the minimum-edge threshold.
"""
from __future__ import annotations
import os
from loguru import logger

from edge.odds_math import american_to_decimal, devig_two_way, devig_multi_way, expected_value
from edge.kelly import stake_units as kelly_units
from alerting.telegram_alerts import BetAlert

MIN_EDGE      = float(os.getenv("MIN_EDGE_THRESHOLD", "0.02"))
KELLY_FRAC    = float(os.getenv("MAX_KELLY_FRACTION", "0.25"))
BANKROLL_U    = float(os.getenv("BANKROLL_UNITS", "100.0"))


def _make_alert(sport: str, event: str, market_label: str,
                model_p: float, fair_p: float,
                best_snap: dict) -> BetAlert | None:
    edge = model_p - fair_p
    if edge < MIN_EDGE:
        return None
    dec = american_to_decimal(best_snap["price"])
    units = kelly_units(model_p, dec, BANKROLL_U, KELLY_FRAC)
    logger.info("Edge found: {} | {} | edge={:.1%} units={:.2f}",
                event, market_label, edge, units)
    return BetAlert(
        sport=sport,
        event=event,
        market=market_label,
        book=best_snap.get("book", "?"),
        line=f"{int(best_snap['price']):+d}",
        model_prob=model_p,
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
    # Use actual team names: The Odds API outcome names are team names, not "home"/"away"
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

    # Totals
    alerts.extend(_check_totals("MLB", event, snapshots, sim))
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

        best_over  = _best_by_outcome(total_snaps, "over")
        best_under = _best_by_outcome(total_snaps, "under")
        if not best_over or not best_under:
            continue

        # Look for the model probability matching this line
        key_over = f"over_{str(line).replace('.', '_')}"
        model_over = sim.get(key_over)
        if model_over is None:
            continue
        model_under = 1.0 - model_over
        fair_over, fair_under = devig_two_way(best_over["price"], best_under["price"])

        for side, model_p, fair_p, snap in [
            (f"Over {line}",  model_over,  fair_over,  best_over),
            (f"Under {line}", model_under, fair_under, best_under),
        ]:
            a = _make_alert(sport, event, side, model_p, fair_p, snap)
            if a:
                alerts.append(a)
    return alerts


def find_soccer_edges(fixture: dict, model: dict, snapshots: list[dict]) -> list[BetAlert]:
    """
    Compare soccer model probabilities against sportsbook odds.
    fixture: dict with home_team, away_team
    model:   dict from soccer_model.predict (home_win, draw, away_win, over_2_5, over_1_5, btts)
    snapshots: list of odds snapshots for this game
    """
    alerts: list[BetAlert] = []
    event = f"{fixture.get('home_team','?')} vs {fixture.get('away_team','?')}"

    h2h = [s for s in snapshots if s.get("market") == "h2h"]
    # Use actual team names: The Odds API outcome names are team names and "Draw", not "home"/"away"
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
        "over_2_5": model.get("over_2_5", 0),
        "over_1_5": model.get("over_1_5", 0),
    }))
    return alerts
