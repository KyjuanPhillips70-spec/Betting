"""
The Odds API fetcher (https://the-odds-api.com).
Free tier: 500 credits/month. Credits used = markets × regions per call.
Conserve credits by batching and caching; only fetch odds for edge candidates.
"""
from __future__ import annotations
import os
import time
import requests
from loguru import logger

API_KEY  = os.getenv("THE_ODDS_API_KEY", "")
BASE_URL = "https://api.the-odds-api.com/v4"

_SESSION = requests.Session()

SPORT_KEYS: dict[str, str] = {
    "mlb":        "baseball_mlb",
    "epl":        "soccer_epl",
    "ucl":        "soccer_uefa_champs_league",
    "la_liga":    "soccer_spain_la_liga",
    "bundesliga": "soccer_germany_bundesliga",
    "serie_a":    "soccer_italy_serie_a",
    "ligue1":     "soccer_france_ligue_one",
    "mls":        "soccer_usa_mls",
}


def _get(path: str, params: dict | None = None, retries: int = 3):
    url = f"{BASE_URL}{path}"
    p = {"apiKey": API_KEY, **(params or {})}
    for attempt in range(retries):
        try:
            r = _SESSION.get(url, params=p, timeout=15)
            logger.debug("Odds API: used={} remaining={}",
                         r.headers.get("x-requests-used", "?"),
                         r.headers.get("x-requests-remaining", "?"))
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            wait = 2 ** attempt
            logger.warning("Odds API error (attempt {}): {} — sleeping {}s", attempt + 1, e, wait)
            time.sleep(wait)
    return []


def get_odds(sport: str, markets: str = "h2h,spreads,totals",
             regions: str = "us", odds_format: str = "american") -> list[dict]:
    """
    Fetch odds for all upcoming events in a sport.
    Credit cost = len(markets.split(',')) × len(regions.split(',')).
    """
    sport_key = SPORT_KEYS.get(sport, sport)
    result = _get(f"/sports/{sport_key}/odds", {
        "regions":    regions,
        "markets":    markets,
        "oddsFormat": odds_format,
    })
    return result if isinstance(result, list) else []


def get_event_odds(sport: str, event_id: str, markets: str,
                   regions: str = "us") -> dict:
    """Odds for a specific event (e.g. player props once you have an event_id)."""
    sport_key = SPORT_KEYS.get(sport, sport)
    result = _get(f"/sports/{sport_key}/events/{event_id}/odds", {
        "regions":    regions,
        "markets":    markets,
        "oddsFormat": "american",
    })
    return result if isinstance(result, dict) else {}


def parse_odds_to_snapshots(events: list[dict], sport: str) -> list[dict]:
    """
    Flatten an odds-API response into snapshot dicts ready for storage.
    Each dict: game_pk, sport, market, book, outcome, price (American), point.
    """
    snapshots: list[dict] = []
    for event in events:
        game_pk = event.get("id", "")
        for bookie in event.get("bookmakers", []):
            book = bookie.get("title", "")
            for market in bookie.get("markets", []):
                market_key = market.get("key", "")
                for outcome in market.get("outcomes", []):
                    snapshots.append({
                        "game_pk": game_pk,
                        "sport":   sport,
                        "market":  market_key,
                        "book":    book,
                        "outcome": outcome.get("name", ""),
                        "price":   outcome.get("price"),
                        "point":   outcome.get("point"),
                    })
    return snapshots
