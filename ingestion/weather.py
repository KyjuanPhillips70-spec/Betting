"""
Open-Meteo weather fetcher. Free, no API key, CC-BY 4.0.
https://open-meteo.com/en/docs
"""
from __future__ import annotations
import math
import time
import requests
from datetime import datetime
from loguru import logger

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL  = "https://archive-api.open-meteo.com/v1/archive"

_SESSION = requests.Session()

_COMPASS = [
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
]


def _deg_to_compass(deg: float) -> str:
    return _COMPASS[round(deg / 22.5) % 16]


def _get(url: str, params: dict, retries: int = 3) -> dict:
    for attempt in range(retries):
        try:
            r = _SESSION.get(url, params=params, timeout=15)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            wait = 2 ** attempt
            logger.warning("Weather API error (attempt {}): {} — sleeping {}s", attempt + 1, e, wait)
            time.sleep(wait)
    return {}


def get_hourly_forecast(lat: float, lon: float, timezone: str = "auto") -> dict:
    return _get(FORECAST_URL, {
        "latitude":  lat,
        "longitude": lon,
        "hourly": ",".join([
            "temperature_2m", "relative_humidity_2m",
            "wind_speed_10m", "wind_direction_10m", "wind_gusts_10m",
            "precipitation", "pressure_msl",
        ]),
        "wind_speed_unit":   "mph",
        "temperature_unit":  "fahrenheit",
        "timezone":          timezone,
        "forecast_days":     3,
    })


def get_game_weather(lat: float, lon: float, game_dt: datetime,
                     timezone: str = "auto") -> dict:
    """
    Return weather at game time. game_dt should be in local time.
    Returns temperature (F), wind speed (mph), wind direction, humidity, precipitation.
    """
    data = get_hourly_forecast(lat, lon, timezone)
    if "hourly" not in data:
        logger.warning("No weather data for lat={} lon={}", lat, lon)
        return {}

    times = data["hourly"].get("time", [])
    target = game_dt.strftime("%Y-%m-%dT%H:00")

    idx = next((i for i, t in enumerate(times) if t == target), None)
    if idx is None and times:
        target_ts = game_dt.timestamp()
        idx = min(range(len(times)),
                  key=lambda i: abs(datetime.fromisoformat(times[i]).timestamp() - target_ts))
    if idx is None:
        return {}

    h = data["hourly"]
    wind_dir = h.get("wind_direction_10m", [None])[idx]
    return {
        "temperature_f":      h.get("temperature_2m",       [None])[idx],
        "wind_speed_mph":     h.get("wind_speed_10m",        [None])[idx],
        "wind_gusts_mph":     h.get("wind_gusts_10m",        [None])[idx],
        "wind_direction_deg": wind_dir,
        "wind_direction_name": _deg_to_compass(wind_dir) if wind_dir is not None else None,
        "humidity_pct":       h.get("relative_humidity_2m", [None])[idx],
        "precipitation_mm":   h.get("precipitation",        [None])[idx],
        "pressure_msl":       h.get("pressure_msl",         [None])[idx],
    }


def get_historical_weather(lat: float, lon: float,
                            start_date: str, end_date: str) -> dict:
    """Historical weather archive — useful for backtesting."""
    return _get(ARCHIVE_URL, {
        "latitude":  lat,
        "longitude": lon,
        "start_date": start_date,
        "end_date":   end_date,
        "hourly": "temperature_2m,wind_speed_10m,wind_direction_10m,precipitation",
        "wind_speed_unit":  "mph",
        "temperature_unit": "fahrenheit",
    })
