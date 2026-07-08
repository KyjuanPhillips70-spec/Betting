"""
tests/test_weather_adj.py — unit tests for weather_adj.py (Priority 1 + 5).
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from models.weather_adj import (
    get_weather_adjustments, PARK_ORIENTATIONS, _resolve_orientation,
    TEMP_COEF_PER_F, WIND_COEF_PER_MPH, HIT_COUPLING,
)


def test_dome_returns_all_ones():
    adj = get_weather_adjustments(40.0, 20.0, 180.0, park_orientation_deg=0.0,
                                  humidity_pct=50.0, is_dome=True)
    assert all(v == 1.0 for v in adj.values())


def test_no_wind_no_temp_effect_near_neutral():
    """Near-baseline weather → all multipliers near 1.0."""
    adj = get_weather_adjustments(70.0, 0.0, 0.0, park_orientation_deg=0.0)
    assert all(0.99 <= v <= 1.01 for v in adj.values()), \
        f"Expected neutral: {adj}"


def test_high_temp_boosts_hr():
    adj_hot  = get_weather_adjustments(95.0, 0.0, 0.0, park_orientation_deg=0.0)
    adj_cold = get_weather_adjustments(45.0, 0.0, 0.0, park_orientation_deg=0.0)
    assert adj_hot["HR"] > adj_cold["HR"]


def test_tailwind_boosts_hr():
    """Wind blowing out toward CF → HR multiplier > 1."""
    # CF at 0° (north); wind FROM south (180°) → blowing toward CF
    adj = get_weather_adjustments(70.0, 15.0, 180.0, park_orientation_deg=0.0)
    assert adj["HR"] > 1.0, f"Expected HR boost, got {adj['HR']}"


def test_headwind_reduces_hr():
    """Wind blowing in from CF → HR multiplier < 1."""
    # CF at 0° (north); wind FROM north (0°) → blowing toward home plate
    adj = get_weather_adjustments(70.0, 15.0, 0.0, park_orientation_deg=0.0)
    assert adj["HR"] < 1.0, f"Expected HR reduction, got {adj['HR']}"


def test_hr_mult_bounds():
    """Combined HR multiplier must stay within the [0.50, 1.50] clamp."""
    for temp in (20.0, 110.0):
        for speed in (0.0, 50.0):
            for wind_dir in (0.0, 90.0, 180.0, 270.0):
                adj = get_weather_adjustments(temp, speed, wind_dir, park_orientation_deg=0.0)
                assert 0.50 <= adj["HR"] <= 1.50, \
                    f"HR={adj['HR']:.3f} out of bounds (temp={temp}, speed={speed}, dir={wind_dir})"


def test_hit_coupling_less_than_hr():
    """Hit multiplier should be closer to 1.0 than HR multiplier (HIT_COUPLING < 1)."""
    adj = get_weather_adjustments(95.0, 15.0, 180.0, park_orientation_deg=0.0)
    hr_dev  = abs(adj["HR"]  - 1.0)
    hit_dev = abs(adj["1B"]  - 1.0)
    assert hit_dev <= hr_dev + 1e-6, \
        f"Hit effect ({hit_dev:.4f}) exceeded HR effect ({hr_dev:.4f})"


def test_named_constants_exist_and_positive():
    assert TEMP_COEF_PER_F  > 0
    assert WIND_COEF_PER_MPH > 0
    assert 0 < HIT_COUPLING < 1


def test_park_orientations_covers_30_parks():
    """All 30 MLB parks must have an entry."""
    assert len(PARK_ORIENTATIONS) >= 30


def test_park_orientations_bearings_valid():
    """All bearings must be in [0, 360)."""
    for park, deg in PARK_ORIENTATIONS.items():
        assert 0.0 <= deg < 360.0, f"{park}: {deg} not in [0, 360)"


def test_unknown_park_uses_neutral_wind(caplog):
    """1.3 — unknown park produces neutral output (wind zeroed) + warning."""
    import logging
    with caplog.at_level(logging.WARNING):
        adj = get_weather_adjustments(70.0, 20.0, 45.0,
                                      park_name="No Such Stadium XYZ")
    # Wind zeroed → all multipliers driven only by temperature (near neutral at 70°F)
    assert all(0.95 <= v <= 1.05 for v in adj.values()), \
        f"Expected near-neutral for unknown park: {adj}"


def test_resolve_orientation_alias():
    """Alias like 'Oriole Park at Camden Yards' should resolve."""
    deg = _resolve_orientation("Oriole Park at Camden Yards")
    assert deg is not None


def test_park_name_lookup_used_when_no_explicit_deg():
    """park_name lookup gives same result as explicit orientation."""
    adj_name = get_weather_adjustments(80.0, 10.0, 200.0,
                                       park_name="Wrigley Field")
    wrigley_deg = PARK_ORIENTATIONS["Wrigley Field"]
    adj_explicit = get_weather_adjustments(80.0, 10.0, 200.0,
                                           park_orientation_deg=wrigley_deg)
    assert abs(adj_name["HR"] - adj_explicit["HR"]) < 1e-6


def test_backward_compat_explicit_orientation():
    """Existing callers that pass park_orientation_deg explicitly still work."""
    adj = get_weather_adjustments(
        temp_f=72.0,
        wind_speed_mph=5.0,
        wind_direction_deg=180.0,
        park_orientation_deg=0.0,
        humidity_pct=50.0,
        is_dome=False,
    )
    assert "HR" in adj
    assert adj["HR"] > 0.0
