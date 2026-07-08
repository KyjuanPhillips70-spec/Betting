"""
Weather adjustments for MLB PA outcome rates.
Temperature and wind are the dominant factors; humidity is a minor correction.

Named constants (Priority 1.1):
  TEMP_COEF_PER_F   - HR rate change per degree F above 70°F baseline
                      Current value 0.05%/°F is deliberately conservative vs.
                      the literature value (~0.10%/°F, A. Nathan, 2008 SABR).
                      Kept conservative to avoid over-weighting a single factor.
  WIND_COEF_PER_MPH - HR rate change per MPH of perfectly aligned wind
  HIT_COUPLING      - fraction of HR factor applied to non-HR hit types.
                      Unvalidated heuristic: hits are affected less than HRs
                      by air-density changes, but no published coefficient exists.
                      0.3 is a reasonable lower bound; treat as a tunable prior.

Park orientations (Priority 1.3):
  PARK_ORIENTATIONS - home-plate → center-field bearing (degrees, compass) for
                      all 30 MLB parks. Missing park → neutral wind (1.0) + warning.
"""
from __future__ import annotations
import math
from loguru import logger

# ---------------------------------------------------------------------------
# Named constants (1.1)
# ---------------------------------------------------------------------------

# HR rate change per °F above baseline (conservative vs. ~0.10%/°F in literature)
TEMP_COEF_PER_F: float = 0.0005

# HR rate change per MPH of perfectly aligned outward wind
WIND_COEF_PER_MPH: float = 0.008

# Fraction of HR multiplier applied to hit types (unvalidated heuristic — 1.2)
HIT_COUPLING: float = 0.3

# ---------------------------------------------------------------------------
# Park orientations: home-plate → center-field bearing, degrees compass (1.3)
# ---------------------------------------------------------------------------
PARK_ORIENTATIONS: dict[str, float] = {
    # American League
    "Yankee Stadium":             0.0,    # N → CF
    "Fenway Park":               52.0,    # NE
    "Camden Yards":             105.0,    # E (slight S)
    "Tropicana Field":          210.0,    # dome — but included for completeness
    "Rogers Centre":             30.0,    # dome
    "Progressive Field":         22.0,    # NNE
    "Guaranteed Rate Field":    225.0,    # SW
    "Comerica Park":            300.0,    # WNW
    "Kauffman Stadium":         330.0,    # NNW
    "Target Field":             350.0,    # N (slight W)
    "Minute Maid Park":         340.0,    # NNW (dome panels retract)
    "Globe Life Field":         315.0,    # NW (retractable dome)
    "T-Mobile Park":             15.0,    # NNE (retractable)
    "Oakland Coliseum":         330.0,    # NNW
    "Angel Stadium":            230.0,    # SW
    # National League
    "Dodger Stadium":           330.0,    # NNW
    "Oracle Park":               20.0,    # NNE (famous wind)
    "Petco Park":               305.0,    # NW
    "Chase Field":              325.0,    # NW (retractable)
    "Coors Field":                0.0,    # N
    "American Family Field":    270.0,    # W (retractable)
    "Wrigley Field":            315.0,    # NW (famous wind)
    "Busch Stadium":            345.0,    # NNW
    "PNC Park":                   0.0,    # N
    "Great American Ball Park": 270.0,    # W
    "Citi Field":               315.0,    # NW
    "Citizens Bank Park":       340.0,    # NNW
    "Truist Park":              315.0,    # NW
    "loanDepot park":           340.0,    # NNW (retractable)
    "Nationals Park":           330.0,    # NNW
}

# Aliases: alternate names that appear in lineup data or weather feeds
_PARK_ALIASES: dict[str, str] = {
    "Oriole Park at Camden Yards":         "Camden Yards",
    "Oriole Park":                         "Camden Yards",
    "Tropicana Field":                     "Tropicana Field",
    "Rogers Centre":                       "Rogers Centre",
    "Comerica Park":                       "Comerica Park",
    "Kauffman Stadium":                    "Kauffman Stadium",
    "Guaranteed Rate Field":               "Guaranteed Rate Field",
    "Target Field":                        "Target Field",
    "Minute Maid Park":                    "Minute Maid Park",
    "Globe Life Field":                    "Globe Life Field",
    "T-Mobile Park":                       "T-Mobile Park",
    "Oakland-Alameda County Coliseum":     "Oakland Coliseum",
    "RingCentral Coliseum":                "Oakland Coliseum",
    "Angel Stadium of Anaheim":            "Angel Stadium",
    "Angel Stadium":                       "Angel Stadium",
    "Dodger Stadium":                      "Dodger Stadium",
    "Oracle Park":                         "Oracle Park",
    "AT&T Park":                           "Oracle Park",
    "Petco Park":                          "Petco Park",
    "Chase Field":                         "Chase Field",
    "Coors Field":                         "Coors Field",
    "American Family Field":               "American Family Field",
    "Miller Park":                         "American Family Field",
    "Wrigley Field":                       "Wrigley Field",
    "Busch Stadium":                       "Busch Stadium",
    "PNC Park":                            "PNC Park",
    "Great American Ball Park":            "Great American Ball Park",
    "Citi Field":                          "Citi Field",
    "Citizens Bank Park":                  "Citizens Bank Park",
    "Truist Park":                         "Truist Park",
    "SunTrust Park":                       "Truist Park",
    "loanDepot park":                      "loanDepot park",
    "Marlins Park":                        "loanDepot park",
    "Nationals Park":                      "Nationals Park",
    "Yankee Stadium":                      "Yankee Stadium",
    "Fenway Park":                         "Fenway Park",
    "Progressive Field":                   "Progressive Field",
    "Guaranteed Rate Field":               "Guaranteed Rate Field",
}


