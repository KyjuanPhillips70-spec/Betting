"""
Soccer data fetchers:
  - football-data.org  (free: 12 competitions, 10 req/min)
  - API-Football / API-Sports (free: 100 req/day)
"""
from __future__ import annotations
import os
import time
import requests
from loguru import logger

FD_BASE   = "https://api.football-data.org/v4"
APIFF_BASE = "https://v3.football.api-sports.io"

FD_KEY   = os.getenv("FOOTBALL_DATA_KEY", "")
APIFF_KEY = os.getenv("API_FOOTBALL_KEY", "")


class FootballDataClient:
    """football-data.org client. Hard rate limit: 10 req/min."""

    COMP_IDS = {
        "epl": "PL", "ucl": "CL", "la_liga": "PD",
        "bundesliga": "BL1", "serie_a": "SA", "ligue1": "FL1",
    }

    def __init__(self):
        self._session = requests.Session()
        self._session.headers.update({
            "X-Auth-Token": FD_KEY,
            "User-Agent":   "BettingBot/1.0",
        })
        self._last_req = 0.0

    def _get(self, path: str, params: dict | None = None) -> dict:
        elapsed = time.monotonic() - self._last_req
        if elapsed < 6.1:           # respect 10 req/min
            time.sleep(6.1 - elapsed)
        self._last_req = time.monotonic()
        url = f"{FD_BASE}{path}"
        for attempt in range(3):
            try:
                r = self._session.get(url, params=params, timeout=15)
                r.raise_for_status()
                return r.json()
            except requests.RequestException as e:
                wait = 2 ** attempt
                logger.warning("football-data error (attempt {}): {}", attempt + 1, e)
                time.sleep(wait)
        return {}

    def get_matches(self, competition: str, date_from: str, date_to: str) -> list[dict]:
        comp = self.COMP_IDS.get(competition, competition)
        data = self._get(f"/competitions/{comp}/matches",
                         {"dateFrom": date_from, "dateTo": date_to, "status": "SCHEDULED"})
        return data.get("matches", [])

    def get_standings(self, competition: str) -> list[dict]:
        comp = self.COMP_IDS.get(competition, competition)
        data = self._get(f"/competitions/{comp}/standings")
        return data.get("standings", [{}])[0].get("table", [])

    def get_team_matches(self, team_id: int, limit: int = 10) -> list[dict]:
        data = self._get(f"/teams/{team_id}/matches",
                         {"status": "FINISHED", "limit": limit})
        return data.get("matches", [])


class APIFootballClient:
    """API-Football (API-Sports) client. Free: 100 req/day."""

    def __init__(self):
        self._session = requests.Session()
        self._session.headers["x-apisports-key"] = APIFF_KEY
        self._req_count = 0

    def _get(self, path: str, params: dict | None = None) -> dict:
        if self._req_count >= 90:
            logger.warning("API-Football: nearing daily limit ({} used)", self._req_count)
        url = f"{APIFF_BASE}{path}"
        for attempt in range(3):
            try:
                r = self._session.get(url, params=params, timeout=15)
                self._req_count += 1
                r.raise_for_status()
                return r.json()
            except requests.RequestException as e:
                wait = 2 ** attempt
                logger.warning("API-Football error (attempt {}): {}", attempt + 1, e)
                time.sleep(wait)
        return {}

    def get_fixtures(self, league_id: int, season: int,
                     date_str: str | None = None) -> list[dict]:
        params: dict = {"league": league_id, "season": season}
        if date_str:
            params["date"] = date_str
        return self._get("/fixtures", params).get("response", [])

    def get_fixture_stats(self, fixture_id: int) -> list[dict]:
        return self._get("/fixtures/statistics",
                         {"fixture": fixture_id}).get("response", [])

    def get_injuries(self, league_id: int, season: int,
                     fixture_id: int | None = None) -> list[dict]:
        params: dict = {"league": league_id, "season": season}
        if fixture_id:
            params["fixture"] = fixture_id
        return self._get("/injuries", params).get("response", [])

    def get_predictions(self, fixture_id: int) -> dict:
        results = self._get("/predictions", {"fixture": fixture_id}).get("response", [])
        return results[0] if results else {}

    def get_standings(self, league_id: int, season: int) -> list[dict]:
        data = self._get("/standings", {"league": league_id, "season": season})
        try:
            return data["response"][0]["league"]["standings"][0]
        except (KeyError, IndexError):
            return []
