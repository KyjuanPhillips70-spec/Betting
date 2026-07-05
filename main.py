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
import time
import difflib
import argparse
from collections import defaultdict
from datetime import date, datetime
from loguru import logger

from dotenv import load_dotenv
load_dotenv()

from storage.database import (
    init_db, upsert_game, insert_bet_log, insert_odds_snapshot,
)
from ingestion.mlb_statsapi import (
    assemble_pregame_bundle, get_batter_vs_pitcher,
)
from ingestion.weather import get_game_weather
from ingestion.odds import get_odds, get_event_odds, parse_odds_to_snapshots
from models.mlb_sim import (
    run_monte_carlo, build_dummy_lineup, PlayerProfile, LEAGUE_RATES
)
from models.weather_adj import get_weather_adjustments
from config.park_factors import get_park_factors, park_factors_to_pa_adjustments
from config.stadiums import get_stadium
from edge.edge import find_mlb_edges, find_player_prop_edges
from edge.odds_math import american_to_decimal, expected_value
from alerting.telegram_alerts import TelegramAlerter, BetAlert


_INTL_AVG = 1.35   # international goals per game per team (neutral-site)


_WC_RATINGS: dict[str, dict[str, float]] = {
    "Brazil":            {"attack": 1.55, "defense": 0.70},
    "France":            {"attack": 1.50, "defense": 0.72},
    "Argentina":         {"attack": 1.50, "defense": 0.72},
    "Spain":             {"attack": 1.45, "defense": 0.73},
    "Portugal":          {"attack": 1.45, "defense": 0.74},
    "Germany":           {"attack": 1.42, "defense": 0.76},
    "England":           {"attack": 1.40, "defense": 0.75},
    "Netherlands":       {"attack": 1.38, "defense": 0.78},
    "Belgium":           {"attack": 1.25, "defense": 0.85},
    "Italy":             {"attack": 1.22, "defense": 0.82},
    "Uruguay":           {"attack": 1.20, "defense": 0.83},
    "Colombia":          {"attack": 1.20, "defense": 0.85},
    "Croatia":           {"attack": 1.18, "defense": 0.85},
    "Denmark":           {"attack": 1.15, "defense": 0.85},
    "Mexico":            {"attack": 1.15, "defense": 0.88},
    "Switzerland":       {"attack": 1.12, "defense": 0.86},
    "United States":     {"attack": 1.10, "defense": 0.90},
    "Senegal":           {"attack": 1.10, "defense": 0.90},
    "Morocco":           {"attack": 1.10, "defense": 0.88},
    "Japan":             {"attack": 1.08, "defense": 0.90},
    "Austria":           {"attack": 1.05, "defense": 0.97},
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
    "Bolivia":           {"attack": 0.85, "defense": 1.12},
    "Nigeria":           {"attack": 0.88, "defense": 1.08},
    "Ivory Coast":       {"attack": 0.88, "defense": 1.08},
    "Côte d'Ivoire":     {"attack": 0.88, "defense": 1.08},
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

_WC_ALIASES: dict[str, str] = {
    "USA":               "United States",
    "US":                "United States",
    "Cote d'Ivoire":     "Côte d'Ivoire",
    "Korea Republic":    "South Korea",
    "Republic of Korea": "South Korea",
    "IR Iran":           "Iran",
    "Czechia":           "Czech Republic",
    "Türkiye":           "Turkey",
}


# ---------------------------------------------------------------------------
# Rate conversion helpers
# ---------------------------------------------------------------------------

def _team_batting_to_rates(stat: dict) -> dict:
    ab      = stat.get("atBats", 0) or 0
    h       = stat.get("hits", 0) or 0
    bb      = stat.get("baseOnBalls", 0) or 0
    hbp     = stat.get("hitByPitch", 0) or 0
    so      = stat.get("strikeOuts", 0) or 0
    hr      = stat.get("homeRuns", 0) or 0
    doubles = stat.get("doubles", 0) or 0
    triples = stat.get("triples", 0) or 0
    singles = max(h - doubles - triples - hr, 0)
    pa = ab + bb + hbp
    if pa < 50:
        return LEAGUE_RATES.copy()
    return {
        "K_rate":   so / pa,
        "BB_rate":  bb / pa,
        "HBP_rate": hbp / pa,
        "1B_rate":  singles / pa,
        "2B_rate":  doubles / pa,
        "3B_rate":  triples / pa,
        "HR_rate":  hr / pa,
        "out_rate": max(0.0, (ab - h)) / pa,
    }


def _pitcher_stats_to_rates(stat: dict) -> dict:
    """
    Convert raw MLB Stats API pitching stats to rate format.
    MLB uses 'hitBatsmen' for pitchers (not 'hitByPitch').
    Falls back to league average when sample < 50 BF.
    """
    bf      = stat.get("battersFaced", 0) or 0
    k       = stat.get("strikeOuts", 0) or 0
    bb      = stat.get("baseOnBalls", 0) or 0
    hbp     = stat.get("hitBatsmen", stat.get("hitByPitch", 0)) or 0
    h       = stat.get("hits", 0) or 0
    hr      = stat.get("homeRuns", 0) or 0
    doubles = stat.get("doubles", 0) or 0
    triples = stat.get("triples", 0) or 0
    singles = max(h - doubles - triples - hr, 0)
    if bf < 50:
        return LEAGUE_RATES.copy()
    in_play_outs = max(0.0, bf - k - bb - hbp - h)
    return {
        "K_rate":   k / bf,
        "BB_rate":  bb / bf,
        "HBP_rate": hbp / bf,
        "1B_rate":  singles / bf,
        "2B_rate":  doubles / bf,
        "3B_rate":  triples / bf,
        "HR_rate":  hr / bf,
        "out_rate": in_play_outs / bf,
    }


def _ratings_from_wc_results(
    results: list[dict], intl_avg: float
) -> tuple[dict, dict, float]:
    PRIOR_GAMES = 8
    scored:   dict[str, list] = defaultdict(list)
    conceded: dict[str, list] = defaultdict(list)
    for r in results:
        scored[r["home_team"]].append(r["home_goals"])
        scored[r["away_team"]].append(r["away_goals"])
        conceded[r["home_team"]].append(r["away_goals"])
        conceded[r["away_team"]].append(r["home_goals"])

    total_goals = sum(r["home_goals"] + r["away_goals"] for r in results)
    total_games = len(results)
    league_avg = (total_goals / (total_games * 2)) if total_games > 0 else intl_avg

    attack:  dict[str, float] = {}
    defense: dict[str, float] = {}
    for team in scored:
        n = len(scored[team])
        w = n / (n + PRIOR_GAMES)
        raw_atk = (sum(scored[team]) / n) / league_avg
        raw_def = (sum(conceded[team]) / n) / league_avg
        canonical = _WC_ALIASES.get(team, team)
        pre = _WC_RATINGS.get(canonical, {"attack": 1.0, "defense": 1.0})
        attack[team]  = w * raw_atk + (1 - w) * pre["attack"]
        defense[team] = w * raw_def + (1 - w) * pre["defense"]

    for team, pre in _WC_RATINGS.items():
        if team not in attack:
            attack[team]  = pre["attack"]
            defense[team] = pre["defense"]

    return attack, defense, league_avg


def _blend_matchup_rates(base_rates: dict, matchup_stats: dict,
                          pa_threshold: int = 15) -> dict:
    """
    Blend a batter's season rates with historical batter-vs-pitcher data.
    Weight = min(PA / 60, 0.70) × 0.50 — max 35% at 60+ PA.
    Falls back to base_rates when the matchup sample is below pa_threshold.

    Computes matchup rates directly from raw counts rather than calling
    _team_batting_to_rates, which returns LEAGUE_RATES for PA < 50 and
    would silently erase small-sample matchup signal.
    """
    pa = matchup_stats.get("plateAppearances", 0) or 0
    if pa < pa_threshold:
        return base_rates
    weight = min(pa / 60.0, 0.70) * 0.50

    ab      = matchup_stats.get("atBats", 0) or 0
    h       = matchup_stats.get("hits", 0) or 0
    bb      = matchup_stats.get("baseOnBalls", 0) or 0
    hbp     = matchup_stats.get("hitByPitch", 0) or 0
    so      = matchup_stats.get("strikeOuts", 0) or 0
    hr      = matchup_stats.get("homeRuns", 0) or 0
    doubles = matchup_stats.get("doubles", 0) or 0
    triples = matchup_stats.get("triples", 0) or 0
    singles = max(h - doubles - triples - hr, 0)
    denom   = ab + bb + hbp
    if denom == 0:
        return base_rates
    matchup_rates = {
        "K_rate":   so / denom,
        "BB_rate":  bb / denom,
        "HBP_rate": hbp / denom,
        "1B_rate":  singles / denom,
        "2B_rate":  doubles / denom,
        "3B_rate":  triples / denom,
        "HR_rate":  hr / denom,
        "out_rate": max(0.0, (ab - h)) / denom,
    }
    blended = {
        k: (1 - weight) * base_rates.get(k, LEAGUE_RATES[k])
           + weight * matchup_rates.get(k, base_rates.get(k, LEAGUE_RATES[k]))
        for k in LEAGUE_RATES
    }
    total = sum(blended.values())
    return {k: v / total for k, v in blended.items()} if total > 0 else base_rates


# ---------------------------------------------------------------------------
# Profile builders
# ---------------------------------------------------------------------------

def _build_pitcher_profile(pid, name: str, hand: str, stats: dict) -> PlayerProfile:
    """Build a pitcher profile from raw MLB Stats API pitching stats."""
    return PlayerProfile(str(pid or "unk"), name or "TBD", hand,
                         _pitcher_stats_to_rates(stats))


def _build_lineup_profiles(
    lineup_stats: list[dict],
    team_name: str,
    fallback_rates: dict,
) -> list[PlayerProfile]:
    """
    Build a 9-slot lineup from confirmed per-player batting stats.
    Slots with insufficient data (< 50 PA) fall back to the team aggregate.
    """
    profiles: list[PlayerProfile] = []
    for i, player in enumerate(lineup_stats[:9]):
        rates = _team_batting_to_rates(player.get("stats", {}))
        profiles.append(PlayerProfile(
            str(player.get("player_id", f"{team_name}_{i}")),
            player.get("name", f"{team_name} {i+1}"),
            player.get("bat_side", "R"),
            rates,
        ))
    while len(profiles) < 9:
        i = len(profiles)
        profiles.append(PlayerProfile(
            f"{team_name}_{i}", f"{team_name} {i+1}", "R", fallback_rates.copy()
        ))
    return profiles


def _store_snapshots(snapshots: list[dict], game_pk: str) -> None:
    """Best-effort odds snapshot storage for CLV tracking."""
    for snap in snapshots:
        try:
            insert_odds_snapshot({**snap, "game_pk": game_pk})
        except Exception as exc:
            logger.debug("Snapshot insert skipped ({}): {}", snap.get("outcome", "?"), exc)


# ---------------------------------------------------------------------------
# MLB pipeline
# ---------------------------------------------------------------------------

def run_mlb(game_date: date | None = None, n_sims: int = 10_000) -> list[BetAlert]:
    """Full MLB pipeline for one date."""
    logger.info("=== MLB pipeline: {} ===", game_date or date.today())
    games = assemble_pregame_bundle(game_date)
    if not games:
        logger.info("No MLB games today.")
        return []

    odds_events = get_odds("mlb", markets="h2h,spreads,totals")
    odds_by_id: dict[str, list] = {}
    for ev in odds_events:
        odds_by_id[ev.get("id", "")] = parse_odds_to_snapshots([ev], "MLB")

    team_to_event: dict[str, str] = {}
    for ev in odds_events:
        for side in ("home_team", "away_team"):
            name = ev.get(side, "").lower()
            if name:
                team_to_event[name] = ev.get("id", "")

    odds_team_names = list(team_to_event.keys())

    def _match_mlb_team(short_name: str) -> str | None:
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
        if not game.get("home_team") or not game.get("away_team"):
            continue

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

        # Team aggregate rates (always available; used as fallback)
        home_agg_rates = _team_batting_to_rates(game.get("home_team_batting", {}))
        away_agg_rates = _team_batting_to_rates(game.get("away_team_batting", {}))

        # Per-player lineup when confirmed (>= 7 players with data), else aggregate
        home_lineup_stats = game.get("home_lineup_stats", [])
        away_lineup_stats = game.get("away_lineup_stats", [])

        if len(home_lineup_stats) >= 7:
            home_lineup = _build_lineup_profiles(
                home_lineup_stats, game.get("home_team", "Home"), home_agg_rates)
            logger.info("Home: confirmed {}-player lineup", len(home_lineup_stats))
        else:
            home_lineup = [
                PlayerProfile(f"home_{i}", f"{game.get('home_team','Home')} {i+1}",
                              "R", home_agg_rates.copy())
                for i in range(9)
            ]

        if len(away_lineup_stats) >= 7:
            away_lineup = _build_lineup_profiles(
                away_lineup_stats, game.get("away_team", "Away"), away_agg_rates)
            logger.info("Away: confirmed {}-player lineup", len(away_lineup_stats))
        else:
            away_lineup = [
                PlayerProfile(f"away_{i}", f"{game.get('away_team','Away')} {i+1}",
                              "R", away_agg_rates.copy())
                for i in range(9)
            ]

        # Starter profiles with real pitch hand from Stats API
        home_pitcher = _build_pitcher_profile(
            game.get("home_pitcher_id"), game.get("home_pitcher"),
            game.get("home_pitcher_hand", "R"), game.get("home_pitcher_stats", {})
        )
        away_pitcher = _build_pitcher_profile(
            game.get("away_pitcher_id"), game.get("away_pitcher"),
            game.get("away_pitcher_hand", "R"), game.get("away_pitcher_stats", {})
        )

        # Bullpen profiles from team pitching aggregate (innings 6+)
        home_bullpen = _build_pitcher_profile(
            None, f"{game.get('home_team','Home')} BP",
            "R", game.get("home_team_pitching", {})
        )
        away_bullpen = _build_pitcher_profile(
            None, f"{game.get('away_team','Away')} BP",
            "R", game.get("away_team_pitching", {})
        )

        # Blend each batter's season rates with batter-vs-opposing-starter history.
        # Only confirmed lineups have integer player IDs; aggregate slots are skipped.
        away_starter_id = game.get("away_pitcher_id")
        home_starter_id = game.get("home_pitcher_id")
        if away_starter_id:
            for profile in home_lineup:
                if profile.player_id.isdigit():
                    try:
                        matchup = get_batter_vs_pitcher(
                            int(profile.player_id), away_starter_id)
                        profile.rates = _blend_matchup_rates(profile.rates, matchup)
                    except Exception:
                        pass
                    time.sleep(0.10)
        if home_starter_id:
            for profile in away_lineup:
                if profile.player_id.isdigit():
                    try:
                        matchup = get_batter_vs_pitcher(
                            int(profile.player_id), home_starter_id)
                        profile.rates = _blend_matchup_rates(profile.rates, matchup)
                    except Exception:
                        pass
                    time.sleep(0.10)

        try:
            sim = run_monte_carlo(
                home_lineup, away_lineup,
                home_pitcher, away_pitcher,
                combined, {},
                n_sims,
                home_bullpen=home_bullpen,
                away_bullpen=away_bullpen,
                track_props=True,
            )
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

        _store_snapshots(game_odds, eid or game.get("game_pk", ""))

        alerts = find_mlb_edges(game, sim, game_odds)
        proj = (
            f"Proj: {game.get('away_team','?')} {sim['mean_away_runs']:.1f}"
            f" @ {game.get('home_team','?')} {sim['mean_home_runs']:.1f}"
            f"  ({sim['mean_total']:.1f} total runs)"
        )
        for alert in alerts:
            alert.projected_score = proj

        for alert in alerts:
            try:
                _dec = american_to_decimal(int(alert.line))
                _ev  = expected_value(alert.model_prob, _dec)
            except (ValueError, ZeroDivisionError):
                _ev = alert.edge
            insert_bet_log({
                "sport":           alert.sport,
                "event":           alert.event,
                "market":          alert.market,
                "book":            alert.book,
                "line":            alert.line,
                "model_prob":      alert.model_prob,
                "fair_prob":       alert.fair_prob,
                "edge":            alert.edge,
                "stake_units":     alert.stake_units,
                "ev":              _ev,
                "projected_score": proj,
            })
        all_alerts.extend(alerts)

        # Player prop edges
        if eid and sim.get("prop_distributions"):
            try:
                prop_event = get_event_odds("mlb", eid, markets=(
                    "batter_hits,batter_total_bases,batter_home_runs,"
                    "batter_rbis,batter_walks,pitcher_strikeouts,pitcher_outs"
                ))
                if prop_event:
                    prop_snaps  = parse_odds_to_snapshots([prop_event], "MLB")
                    prop_alerts = find_player_prop_edges(game, sim, prop_snaps)
                    for a in prop_alerts:
                        a.projected_score = proj
                    for a in prop_alerts:
                        try:
                            _dec = american_to_decimal(int(a.line))
                            _ev  = expected_value(a.model_prob, _dec)
                        except (ValueError, ZeroDivisionError):
                            _ev = a.edge
                        insert_bet_log({
                            "sport":           a.sport,
                            "event":           a.event,
                            "market":          a.market,
                            "book":            a.book,
                            "line":            a.line,
                            "model_prob":      a.model_prob,
                            "fair_prob":       a.fair_prob,
                            "edge":            a.edge,
                            "stake_units":     a.stake_units,
                            "ev":              _ev,
                            "projected_score": proj,
                        })
                    all_alerts.extend(prop_alerts)
                    if prop_alerts:
                        logger.info("Props: {} edge(s) for {} @ {}",
                                    len(prop_alerts),
                                    game["away_team"], game["home_team"])
            except Exception as e:
                logger.warning("Prop pipeline failed for {} @ {}: {}",
                               game.get("away_team", "?"),
                               game.get("home_team", "?"), e)

    return all_alerts


# ---------------------------------------------------------------------------
# Soccer pipeline
# ---------------------------------------------------------------------------

def run_soccer(game_date: date | None = None) -> list[BetAlert]:
    """Soccer pipeline for the 2026 FIFA World Cup."""
    from ingestion.espn import get_soccer_standings, get_wc_results, get_wc_scoring_leaders
    from models.soccer_model import build_score_matrix, matrix_to_markets
    from edge.edge import find_soccer_edges

    logger.info("=== Soccer pipeline (World Cup): {} ===", game_date or date.today())

    LEAGUES  = ["world_cup"]
    HOME_ADV = 1.0

    try:
        scoring_leaders = get_wc_scoring_leaders()
        leaders_by_team: dict[str, list[str]] = defaultdict(list)
        for entry in scoring_leaders:
            t = entry["team"]
            if len(leaders_by_team[t]) < 3:
                leaders_by_team[t].append(f"{entry['player_name']}({entry['goals']}g)")
    except Exception:
        leaders_by_team = defaultdict(list)

    all_alerts: list[BetAlert] = []

    for league in LEAGUES:
        odds_events = get_odds(league, markets="h2h,totals,btts")
        if not odds_events:
            logger.info("No {} odds events today.", league.upper())
            continue
        logger.info("{}: {} fixture(s) with odds", league.upper(), len(odds_events))

        attack:  dict[str, float] = {}
        defense: dict[str, float] = {}
        league_avg: float = _INTL_AVG

        if league == "world_cup":
            wc_results = get_wc_results()
            if len(wc_results) >= 4:
                attack, defense, league_avg = _ratings_from_wc_results(
                    wc_results, _INTL_AVG
                )
                logger.info("WORLD_CUP: {} matches → Bayesian-blended ratings",
                            len(wc_results))
            else:
                attack  = {k: v["attack"]  for k, v in _WC_RATINGS.items()}
                defense = {k: v["defense"] for k, v in _WC_RATINGS.items()}
                logger.info("WORLD_CUP: {} results (< 4); using pre-tournament ratings",
                            len(wc_results))
        else:
            standings = get_soccer_standings(league)
            if standings and sum(s["games_played"] for s in standings) > 0:
                total_gp   = sum(s["games_played"] for s in standings)
                total_gf   = sum(s["goals_for"]    for s in standings)
                league_avg = (total_gf / total_gp) if total_gp > 0 else _INTL_AVG
                for row in standings:
                    gp = row["games_played"]
                    attack[row["team_name"]]  = (row["goals_for"]     / gp) / league_avg
                    defense[row["team_name"]] = (row["goals_against"] / gp) / league_avg
                logger.info("{}: ESPN standings ({} teams, avg={:.3f})",
                            league.upper(), len(standings), league_avg)
            else:
                logger.warning("{}: no ratings data; skipping.", league.upper())
                continue

        espn_names = list(attack.keys())

        def _match(name: str) -> str:
            canonical = _WC_ALIASES.get(name, name)
            if canonical in attack:
                return canonical
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

            h_stars = ", ".join(leaders_by_team.get(home_raw, [])) or "n/a"
            a_stars = ", ".join(leaders_by_team.get(away_raw, [])) or "n/a"
            logger.info("  {}: top scorers {} | {}: top scorers {}",
                        home_raw, h_stars, away_raw, a_stars)

            score_mat   = build_score_matrix(lam_h, lam_a)
            model_probs = matrix_to_markets(score_mat)

            snapshots = parse_odds_to_snapshots([event], "Soccer")
            fixture   = {"home_team": home_raw, "away_team": away_raw}
            alerts    = find_soccer_edges(fixture, model_probs, snapshots)

            _store_snapshots(snapshots, event.get("id", ""))

            proj = f"Proj: {home_raw} {lam_h:.2f} – {away_raw} {lam_a:.2f} goals"
            for alert in alerts:
                alert.projected_score = proj

            for alert in alerts:
                try:
                    _dec = american_to_decimal(int(alert.line))
                    _ev  = expected_value(alert.model_prob, _dec)
                except (ValueError, ZeroDivisionError):
                    _ev = alert.edge
                insert_bet_log({
                    "sport":           alert.sport,
                    "event":           alert.event,
                    "market":          alert.market,
                    "book":            alert.book,
                    "line":            alert.line,
                    "model_prob":      alert.model_prob,
                    "fair_prob":       alert.fair_prob,
                    "edge":            alert.edge,
                    "stake_units":     alert.stake_units,
                    "ev":              _ev,
                    "projected_score": proj,
                })
            all_alerts.extend(alerts)
            league_alerts += len(alerts)

        logger.info("WORLD_CUP: {} edge(s) found", league_alerts)

    return all_alerts


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run_daily_card(game_date: date | None = None,
                   sport: str = "mlb",
                   n_sims: int = 10_000) -> None:
    try:
        alerter = TelegramAlerter()
        alerter.reset_dedup()
    except ValueError as e:
        logger.warning("Telegram not configured ({}). Alerts will be skipped.", e)
        alerter = None

    all_alerts: list[BetAlert] = []

    if sport in ("mlb", "all"):
        try:
            all_alerts.extend(run_mlb(game_date, n_sims))
        except Exception as exc:
            logger.error("MLB pipeline failed: {}", exc)

    if sport in ("soccer", "all"):
        try:
            all_alerts.extend(run_soccer(game_date))
        except Exception as exc:
            logger.error("Soccer pipeline failed: {}", exc)

    if not all_alerts:
        logger.info("No +EV bets found today.")
        if alerter:
            alerter.send_message("No +EV bets cleared the threshold today.")
        return

    logger.info("Found {} +EV bet(s). Sending per-game cards.", len(all_alerts))
    if alerter:
        alerter.send_game_cards(all_alerts)


def main() -> None:
    parser = argparse.ArgumentParser(description="+EV Sports Betting Bot")
    parser.add_argument("--sport",   default="mlb", choices=["mlb", "soccer", "all"])
    parser.add_argument("--date",    help="YYYY-MM-DD (default: today)")
    parser.add_argument("--sims",    type=int, default=10_000)
    parser.add_argument("--individual", action="store_true")
    parser.add_argument("--init-db", action="store_true")
    parser.add_argument("--report",  action="store_true")
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

    if args.individual:
        try:
            alerter = TelegramAlerter()
            alerter.reset_dedup()
        except ValueError as e:
            logger.warning("Telegram not configured: {}", e)
            alerter = None
        alerts: list[BetAlert] = []
        if args.sport in ("mlb", "all"):
            try:
                alerts.extend(run_mlb(game_date, args.sims))
            except Exception as exc:
                logger.error("MLB pipeline failed: {}", exc)
        if args.sport in ("soccer", "all"):
            try:
                alerts.extend(run_soccer(game_date))
            except Exception as exc:
                logger.error("Soccer pipeline failed: {}", exc)
        if alerter and alerts:
            alerter.send_batch(alerts)
        elif not alerts:
            logger.info("No +EV bets found.")
    else:
        run_daily_card(game_date, args.sport, args.sims)


if __name__ == "__main__":
    main()
