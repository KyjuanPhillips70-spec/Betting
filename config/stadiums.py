"""
MLB stadium coordinates and timezones for weather lookups.
"""
from __future__ import annotations

STADIUMS: dict[str, dict] = {
    "Yankee Stadium":           {"lat": 40.8296, "lon": -73.9262, "tz": "America/New_York"},
    "Fenway Park":              {"lat": 42.3467, "lon": -71.0972, "tz": "America/New_York"},
    "Rogers Centre":            {"lat": 43.6414, "lon": -79.3894, "tz": "America/Toronto"},
    "Camden Yards":             {"lat": 39.2838, "lon": -76.6218, "tz": "America/New_York"},
    "Tropicana Field":          {"lat": 27.7682, "lon": -82.6534, "tz": "America/New_York"},
    "Guaranteed Rate Field":    {"lat": 41.8300, "lon": -87.6338, "tz": "America/Chicago"},
    "Progressive Field":        {"lat": 41.4962, "lon": -81.6852, "tz": "America/New_York"},
    "Comerica Park":            {"lat": 42.3390, "lon": -83.0485, "tz": "America/New_York"},
    "Kauffman Stadium":         {"lat": 39.0517, "lon": -94.4803, "tz": "America/Chicago"},
    "Target Field":             {"lat": 44.9817, "lon": -93.2781, "tz": "America/Chicago"},
    "Angel Stadium":            {"lat": 33.8003, "lon": -117.8827, "tz": "America/Los_Angeles"},
    "Oakland Coliseum":         {"lat": 37.7516, "lon": -122.2005, "tz": "America/Los_Angeles"},
    "Minute Maid Park":         {"lat": 29.7572, "lon": -95.3555, "tz": "America/Chicago"},
    "T-Mobile Park":            {"lat": 47.5914, "lon": -122.3325, "tz": "America/Los_Angeles"},
    "Globe Life Field":         {"lat": 32.7473, "lon": -97.0836, "tz": "America/Chicago"},
    "Citi Field":               {"lat": 40.7571, "lon": -73.8458, "tz": "America/New_York"},
    "Citizens Bank Park":       {"lat": 39.9061, "lon": -75.1665, "tz": "America/New_York"},
    "Nationals Park":           {"lat": 38.8730, "lon": -77.0074, "tz": "America/New_York"},
    "Truist Park":              {"lat": 33.8908, "lon": -84.4677, "tz": "America/New_York"},
    "loanDepot park":           {"lat": 25.7781, "lon": -80.2197, "tz": "America/New_York"},
    "Wrigley Field":            {"lat": 41.9484, "lon": -87.6553, "tz": "America/Chicago"},
    "Great American Ball Park": {"lat": 39.0979, "lon": -84.5077, "tz": "America/New_York"},
    "American Family Field":    {"lat": 43.0284, "lon": -87.9712, "tz": "America/Chicago"},
    "PNC Park":                 {"lat": 40.4469, "lon": -80.0057, "tz": "America/New_York"},
    "Busch Stadium":            {"lat": 38.6226, "lon": -90.1928, "tz": "America/Chicago"},
    "Dodger Stadium":           {"lat": 34.0739, "lon": -118.2400, "tz": "America/Los_Angeles"},
    "Petco Park":               {"lat": 32.7076, "lon": -117.1570, "tz": "America/Los_Angeles"},
    "Oracle Park":              {"lat": 37.7786, "lon": -122.3893, "tz": "America/Los_Angeles"},
    "Chase Field":              {"lat": 33.4453, "lon": -112.0667, "tz": "America/Phoenix"},
    "Coors Field":              {"lat": 39.7559, "lon": -104.9942, "tz": "America/Denver"},
}


def get_stadium(venue_name: str) -> dict:
    """Look up stadium info with fuzzy-match fallback."""
    if venue_name in STADIUMS:
        return STADIUMS[venue_name]
    vl = venue_name.lower()
    for name, info in STADIUMS.items():
        if vl in name.lower() or name.lower() in vl:
            return info
    return {"lat": None, "lon": None, "tz": "America/New_York"}
