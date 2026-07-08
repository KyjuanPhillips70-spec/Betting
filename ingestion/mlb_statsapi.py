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

def _current_season() -> int:
    """Derive MLB season from today's date (or MLB_SEASON env var override)."""
    import os
    override = os.getenv("MLB_SEASON", "").strip()
    if override.isdigit():
        return int(override)
    today = date.today()
    # MLB season runs roughly April–October; use previous year Jan–Mar
    return today.year if today.month >= 3 else today.year - 1

CURRENT_SEASON: int = _current_season()

# Games in these abstract states are already underway or finished;
# fetching pitcher/lineup data for them wastes API quota.
_SKIP_STATES = {"Live", "Final", "Game Over", "Completed Early"}

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


def get_batter_vs_pitcher(batter_id: int, pitcher_id: int,
                           season: int = CURRENT_SEASON) -> dict:
    """
    Historical stats for batter_id against pitcher_id this season.
    Returns raw stat dict (plateAppearances, hits, homeRuns, etc.) or {} if
    there is no matchup data. Callers should check plateAppearances before using.
    """
    data = _get(f"/v1/people/{batter_id}/stats", {
        "stats":            "vsPlayer",
        "opposingPlayerId": pitcher_id,
        "group":            "hitting",
        "season":           season,
    })
    splits = _first_splits(data)
    return splits[0].get("stat", {}) if splits else {}


def get_pitcher_hand(person_id: int) -> str:
    """
    Fetch pitcher's throwing hand from the MLB Stats API.
    Returns 'L' or 'R'. Falls back to 'R' on error.
    """
    data = _get(f"/v1/people/{person_id}", {"hydrate": "pitchHand"})
    people = data.get("people", [])
    if not people:
        return "R"
    return people[0].get("pitchHand", {}).get("code", "R")


def get_players_batting_stats(player_ids: list[int],
                              season: int = CURRENT_SEASON) -> list[dict]:
    """
    Batch-fetch season batting stats for a confirmed lineup in ONE API call.
    Returns list of {player_id, name, bat_side, stats} in the same order as
    player_ids. Entries with no stats data have an empty stats dict.
    """
    if not player_ids:
        return []
    ids_str = ",".join(str(p) for p in player_ids)
    data = _get("/v1/people", {
        "personIds": ids_str,
        "hydrate": f"stats(group=[hitting],type=[season],season={season}),batSide",
    })
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
    """Season aggregate team pitching stats (all pitchers combined)."""
    data = _get(f"/v1/teams/{team_id}/stats",
                {"stats": "season", "group": "pitching", "season": season})
    splits = _first_splits(data)
    return splits[0].get("stat", {}) if splits else {}


def get_team_bullpen_stats(team_id: int, season: int = CURRENT_SEASON) -> dict:
    """
    Aggregate pitching stats for relievers only (innings 6+ proxy).
    Strategy: fetch active roster, batch-get each pitcher's season stats,
    exclude anyone whose GS/G ratio >= 0.40 (primarily a starter).
    Falls back to team pitching aggregate when bullpen sample < 50 BF.
    """
    roster_data = _get(f"/v1/teams/{team_id}/roster",
                        {"rosterType": "active", "season": season})
    pitcher_ids = [
        p["person"]["id"]
        for p in roster_data.get("roster", [])
        if p.get("position", {}).get("type") == "Pitcher"
        and p.get("person", {}).get("id")
    ]
    if not pitcher_ids:
        return get_team_pitching_stats(team_id, season)

    ids_str = ",".join(str(pid) for pid in pitcher_ids)
    data = _get("/v1/people", {
        "personIds": ids_str,
        "hydrate": f"stats(group=[pitching],type=[season],season={season})",
    })

    agg: dict[str, int] = {}
    relievers_found = 0
    for person in data.get("people", []):
        stat: dict = {}
        for grp in (person.get("stats") or []):
            splits = grp.get("splits", [])
            if splits:
                stat = splits[0].get("stat", {})
                break
        bf = stat.get("battersFaced", 0) or 0
        g  = stat.get("gamesPitched", 0) or 0
        gs = stat.get("gamesStarted", 0) or 0
        if bf < 10 or (g > 0 and gs / g >= 0.40):
            continue
        relievers_found += 1
        for key in ("battersFaced", "strikeOuts", "baseOnBalls", "hitBatsmen",
                    "hits", "homeRuns", "doubles", "triples"):
            agg[key] = agg.get(key, 0) + (stat.get(key) or 0)

    if relievers_found == 0 or agg.get("battersFaced", 0) < 50:
        logger.warning("Team {} bullpen insufficient ({} relievers, {} BF) — using team aggregate",
                       team_id, relievers_found, agg.get("battersFaced", 0))
        return get_team_pitching_stats(team_id, season)

    logger.debug("Team {} bullpen: {} relievers, {} BF aggregated", team_id, relievers_found, agg.get("battersFaced"))
    return agg


