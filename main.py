"""
Main entry point — orchestrates ingestion → simulation → edge → alerts.

Usage:
  python main.py                         # today's MLB card
  python main.py --sport soccer          # World Cup only
  python main.py --sport all             # MLB + World Cup
  python main.py --date 2026-07-04       # specific date
  python main.py --sims 5000             # faster (less accurate)
  python main.py --individual            # one Telegram message per bet
  python main.py --report                # backtest summary
  python main.py --init-db               # create/migrate the database
"""
from __future__ import annotations
import os
import difflib
import argparse
from datetime import date, datetime
from loguru import logger

from dotenv import load_dotenv
load_dotenv()

from storage.database import init_db, upsert_game, insert_bet_log
from ingestion.mlb_statsapi import assemble_pregame_bundle
from ingestion.weather import get_game_weather
from ingestion.odds import get_odds, parse_odds_to_snapshots
from models.mlb_sim import (
    run_monte_carlo, build_dummy_lineup, PlayerProfile, LEAGUE_RATES
)
from models.weather_adj import get_weather_adjustments
from config.park_factors import get_park_factors, park_factors_to_pa_adjustments
from config.stadiums import get_stadium
from edge.edge import find_mlb_edges
from alerting.telegram_alerts import TelegramAlerter, BetAlert


# ---------------------------------------------------------------------------
# Pre-tournament 2026 FIFA World Cup team ratings
# attack / defense are multipliers relative to an international league average
# of 1.35 goals per game per team.  Values derived from pre-tournament Elo.
# Used as fallback when ESPN group-stage standings aren’t yet populated.
# ---------------------------------------------------------------------------
_WC_RATINGS: dict[str, dict[str, float]] = {
    # Tier 1 — elite
    "Brazil":            {"attack": 1.55, "defense": 0.70},
    "France":            {"attack": 1.50, "defense": 0.72},
    "Argentina":         {"attack": 1.50, "defense": 0.72},
    "Spain":             {"attack": 1.45, "defense": 0.73},
    "Portugal":          {"attack": 1.45, "defense": 0.74},
    "Germany":           {"attack": 1.42, "defense": 0.76},
    "England":           {"attack": 1.40, "defense": 0.75},
    "Netherlands":       {"attack": 1.38, "defense": 0.78},
    # Tier 2 — strong
    "Belgium":           {"attack": 1.25, "defense": 0.85},
    "Italy":             {"attack": 1.22, "defense": 0.82},
    "Uruguay":           {"attack": 1.20, "defense": 0.83},
    "Colombia":          {"attack": 1.20, "defense": 0.85},
    "Croatia":           {"attack": 1.18, "defense": 0.85},
    "Denmark":           {"attack": 1.15, "defense": 0.85},
    "Mexico":            {"attack": 1.15, "defense": 0.88},
    "Switzerland":       {"attack": 1.12, "defense": 0.86},
    "United States":     {"attack": 1.10, "defense": 0.90},
    "USA":               {"attack": 1.10, "defense": 0.90},
    "Senegal":           {"attack": 1.10, "defense": 0.90},
    "Morocco":           {"attack": 1.10, "defense": 0.88},
    "Japan":             {"attack": 1.08, "defense": 0.90},
    "Austria":           {"attack": 1.05, "defense": 0.97},
    # Tier 3 — average
    "Turkey":            {"attack": 1.02, "defense": 1.00},
    "Norway":            {"attack": 1.02, "defense": 0.98},
    "Serbia":            {"attack": 0.98, "defense": 1.00},
    "Czech Republic":    {"attack": 0.98, "defense": 1.02},
    "South Korea":       {"attack": 0.98, "defense": 1.02},
    "Ecuador":           {"attack": 1.00, "defense": 1.00},
    "Ukraine":           {"attack": 1.00, "defense": 1.00},
    "Sweden":            {"attack": 1.00, "defense": 1.00},
    "Canada":            {"attack": 0.95, "defense": 1.05},
    "Poland":            {"attack": 0.95, "defense": 1.02},
    "Chile":             {"attack": 0.95, "defense": 1.03},
    "Venezuela":         {"attack": 0.90, "defense": 1.08},
    "Paraguay":          {"attack": 0.90, "defense": 1.08},
    "Wales":             {"attack": 0.92, "defense": 1.05},
    "Peru":              {"attack": 0.92, "defense": 1.05},
    "Romania":           {"attack": 0.95, "defense": 1.05},
    # Tier 4 — below average
    "Bolivia":           {"attack": 0.85, "defense": 1.12},
    "Nigeria":           {"attack": 0.88, "defense": 1.08},
    "Ivory Coast":       {"attack": 0.88, "defense": 1.08},
    "Côte d'Ivoire":    {"attack": 0.88, "defense": 1.08},
    "Cameroon":          {"attack": 0.85, "defense": 1.10},
    "Egypt":             {"attack": 0.85, "defense": 1.10},
    "Algeria":           {"attack": 0.85, "defense": 1.10},
    "Australia":         {"attack": 0.85, "defense": 1.10},
    "Ghana":             {"attack": 0.82, "defense": 1.12},
    "Tunisia":           {"attack": 0.82, "defense": 1.10},
    "Iran":              {"attack": 0.82, "defense": 1.12},
    "Saudi Arabia":      {"attack": 0.80, "defense": 1.15},
    "Costa Rica":        {"attack": 0.78, "defense": 1.15},
    "New Zealand":       {"attack": 0.75, "defense": 1.18},
    "Jamaica":           {"attack": 0.75, "defense": 1.18},
    "Panama":            {"attack": 0.75, "defense": 1.18},
    "Qatar":             {"attack": 0.72, "defense": 1.20},
    "Honduras":          {"attack": 0.72, "defense": 1.20},
    "El Salvador":       {"attack": 0.70, "defense": 1.22},
    "Indonesia":         {"attack": 0.68, "defense": 1.25},
    "Cuba":              {"attack": 0.68, "defense": 1.25},
}


