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
    "epl": "eng.1", "la_liga": "esp.1", "bundesliga": "ger.1",
    "serie_a": "ita.1", "ligue1": "fra.1", "ucl": "uefa.champions",
    "mls": "usa.1",
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


def get_game_summary(sport: str, league: str, event_id: str) -> dict:
    return _get(f"{SITE_BASE}/sports/{sport}/{league}/summary", {"event": event_id})


def get_all_mlb_injuries(team_ids: list[int]) -> list[dict]:
    all_injuries: list[dict] = []
    for tid in team_ids:
        all_injuries.extend(get_mlb_injuries(tid))
        time.sleep(0.25)
    return all_injuries