def _resolve_orientation(park_name: str) -> float | None:
    """Return orientation degrees for a park, or None if unknown."""
    canonical = _PARK_ALIASES.get(park_name, park_name)
    return PARK_ORIENTATIONS.get(canonical)


def _wind_effect_on_hr(wind_speed_mph: float, wind_dir_deg: float,
                        park_orientation_deg: float) -> float:
    """
    HR rate multiplier from wind speed/direction relative to the outfield.
    park_orientation_deg: compass bearing from home plate toward center field.
    ~4% HR change per 5 mph of aligned wind (WIND_COEF_PER_MPH = 0.008).
    """
    wind_toward_deg = (wind_dir_deg + 180) % 360
    diff = abs(wind_toward_deg - park_orientation_deg)
    if diff > 180:
        diff = 360 - diff
    alignment = math.cos(math.radians(diff))   # +1 = straight out, -1 = straight in
    effect = 1.0 + alignment * wind_speed_mph * WIND_COEF_PER_MPH
    return max(0.70, min(1.30, effect))


def _temperature_effect_on_hr(temp_f: float, baseline_f: float = 70.0) -> float:
    """
    HR rate multiplier from temperature above baseline.
    Literature value: ~1% per 10°F (A. Nathan, 2008 SABR).
    TEMP_COEF_PER_F = 0.0005 → 0.5% per 10°F (deliberately conservative).
    """
    effect = 1.0 + (temp_f - baseline_f) * TEMP_COEF_PER_F
    return max(0.85, min(1.15, effect))


def _humidity_effect_on_hr(humidity_pct: float) -> float:
    """Humid air is slightly less dense. Effect is minor (±2%)."""
    effect = 1.0 + (humidity_pct - 50) * 0.0001
    return max(0.98, min(1.02, effect))


def get_weather_adjustments(
    temp_f: float,
    wind_speed_mph: float,
    wind_direction_deg: float,
    park_name: str = "",
    park_orientation_deg: float | None = None,
    humidity_pct: float = 50.0,
    is_dome: bool = False,
) -> dict[str, float]:
    """
    PA outcome rate multipliers for current weather conditions.
    Returns 1.0 for outcomes unaffected by weather.
    Renormalization is handled by the simulator after combining with park factors.

    park_name: used to look up PARK_ORIENTATIONS when park_orientation_deg is None.
    park_orientation_deg: explicit override; takes precedence over park_name lookup.
      If both are None/unknown, neutral wind (1.0) is used and a warning is logged.

    Backward-compatible: callers that still pass park_orientation_deg as a
    positional keyword argument continue to work unchanged.
    """
    neutral = {o: 1.0 for o in ["K", "BB", "HBP", "1B", "2B", "3B", "HR", "out"]}
    if is_dome:
        return neutral

    # Resolve orientation
    if park_orientation_deg is None:
        orientation = _resolve_orientation(park_name)
        if orientation is None:
            if park_name:
                logger.warning(
                    "Park orientation unknown for '{}'; using neutral wind (1.0). "
                    "Add it to PARK_ORIENTATIONS in weather_adj.py.",
                    park_name,
                )
            orientation = 0.0  # neutral: treated as no wind effect below
            wind_speed_mph = 0.0  # zero speed → effect = 1.0 regardless of direction
    else:
        orientation = park_orientation_deg

    hr_mult = max(0.50, min(1.50,
        _wind_effect_on_hr(wind_speed_mph, wind_direction_deg, orientation)
        * _temperature_effect_on_hr(temp_f)
        * _humidity_effect_on_hr(humidity_pct)
    ))
    hit_mult = 1.0 + (hr_mult - 1.0) * HIT_COUPLING

    logger.debug("Weather adj: hr_mult={:.3f} hit_mult={:.3f}", hr_mult, hit_mult)
    return {**neutral, "HR": hr_mult, "1B": hit_mult, "2B": hit_mult, "3B": hit_mult}