def _build_profile(pid, name: str, hand: str, stats: dict) -> PlayerProfile:
    rates = {k: v for k, v in stats.items() if k.endswith("_rate")}
    if not rates:
        rates = LEAGUE_RATES.copy()
    return PlayerProfile(str(pid or "unk"), name or "TBD", hand, rates)


def run_mlb(game_date: date | None = None,
            n_sims: int = 10_000) -> list[BetAlert]:
    """Full MLB pipeline for one date. Returns BetAlert list."""
    logger.info("=== MLB pipeline: {} ===", game_date or date.today())
    games = assemble_pregame_bundle(game_date)
    if not games:
        logger.info("No MLB games today.")
        return []

    # Fetch odds once for the whole slate (conserves API credits)
    odds_events = get_odds("mlb", markets="h2h,spreads,totals")
    odds_by_id: dict[str, list] = {}
    for ev in odds_events:
        odds_by_id[ev.get("id", "")] = parse_odds_to_snapshots([ev], "MLB")

    # Build team-name → event-id lookup (Odds API uses full names e.g. "Houston Astros")
    team_to_event: dict[str, str] = {}
    for ev in odds_events:
        for side in ("home_team", "away_team"):
            name = ev.get(side, "").lower()
            if name:
                team_to_event[name] = ev.get("id", "")

    odds_team_names = list(team_to_event.keys())

    def _match_mlb_team(short_name: str) -> str | None:
        """Match MLB Stats API short name (e.g. 'Giants') to an Odds API event ID."""
        n = short_name.lower()
        if n in team_to_event:
            return team_to_event[n]
        for full, eid in team_to_event.items():
            if n in full:
                return eid
        hits = difflib.get_close_matches(n, odds_team_names, n=1, cutoff=0.6)
        return team_to_event[hits[0]] if hits else None

    all_alerts: list[BetAlert] = []

    for game in games:
        logger.info("Game: {} @ {}", game["away_team"], game["home_team"])
        upsert_game({**game, "sport": "MLB"})

        # Weather
        venue   = game.get("venue", "")
        stadium = get_stadium(venue)
        weather: dict = {}
        if stadium["lat"] and game.get("game_time"):
            try:
                from zoneinfo import ZoneInfo
                game_dt = datetime.fromisoformat(
                    game["game_time"].replace("Z", "+00:00")
                ).astimezone(ZoneInfo(stadium["tz"]))
                weather = get_game_weather(stadium["lat"], stadium["lon"],
                                           game_dt, stadium["tz"])
            except Exception as e:
                logger.warning("Weather fetch failed for {}: {}", venue, e)

        pf_raw   = get_park_factors(venue)
        park_adj = park_factors_to_pa_adjustments(pf_raw)
        wa = get_weather_adjustments(
            temp_f=weather.get("temperature_f", 70.0),
            wind_speed_mph=weather.get("wind_speed_mph", 0.0),
            wind_direction_deg=weather.get("wind_direction_deg", 0.0),
            park_orientation_deg=pf_raw.get("orientation_deg", 0),
            humidity_pct=weather.get("humidity_pct", 50.0),
            is_dome=pf_raw.get("is_dome", False),
        ) if not pf_raw.get("is_dome") else {}
        combined = {k: park_adj.get(k, 1.0) * wa.get(k, 1.0)
                    for k in set(park_adj) | set(wa)}

        home_lineup = build_dummy_lineup(9)
        away_lineup = build_dummy_lineup(9)
        home_pitcher = _build_profile(
            game.get("home_pitcher_id"), game.get("home_pitcher"),
            "R", game.get("home_pitcher_stats", {})
        )
        away_pitcher = _build_profile(
            game.get("away_pitcher_id"), game.get("away_pitcher"),
            "R", game.get("away_pitcher_stats", {})
        )

        try:
            sim = run_monte_carlo(home_lineup, away_lineup,
                                   home_pitcher, away_pitcher,
                                   combined, {}, n_sims)
            logger.info("Sim: home_win={:.1%} mean_total={:.2f}",
                        sim["home_win_prob"], sim["mean_total"])
        except Exception as e:
            logger.error("Sim failed for {}: {}", game["game_pk"], e)
            continue

        eid = (_match_mlb_team(game["home_team"]) or
               _match_mlb_team(game["away_team"]))
        game_odds = odds_by_id.get(eid, [])
        if not game_odds:
            logger.warning("No odds for {} @ {}", game["away_team"], game["home_team"])
            continue

        alerts = find_mlb_edges(game, sim, game_odds)
        for alert in alerts:
            insert_bet_log({
                "sport":      alert.sport,
                "event":      alert.event,
                "market":     alert.market,
                "book":       alert.book,
                "line":       alert.line,
                "model_prob": alert.model_prob,
                "fair_prob":  alert.fair_prob,
                "edge":       alert.edge,
                "stake_units": alert.stake_units,
                "ev":         alert.edge,
            })
        all_alerts.extend(alerts)

    return all_alerts


