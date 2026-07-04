"""
MLB Stats API fetcher.
Base URL: https://statsapi.mlb.com/api/
No API key required. Rate-limit respected via caching and small sleeps.
"""
from __future__ import annotations
import time
import requests
from datetime import date
from loguru import logger

BASE_URL = "https://statsapi.mlb.com/api"
CURRENT_SEASON = 2026

_SESSION = requests.Session()
_SESSION.headers["User-Agent"] = "BettingBot/1.0 (personal, non-commercial research)"


def _get(path: str, params: dict | None = None, retries: int = 3) -> dict:
    url = f"{BASE_URL}{path}"
    for attempt in range(retries):
        try:
            r = _SESSION.get(url, params=params, timeout=15)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            wait = 2 ** attempt
            logger.warning("MLB API error (attempt {}): {} — sleeping {}s", attempt + 1, e, wait)
            time.sleep(wait)
    return {}


def get_schedule(game_date: date | None = None) -> list[dict]:
    """Return schedule for a date with game_pk, teams, probable pitchers, venue coords."""
    d = (game_date or date.today()).strftime("%Y-%m-%d")
    data = _get("/v1/schedule", {
        "sportId": 1,
        "date": d,
        "hydrate": "probablePitcher,lineups,team,venue(location)",
    })
    games = []
    for date_block in data.get("dates", []):
        for g in date_block.get("games", []):
            venue_loc = g.get("venue", {}).get("location", {}).get("defaultCoordinates", {})
            games.append({
                "game_pk":        str(g.get("gamePk", "")),
                "game_date":      d,
                "game_time":      g.get("gameDate", ""),
                "status":         g.get("status", {}).get("abstractGameState", ""),
                "home_team":      g["teams"]["home"]["team"].get("teamName", ""),
                "home_team_id":   g["teams"]["home"]["team"].get("id"),
                "away_team":      g["teams"]["away"]["team"].get("teamName", ""),
                "away_team_id":   g["teams"]["away"]["team"].get("id"),
                "home_pitcher_id": g["teams"]["home"].get("probablePitcher", {}).get("id"),
                "home_pitcher":   g["teams"]["home"].get("probablePitcher", {}).get("fullName"),
                "away_pitcher_id": g["teams"]["away"].get("probablePitcher", {}).get("id"),
                "away_pitcher":   g["teams"]["away"].get("probablePitcher", {}).get("fullName"),
                "venue":          g.get("venue", {}).get("name", ""),
                "venue_lat":      venue_loc.get("latitude"),
                "venue_lon":      venue_loc.get("longitude"),
            })
    return games


def get_player_stats(person_id: int, group: str = "hitting",
                     season: int = CURRENT_SEASON) -> dict:
    """Season aggregate stats for a player. group: 'hitting' or 'pitching'."""
    data = _get(f"/v1/people/{person_id}/stats",
                {"stats": "season", "group": group, "season": season})
    splits = data.get("stats", [{}])[0].get("splits", [{}])
    return splits[0].get("stat", {}) if splits else {}


def get_player_splits(person_id: int, group: str = "hitting",
                      season: int = CURRENT_SEASON) -> list[dict]:
    """Hitting/pitching situational splits (vs L, vs R, home, away)."""
    data = _get(f"/v1/people/{person_id}/stats",
                {"stats": "statSplits", "group": group,
                 "season": season, "sitCodes": "vl,vr,h,a"})
    return data.get("stats", [{}])[0].get("splits", [])


def get_roster(team_id: int, season: int = CURRENT_SEASON) -> list[dict]:
    data = _get(f"/v1/teams/{team_id}/roster",
                {"rosterType": "active", "season": season})
    return data.get("roster", [])


def get_team_batting_stats(team_id: int, season: int = CURRENT_SEASON) -> dict:
    """Season aggregate team batting stats — used to build realistic lineup profiles."""
    data = _get(f"/v1/teams/{team_id}/stats",
                {"stats": "season", "group": "hitting", "season": season})
    splits = data.get("stats", [{}])[0].get("splits", [{}])
    return splits[0].get("stat", {}) if splits else {}


def get_live_feed(game_pk: str) -> dict:
    """GUMBO live game feed — full game state including in-game weather."""
    return _get(f"/v1.1/game/{game_pk}/feed/live")


def get_boxscore(game_pk: str) -> dict:
    return _get(f"/v1/game/{game_pk}/boxscore")


def assemble_pregame_bundle(game_date: date | None = None) -> list[dict]:
    """
    Full pre-game data bundle for every game on a date:
    schedule + probable pitcher stats/splits + team season batting stats.
    """
    games = get_schedule(game_date)
    logger.info("Found {} MLB games for {}", len(games), game_date or date.today())

    for game in games:
        for side in ("home", "away"):
            pid = game.get(f"{side}_pitcher_id")
            if pid:
                game[f"{side}_pitcher_stats"] = get_player_stats(pid, "pitching")
                game[f"{side}_pitcher_splits"] = get_player_splits(pid, "pitching")
                time.sleep(0.15)
            # Real team batting stats for simulation (replaces dummy league-average lineup)
            tid = game.get(f"{side}_team_id")
            if tid:
                game[f"{side}_team_batting"] = get_team_batting_stats(tid)
                time.sleep(0.10)

    return games
