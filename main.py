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
        # 1. Exact match
        if n in team_to_event:
            return team_to_event[n]
        # 2. Substring: 'giants' in 'san francisco giants'
        for full, eid in team_to_event.items():
            if n in full:
                return eid
        # 3. Fuzzy match as last resort
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

        # Match odds to this game by team name (short MLB name → full Odds API name)
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
    fixtures. Group-stage standings from ESPN are used to calibrate team
    attack/defense ratings.
    """
    import difflib
    from ingestion.espn import get_soccer_standings
    from models.soccer_model import build_score_matrix, matrix_to_markets
    from edge.edge import find_soccer_edges

    logger.info("=== Soccer pipeline (World Cup): {} ===", game_date or date.today())

    LEAGUES = ["world_cup"]
    HOME_ADV = 1.0   # no home advantage at a neutral-site tournament

    all_alerts: list[BetAlert] = []

    for league in LEAGUES:
        # h2h = 1 credit, totals = 1 credit
        odds_events = get_odds(league, markets="h2h,totals")
        if not odds_events:
            logger.info("No {} odds events today.", league.upper())
            continue
        logger.info("{}: {} fixture(s) with odds", league.upper(), len(odds_events))

        # Group-stage standings calibrate team strength.
        # Skip if no standings yet (e.g. tournament hasn't started).
        standings = get_soccer_standings(league)
        if not standings:
            logger.warning("{}: no standings data yet; skipping.", league.upper())
            continue

        total_gp = sum(s["games_played"] for s in standings)
        total_gf = sum(s["goals_for"]    for s in standings)
        league_avg = (total_gf / total_gp) if total_gp > 0 else 1.35
        logger.info("World Cup league avg goals/game: {:.3f}", league_avg)

        attack:  dict[str, float] = {}
        defense: dict[str, float] = {}
        for row in standings:
            gp = row["games_played"]
            attack[row["team_name"]]  = (row["goals_for"]     / gp) / league_avg
            defense[row["team_name"]] = (row["goals_against"] / gp) / league_avg

        espn_names = list(attack.keys())

        def _match(name: str) -> str:
            """Fuzzy-match Odds API team name to ESPN standing name."""
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