def get_recent_batting_games(person_id: int, last_n: int = 15,
                              season: int = CURRENT_SEASON) -> dict:
    """
    Aggregate raw batting counts from a player's last N regular-season games.
    Returns a stat dict compatible with _team_batting_to_rates().
    Returns {} if no game log data is available.
    """
    data = _get(f"/v1/people/{person_id}/stats", {
        "stats": "gameLog", "group": "hitting",
        "season": season, "gameType": "R",
    })
    splits = _first_splits(data)
    if not splits:
        return {}
    recent = splits[-last_n:]
    agg: dict[str, int] = {}
    for sp in recent:
        s = sp.get("stat", {})
        for key in ("atBats", "hits", "doubles", "triples", "homeRuns",
                    "baseOnBalls", "hitByPitch", "strikeOuts"):
            agg[key] = agg.get(key, 0) + (s.get(key) or 0)
    return agg


def get_recent_pitching_games(person_id: int, last_n: int = 3,
                               season: int = CURRENT_SEASON) -> dict:
    """
    Aggregate raw pitching counts from a starter's last N outings.
    Returns a stat dict compatible with _pitcher_stats_to_rates().
    Returns {} if no game log data is available.
    """
    data = _get(f"/v1/people/{person_id}/stats", {
        "stats": "gameLog", "group": "pitching",
        "season": season, "gameType": "R",
    })
    splits = _first_splits(data)
    if not splits:
        return {}
    recent = splits[-last_n:]
    agg: dict[str, int] = {}
    for sp in recent:
        s = sp.get("stat", {})
        for key in ("battersFaced", "strikeOuts", "baseOnBalls", "hitBatsmen",
                    "hits", "homeRuns", "doubles", "triples"):
            agg[key] = agg.get(key, 0) + (s.get(key) or 0)
    return agg


def get_live_feed(game_pk: str) -> dict:
    """GUMBO live game feed — full game state including in-game weather."""
    return _get(f"/v1.1/game/{game_pk}/feed/live")


def get_boxscore(game_pk: str) -> dict:
    return _get(f"/v1/game/{game_pk}/boxscore")


def assemble_pregame_bundle(game_date: date | None = None) -> list[dict]:
    """
    Full pre-game data bundle for every pre-game scheduled game on a date.
    Skips games that are already Live or Final to avoid wasted API calls.
    """
    games = get_schedule(game_date)
    logger.info("Found {} MLB games for {}", len(games), game_date or date.today())

    for game in games:
        state = game.get("status", "")
        if state in _SKIP_STATES:
            logger.info("Skipping {} @ {} (status={})",
                        game.get("away_team", "?"), game.get("home_team", "?"), state)
            continue

        for side in ("home", "away"):
            pid = game.get(f"{side}_pitcher_id")
            if pid:
                game[f"{side}_pitcher_stats"]  = get_player_stats(pid, "pitching")
                game[f"{side}_pitcher_splits"] = get_player_splits(pid, "pitching")
                game[f"{side}_pitcher_hand"]   = get_pitcher_hand(pid)
                game[f"{side}_pitcher_recent"] = get_recent_pitching_games(pid)
                time.sleep(0.15)

            tid = game.get(f"{side}_team_id")
            if tid:
                game[f"{side}_team_batting"]  = get_team_batting_stats(tid)
                game[f"{side}_team_pitching"] = get_team_pitching_stats(tid)
                game[f"{side}_team_bullpen"]  = get_team_bullpen_stats(tid)
                time.sleep(0.15)

            lineup_ids = game.get(f"{side}_lineup_ids", [])
            if lineup_ids:
                game[f"{side}_lineup_stats"] = get_players_batting_stats(lineup_ids)
                logger.info("{} {} confirmed lineup: {} players fetched",
                            game.get(f"{side}_team", side), side, len(lineup_ids))
                time.sleep(0.15)
            else:
                game[f"{side}_lineup_stats"] = []

    return games
