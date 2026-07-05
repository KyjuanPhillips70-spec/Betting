"""
MLB park factors — rate multipliers relative to league average (1.0 = neutral).
Approximated from FanGraphs / Baseball Reference multi-year park factor data.

Keys per park:
  HR_factor      : HR rate multiplier
  hit_factor     : general hit environment
  K_factor       : strikeout environment
  altitude_ft    : elevation above sea level
  is_dome        : True = weather has no effect
  orientation_deg: compass bearing from home plate to center field (for wind calc)
"""
from __future__ import annotations

PARK_FACTORS: dict[str, dict] = {
    # AL East
    "Yankee Stadium":           {"HR_factor": 1.13, "hit_factor": 1.01, "K_factor": 1.00, "altitude_ft": 55,   "is_dome": False, "orientation_deg": 340},
    "Fenway Park":              {"HR_factor": 0.90, "hit_factor": 1.05, "K_factor": 0.98, "altitude_ft": 20,   "is_dome": False, "orientation_deg": 90},
    "Rogers Centre":            {"HR_factor": 1.05, "hit_factor": 0.97, "K_factor": 1.01, "altitude_ft": 76,   "is_dome": True,  "orientation_deg": 0},
    "Camden Yards":             {"HR_factor": 1.07, "hit_factor": 1.02, "K_factor": 0.99, "altitude_ft": 36,   "is_dome": False, "orientation_deg": 60},
    "Tropicana Field":          {"HR_factor": 0.96, "hit_factor": 0.97, "K_factor": 1.02, "altitude_ft": 15,   "is_dome": True,  "orientation_deg": 0},
    # AL Central
    "Guaranteed Rate Field":    {"HR_factor": 1.04, "hit_factor": 0.99, "K_factor": 1.01, "altitude_ft": 595,  "is_dome": False, "orientation_deg": 5},
    "Progressive Field":        {"HR_factor": 0.95, "hit_factor": 0.98, "K_factor": 1.02, "altitude_ft": 650,  "is_dome": False, "orientation_deg": 30},
    "Comerica Park":            {"HR_factor": 0.92, "hit_factor": 0.99, "K_factor": 1.00, "altitude_ft": 585,  "is_dome": False, "orientation_deg": 45},
    "Kauffman Stadium":         {"HR_factor": 0.93, "hit_factor": 1.00, "K_factor": 0.99, "altitude_ft": 750,  "is_dome": False, "orientation_deg": 5},
    "Target Field":             {"HR_factor": 0.97, "hit_factor": 0.99, "K_factor": 1.00, "altitude_ft": 841,  "is_dome": False, "orientation_deg": 15},
    # AL West
    "Angel Stadium":            {"HR_factor": 0.94, "hit_factor": 0.99, "K_factor": 1.00, "altitude_ft": 160,  "is_dome": False, "orientation_deg": 355},
    "Oakland Coliseum":         {"HR_factor": 0.88, "hit_factor": 0.97, "K_factor": 1.02, "altitude_ft": 25,   "is_dome": False, "orientation_deg": 330},
    "Minute Maid Park":         {"HR_factor": 1.03, "hit_factor": 1.01, "K_factor": 0.99, "altitude_ft": 43,   "is_dome": True,  "orientation_deg": 10},
    "T-Mobile Park":            {"HR_factor": 0.91, "hit_factor": 0.99, "K_factor": 1.01, "altitude_ft": 10,   "is_dome": True,  "orientation_deg": 350},
    "Globe Life Field":         {"HR_factor": 1.01, "hit_factor": 1.00, "K_factor": 1.00, "altitude_ft": 551,  "is_dome": True,  "orientation_deg": 0},
    # NL East
    "Citi Field":               {"HR_factor": 0.97, "hit_factor": 0.98, "K_factor": 1.01, "altitude_ft": 10,   "is_dome": False, "orientation_deg": 335},
    "Citizens Bank Park":       {"HR_factor": 1.07, "hit_factor": 1.02, "K_factor": 0.99, "altitude_ft": 40,   "is_dome": False, "orientation_deg": 15},
    "Nationals Park":           {"HR_factor": 1.04, "hit_factor": 1.00, "K_factor": 1.00, "altitude_ft": 25,   "is_dome": False, "orientation_deg": 345},
    "Truist Park":              {"HR_factor": 1.05, "hit_factor": 1.01, "K_factor": 0.99, "altitude_ft": 1050, "is_dome": False, "orientation_deg": 50},
    "loanDepot park":           {"HR_factor": 0.89, "hit_factor": 0.96, "K_factor": 1.02, "altitude_ft": 10,   "is_dome": True,  "orientation_deg": 0},
    # NL Central
    "Wrigley Field":            {"HR_factor": 1.06, "hit_factor": 1.04, "K_factor": 0.97, "altitude_ft": 595,  "is_dome": False, "orientation_deg": 60},
    "Great American Ball Park": {"HR_factor": 1.09, "hit_factor": 1.02, "K_factor": 0.98, "altitude_ft": 490,  "is_dome": False, "orientation_deg": 30},
    "American Family Field":    {"HR_factor": 1.02, "hit_factor": 1.00, "K_factor": 1.00, "altitude_ft": 634,  "is_dome": True,  "orientation_deg": 0},
    "PNC Park":                 {"HR_factor": 0.96, "hit_factor": 1.00, "K_factor": 1.00, "altitude_ft": 730,  "is_dome": False, "orientation_deg": 340},
    "Busch Stadium":            {"HR_factor": 0.93, "hit_factor": 1.00, "K_factor": 1.00, "altitude_ft": 465,  "is_dome": False, "orientation_deg": 20},
    # NL West
    "Dodger Stadium":           {"HR_factor": 0.96, "hit_factor": 0.99, "K_factor": 1.01, "altitude_ft": 515,  "is_dome": False, "orientation_deg": 0},
    "Petco Park":               {"HR_factor": 0.87, "hit_factor": 0.96, "K_factor": 1.03, "altitude_ft": 17,   "is_dome": False, "orientation_deg": 315},
    "Oracle Park":              {"HR_factor": 0.89, "hit_factor": 0.97, "K_factor": 1.02, "altitude_ft": 10,   "is_dome": False, "orientation_deg": 290},
    "Chase Field":              {"HR_factor": 1.02, "hit_factor": 1.00, "K_factor": 0.99, "altitude_ft": 1082, "is_dome": True,  "orientation_deg": 0},
    "Coors Field":              {"HR_factor": 1.26, "hit_factor": 1.12, "K_factor": 0.94, "altitude_ft": 5200, "is_dome": False, "orientation_deg": 340},
}

NEUTRAL_PARK = {
    "HR_factor": 1.0, "hit_factor": 1.0, "K_factor": 1.0,
    "altitude_ft": 500, "is_dome": False, "orientation_deg": 0,
}


def get_park_factors(venue_name: str) -> dict:
    """Return park factors for a venue, with fuzzy-match fallback to neutral."""
    if venue_name in PARK_FACTORS:
        return PARK_FACTORS[venue_name]
    vl = venue_name.lower()
    for known, factors in PARK_FACTORS.items():
        if vl in known.lower() or known.lower() in vl:
            return factors
    return NEUTRAL_PARK.copy()


def park_factors_to_pa_adjustments(pf: dict) -> dict[str, float]:
    """Convert park factor dict into per-outcome PA rate multipliers."""
    return {
        "HR":  pf.get("HR_factor", 1.0),
        "1B":  pf.get("hit_factor", 1.0),
        "2B":  pf.get("hit_factor", 1.0),
        "3B":  pf.get("hit_factor", 1.0),
        "K":   pf.get("K_factor", 1.0),
        "BB":  1.0,
        "HBP": 1.0,
        "out": 1.0,
    }
