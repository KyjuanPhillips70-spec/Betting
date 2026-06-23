"""
ESPN hidden JSON API — injuries, scoreboard, and game summaries.
No API key required. Endpoints documented via community reverse-engineering.
"""
from __future__ import annotations
import time
import requests
from datetime import date
from loguru import logger

CORE_BASE = "https://sports.core.api.espn.com/v2"
SITE_BASE = "https://site.api.espn.com/apis/site/v2"

_SESSION = requests.Session()
_SESSION.headers["User-Agent"] = "BettingBot/1.0 (personal research)"

_SOCCER_SLUGS = {
    "world_cup":  "fifa.world",
    "epl":        "eng.1",
    "la_liga":    "esp.1",
    "bundesliga": "ger.1",
    "serie_a":    "ita.1",
    "ligue1":     "fra.1",
    "ucl":        "uefa.champions",
    "mls":        "usa.1",
}


def _get(url: str, params: dict | None = None, retries: int = 3) -> dict:
    for attempt in range(retries):
        try:
            r = _SESSION.get(url, params=params, timeout=10)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            wait = 2 ** attempt
            logger.warning("ESPN error (attempt {}): {} — sleeping {}s", attempt + 1, e, wait)
            time.sleep(wait)
    return {}


def get_mlb_injuries(team_id: int) -> list[dict]:
    """Fetch active injury list for an MLB team."""
    url = f"{CORE_BASE}/sports/baseball/leagues/mlb/teams/{team_id}/injuries"
    data = _get(url)
    injuries = []
    for item in data.get("items", []):
        ref = item.get("$ref", "")
        if not ref:
            continue
        detail = _get(ref)
        athlete = detail.get("athlete", {})
        injuries.append({
            "player_id":   str(athlete.get("id", "")),
            "player_name": athlete.get("displayName", ""),
            "status":      detail.get("status", ""),
            "description": detail.get("longComment", detail.get("shortComment", "")),
            "team_id":     team_id,
        })
    return injuries


def get_mlb_scoreboard(game_date: date | None = None) -> list[dict]:
    d = (game_date or date.today()).strftime("%Y%m%d")
    data = _get(f"{SITE_BASE}/sports/baseball/mlb/scoreboard", {"dates": d})
    return data.get("events", [])


def get_soccer_scoreboard(league_key: str, game_date: date | None = None) -> list[dict]:
    slug = _SOCCER_SLUGS.get(league_key, league_key)
    d = (game_date or date.today()).strftime("%Y%m%d")
    data = _get(f"{SITE_BASE}/sports/soccer/{slug}/scoreboard", {"dates": d})
    return data.get("events", [])


def get_soccer_standings(league_key: str) -> list[dict]:
    """
    Fetch season/tournament standings and return per-team goal stats.
    Returns list of: {team_name, games_played, goals_for, goals_against}
    Used to derive attack/defense ratings for the Poisson soccer model.
    For the World Cup this returns group-stage table data.
    """
    slug = _SOCCER_SLUGS.get(league_key, league_key)
    data = _get(f"{SITE_BASE}/sports/soccer/{slug}/standings")

    def _stat(stats_list: list, *names: str) -> float:
        for s in stats_list:
            if s.get("name") in names:
                return float(s.get("value", 0))
        return 0.0

    results: list[dict] = []

    def _parse_entries(entries: list) -> None:
        for entry in entries:
            team = entry.get("team", {})
            name = (team.get("displayName") or team.get("name", "")).strip()
            stats = entry.get("stats", [])
            gp = _stat(stats, "gamesPlayed", "GP")
            gf = _stat(stats, "pointsFor", "goalsFor", "GF")
            ga = _stat(stats, "pointsAgainst", "goalsAgainst", "GA")
            if gp > 0 and name:
                results.append({
                    "team_name":     name,
                    "games_played":  int(gp),
                    "goals_for":     gf,
                    "goals_against": ga,
                })

    # ESPN nests some tables under "children" (e.g. World Cup groups, MLS conferences)
    children = data.get("children", [])
    if children:
        for child in children:
            entries = (child.get("standings", {}).get("entries", [])
                       or child.get("entries", []))
            if entries:
                _parse_entries(entries)
    else:
        entries = (data.get("standings", {}).get("entries", [])
                   or data.get("entries", []))
        _parse_entries(entries)

    return results


def get_game_summary(sport: str, league: str, event_id: str) -> dict:
    return _get(f"{SITE_BASE}/sports/{sport}/{league}/summary", {"event": event_id})


def get_all_mlb_injuries(team_ids: list[int]) -> list[dict]:
    all_injuries: list[dict] = []
    for tid in team_ids:
        all_injuries.extend(get_mlb_injuries(tid))
        time.sleep(0.25)
    return all_injuries