def run_soccer(game_date: date | None = None) -> list[BetAlert]:
    """Soccer pipeline: ESPN standings → Poisson model → Odds API edge detection.

    During the 2026 FIFA World Cup (June–July 2026) this scans only World Cup
    fixtures.  When ESPN group-stage standings aren’t available yet, falls back
    to pre-tournament Elo-based ratings stored in _WC_RATINGS.
    """
    import difflib
    from ingestion.espn import get_soccer_standings
    from models.soccer_model import build_score_matrix, matrix_to_markets
    from edge.edge import find_soccer_edges

    logger.info("=== Soccer pipeline (World Cup): {} ===", game_date or date.today())

    LEAGUES    = ["world_cup"]
    HOME_ADV   = 1.0    # neutral-site tournament
    INTL_AVG   = 1.35   # international goals per game per team

    all_alerts: list[BetAlert] = []

    for league in LEAGUES:
        odds_events = get_odds(league, markets="h2h,totals")
        if not odds_events:
            logger.info("No {} odds events today.", league.upper())
            continue
        logger.info("{}: {} fixture(s) with odds", league.upper(), len(odds_events))

        # Try ESPN group-stage standings first; fall back to pre-tournament ratings.
        standings = get_soccer_standings(league)
        if standings:
            total_gp   = sum(s["games_played"] for s in standings)
            total_gf   = sum(s["goals_for"]    for s in standings)
            league_avg = (total_gf / total_gp) if total_gp > 0 else INTL_AVG
            attack:  dict[str, float] = {}
            defense: dict[str, float] = {}
            for row in standings:
                gp = row["games_played"]
                attack[row["team_name"]]  = (row["goals_for"]     / gp) / league_avg
                defense[row["team_name"]] = (row["goals_against"] / gp) / league_avg
            logger.info("WORLD_CUP: using ESPN standings ({} teams, avg={:.3f})",
                        len(standings), league_avg)
        elif league == "world_cup" and _WC_RATINGS:
            league_avg = INTL_AVG
            attack  = {k: v["attack"]  for k, v in _WC_RATINGS.items()}
            defense = {k: v["defense"] for k, v in _WC_RATINGS.items()}
            logger.info("WORLD_CUP: ESPN standings unavailable; using pre-tournament ratings")
        else:
            logger.warning("{}: no standings data; skipping.", league.upper())
            continue

        espn_names = list(attack.keys())

        def _match(name: str) -> str:
            if name in attack:
                return name
            hits = difflib.get_close_matches(name, espn_names, n=1, cutoff=0.4)
            return hits[0] if hits else name

        league_alerts = 0
        for event in odds_events:
            home_raw = event.get("home_team", "")
            away_raw = event.get("away_team", "")
            home = _match(home_raw)
            away = _match(away_raw)

            lam_h = attack.get(home, 1.0) * defense.get(away, 1.0) * league_avg * HOME_ADV
            lam_a = attack.get(away, 1.0) * defense.get(home, 1.0) * league_avg

            score_mat   = build_score_matrix(lam_h, lam_a)
            model_probs = matrix_to_markets(score_mat)

            snapshots = parse_odds_to_snapshots([event], "Soccer")
            fixture   = {"home_team": home_raw, "away_team": away_raw}
            alerts    = find_soccer_edges(fixture, model_probs, snapshots)
            for alert in alerts:
                insert_bet_log({
                    "sport":       alert.sport,
                    "event":       alert.event,
                    "market":      alert.market,
                    "book":        alert.book,
                    "line":        alert.line,
                    "model_prob":  alert.model_prob,
                    "fair_prob":   alert.fair_prob,
                    "edge":        alert.edge,
                    "stake_units": alert.stake_units,
                    "ev":          alert.edge,
                })
            all_alerts.extend(alerts)
            league_alerts += len(alerts)

        logger.info("WORLD_CUP: {} edge(s) found", league_alerts)

    return all_alerts


