"""
Main entry point — orchestrates ingestion → simulation → edge → alerts.

Usage:
  python main.py                         # today's MLB card
  python main.py --sport soccer          # soccer only
  python main.py --date 2026-07-04       # specific date
  python main.py --sims 5000             # faster (less accurate)
  python main.py --individual            # one Telegram message per bet
  python main.py --report                # backtest summary
  python main.py --init-db               # create/migrate the database
"""
from __future__ import annotations
import os
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

    # Build a team-name -> odds_event_id lookup for fuzzy matching
    team_to_event: dict[str, str] = {}
    for ev in odds_events:
        for side in ("home_team", "away_team"):
            name = ev.get(side, "").lower()
            if name:
                team_to_event[name] = ev.get("id", "")

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

        # Park + weather adjustments (combined multiplicatively)
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

        # Lineups — use league-average placeholder when confirmed lineup not available
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

        # Monte Carlo simulation
        try:
            sim = run_monte_carlo(home_lineup, away_lineup,
                                   home_pitcher, away_pitcher,
                                   combined, {}, n_sims)
            logger.info("Sim: home_win={:.1%} mean_total={:.2f}",
                        sim["home_win_prob"], sim["mean_total"])
        except Exception as e:
            logger.error("Sim failed for {}: {}", game["game_pk"], e)
            continue

        # Match odds to this game by team name
        eid = (team_to_event.get(game["home_team"].lower()) or
               team_to_event.get(game["away_team"].lower()))
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

    init_db()   # idempotent — safe to call every run

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
