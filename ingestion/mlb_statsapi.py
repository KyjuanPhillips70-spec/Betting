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


def _first_splits(data: dict, outer_key: str = "stats") -> list:
    """Safely extract the splits list from the first stats group."""
    groups = data.get(outer_key) or []
    if not groups:
        return []
    return groups[0].get("splits") or []


def get_schedule(game_date: date | None = None) -> list[dict]:
    """Return schedule for a date with game_pk, teams, probable pitchers, venue,
    and confirmed lineup player IDs when the lineup has been posted."""
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

            # Extract confirmed lineup player IDs when the lineup is already posted
            # (typically 2-3 hours before first pitch). Empty list when not yet available.
            lineups_raw = g.get("lineups", {})
            home_lineup_ids = [
                p["id"] for p in lineups_raw.get("homePlayers", []) if p.get("id")
            ][:9]
            away_lineup_ids = [
                p["id"] for p in lineups_raw.get("awayPlayers", []) if p.get("id")
            ][:9]

            games.append({
                "game_pk":         str(g.get("gamePk", "")),
                "game_date":       d,
                "game_time":       g.get("gameDate", ""),
                "status":          g.get("status", {}).get("abstractGameState", ""),
                "home_team":       g["teams"]["home"]["team"].get("teamName", ""),
                "home_team_id":    g["teams"]["home"]["team"].get("id"),
                "away_team":       g["teams"]["away"]["team"].get("teamName", ""),
                "away_team_id":    g["teams"]["away"]["team"].get("id"),
                "home_pitcher_id": g["teams"]["home"].get("probablePitcher", {}).get("id"),
                "home_pitcher":    g["teams"]["home"].get("probablePitcher", {}).get("fullName"),
                "away_pitcher_id": g["teams"]["away"].get("probablePitcher", {}).get("id"),
                "away_pitcher":    g["teams"]["away"].get("probablePitcher", {}).get("fullName"),
                "venue":           g.get("venue", {}).get("name", ""),
                "venue_lat":       venue_loc.get("latitude"),
                "venue_lon":       venue_loc.get("longitude"),
                "home_lineup_ids": home_lineup_ids,
                "away_lineup_ids": away_lineup_ids,
            })
    return games


def get_player_stats(person_id: int, group: str = "hitting",
                     season: int = CURRENT_SEASON) -> dict:
    """Season aggregate stats for a player. group: 'hitting' or 'pitching'."""
    data = _get(f"/v1/people/{person_id}/stats",
                {"stats": "season", "group": group, "season": season})
    splits = _first_splits(data)
    return splits[0].get("stat", {}) if splits else {}


def get_players_batting_stats(player_ids: list[int],
                              season: int = CURRENT_SEASON) -> list[dict]:
    """
    Batch-fetch season batting stats for a confirmed lineup in ONE API call.
    Returns list of {player_id, name, bat_side, stats} in the same order as
    player_ids. Entries with no stats data have an empty stats dict.

    This replaces the old 9-identical-batter approach: each slot now has the
    real batter's K%, BB%, HR%, hit rates so the log5 matchup calculation
    reflects actual offensive tendencies.
    """
    if not player_ids:
        return []
    ids_str = ",".join(str(p) for p in player_ids)
    data = _get("/v1/people", {
        "personIds": ids_str,
        "hydrate": f"stats(group=[hitting],type=[season],season={season}),batSide",
    })
    # Build a lookup by player ID so we can return results in batting-order
    by_id: dict[int, dict] = {}
    for person in data.get("people", []):
        pid = person.get("id")
        stats_groups = person.get("stats") or []
        batting_stats: dict = {}
        for grp in stats_groups:
            splits = grp.get("splits", [])
            if splits:
                batting_stats = splits[0].get("stat", {})
                break
        by_id[pid] = {
            "player_id": pid,
            "name":      person.get("fullName", str(pid)),
            "bat_side":  person.get("batSide", {}).get("code", "R"),
            "stats":     batting_stats,
        }
    return [by_id.get(pid, {"player_id": pid, "name": str(pid),
                             "bat_side": "R", "stats": {}})
            for pid in player_ids]


def get_player_splits(person_id: int, group: str = "hitting",
                      season: int = CURRENT_SEASON) -> list[dict]:
    """Hitting/pitching situational splits (vs L, vs R, home, away)."""
    data = _get(f"/v1/people/{person_id}/stats",
                {"stats": "statSplits", "group": group,
                 "season": season, "sitCodes": "vl,vr,h,a"})
    return _first_splits(data)


def get_roster(team_id: int, season: int = CURRENT_SEASON) -> list[dict]:
    data = _get(f"/v1/teams/{team_id}/roster",
                {"rosterType": "active", "season": season})
    return data.get("roster", [])


def get_team_batting_stats(team_id: int, season: int = CURRENT_SEASON) -> dict:
    """Season aggregate team batting stats — fallback when individual lineup unavailable."""
    data = _get(f"/v1/teams/{team_id}/stats",
                {"stats": "season", "group": "hitting", "season": season})
    splits = _first_splits(data)
    return splits[0].get("stat", {}) if splits else {}


def get_team_pitching_stats(team_id: int, season: int = CURRENT_SEASON) -> dict:
    """
    Season aggregate team pitching stats — used as bullpen profile.
    The aggregate blends starters + relievers but is dominated by bullpen
    volume in later innings when the starter is gone.
    """
    data = _get(f"/v1/teams/{team_id}/stats",
                {"stats": "season", "group": "pitching", "season": season})
    splits = _first_splits(data)
    return splits[0].get("stat", {}) if splits else {}


def get_live_feed(game_pk: str) -> dict:
    """GUMBO live game feed — full game state including in-game weather."""
    return _get(f"/v1.1/game/{game_pk}/feed/live")


def get_boxscore(game_pk: str) -> dict:
    return _get(f"/v1/game/{game_pk}/boxscore")


def assemble_pregame_bundle(game_date: date | None = None) -> list[dict]:
    """
    Full pre-game data bundle for every game on a date.
    Fetches (in order):
      - Probable pitcher season stats + splits
      - Team batting aggregate (fallback lineup)
      - Team pitching aggregate (bullpen proxy)
      - Per-player batting stats when confirmed lineup is available (batch call)
    """
    games = get_schedule(game_date)
    logger.info("Found {} MLB games for {}", len(games), game_date or date.today())

    for game in games:
        for side in ("home", "away"):
            pid = game.get(f"{side}_pitcher_id")
            if pid:
                game[f"{side}_pitcher_stats"]  = get_player_stats(pid, "pitching")
                game[f"{side}_pitcher_splits"] = get_player_splits(pid, "pitching")
                time.sleep(0.15)

            tid = game.get(f"{side}_team_id")
            if tid:
                game[f"{side}_team_batting"]  = get_team_batting_stats(tid)
                game[f"{side}_team_pitching"] = get_team_pitching_stats(tid)
                time.sleep(0.10)

            # Per-player lineup stats when the lineup has been officially posted
            lineup_ids = game.get(f"{side}_lineup_ids", [])
            if lineup_ids:
                game[f"{side}_lineup_stats"] = get_players_batting_stats(lineup_ids)
                logger.info("{} {} confirmed lineup: {} players fetched",
                            game.get(f"{side}_team", side), side, len(lineup_ids))
                time.sleep(0.15)
            else:
                game[f"{side}_lineup_stats"] = []

    return games