def run_daily_card(game_date: date | None = None,
                   sport: str = "mlb",
                   consolidated: bool = True,
                   n_sims: int = 10_000) -> None:
    """Orchestrate the full daily pipeline and send Telegram alerts."""
    try:
        alerter = TelegramAlerter()
        alerter.reset_dedup()
    except ValueError as e:
        logger.warning("Telegram not configured ({}). Alerts will be skipped.", e)
        alerter = None

    all_alerts: list[BetAlert] = []

    if sport in ("mlb", "all"):
        all_alerts.extend(run_mlb(game_date, n_sims))

    if sport in ("soccer", "all"):
        all_alerts.extend(run_soccer(game_date))

    if not all_alerts:
        logger.info("No +EV bets found today.")
        if alerter:
            alerter.send_message("No +EV bets cleared the threshold today.")
        return

    logger.info("Found {} +EV bet(s). Sending alerts.", len(all_alerts))
    if alerter:
        if consolidated:
            alerter.send_consolidated_card(all_alerts)
        else:
            alerter.send_batch(all_alerts)


def main() -> None:
    parser = argparse.ArgumentParser(description="+EV Sports Betting Bot")
    parser.add_argument("--sport",   default="mlb", choices=["mlb", "soccer", "all"])
    parser.add_argument("--date",    help="YYYY-MM-DD (default: today)")
    parser.add_argument("--sims",    type=int, default=10_000)
    parser.add_argument("--individual", action="store_true",
                        help="Send one Telegram message per bet instead of a card")
    parser.add_argument("--init-db", action="store_true",
                        help="Initialize / migrate the database and exit")
    parser.add_argument("--report",  action="store_true",
                        help="Print backtest performance report")
    args = parser.parse_args()

    init_db()

    if args.report:
        import json
        from backtest.backtest import generate_report
        print(json.dumps(generate_report(), indent=2))
        return

    if args.init_db:
        logger.info("Database ready.")
        return

    game_date = date.fromisoformat(args.date) if args.date else None
    run_daily_card(game_date, args.sport, not args.individual, args.sims)


if __name__ == "__main__":
    main()
