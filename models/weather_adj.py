"""
Weather adjustments for MLB PA outcome rates.
Temperature and wind are the dominant factors; humidity is a minor correction.
"""
from __future__ import annotations
import math
from loguru import logger


def _wind_effect_on_hr(wind_speed_mph: float, wind_dir_deg: float,
                        park_orientation_deg: float) -> float:
    """
    HR rate multiplier from wind speed/direction relative to the outfield.
    park_orientation_deg: compass bearing from home plate toward center field.
    ~4% HR change per 5 mph of aligned wind.
    """
    wind_toward_deg = (wind_dir_deg + 180) % 360
    diff = abs(wind_toward_deg - park_orientation_deg)
    if diff > 180:
        diff = 360 - diff
    alignment = math.cos(math.radians(diff))   # +1 = straight out, -1 = straight in
    effect = 1.0 + alignment * wind_speed_mph * 0.008
    return max(0.70, min(1.30, effect))


def _temperature_effect_on_hr(temp_f: float, baseline_f: float = 70.0) -> float:
    """~0.5% HR increase per 10°F above baseline; air density effect."""
    effect = 1.0 + (temp_f - baseline_f) * 0.0005
    return max(0.85, min(1.15, effect))


def _humidity_effect_on_hr(humidity_pct: float) -> float:
    """Humid air is slightly less dense. Effect is minor (±2%)."""
    effect = 1.0 + (humidity_pct - 50) * 0.0001
    return max(0.98, min(1.02, effect))


def get_weather_adjustments(
    temp_f: float,
    wind_speed_mph: float,
    wind_direction_deg: float,
    park_orientation_deg: float,
    humidity_pct: float = 50.0,
    is_dome: bool = False,
) -> dict[str, float]:
    """
    PA outcome rate multipliers for current weather conditions.
    Returns 1.0 for outcomes unaffected by weather.
    Renormalization is handled by the simulator after combining with park factors.
    """
    neutral = {o: 1.0 for o in ["K", "BB", "HBP", "1B", "2B", "3B", "HR", "out"]}
    if is_dome:
        return neutral

    hr_mult = (
        _wind_effect_on_hr(wind_speed_mph, wind_direction_deg, park_orientation_deg)
        * _temperature_effect_on_hr(temp_f)
        * _humidity_effect_on_hr(humidity_pct)
    )
    hit_mult = 1.0 + (hr_mult - 1.0) * 0.3   # hits affected less than HRs

    logger.debug("Weather adj: hr_mult={:.3f} hit_mult={:.3f}", hr_mult, hit_mult)
    return {**neutral, "HR": hr_mult, "1B": hit_mult, "2B": hit_mult, "3B": hit_mult}
