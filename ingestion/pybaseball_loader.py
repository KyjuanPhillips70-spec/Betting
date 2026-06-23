"""
Statcast / pybaseball data loader.
Caches results to disk via pybaseball.cache to stay well within rate limits.
"""
from __future__ import annotations
import pandas as pd
from datetime import date, timedelta
from loguru import logger

try:
    import pybaseball as pb
    pb.cache.enable()
    _HAS_PB = True
except ImportError:
    _HAS_PB = False
    logger.warning("pybaseball not installed; Statcast features disabled")


def _require_pb() -> None:
    if not _HAS_PB:
        raise ImportError("pip install pybaseball")


def get_batter_statcast(player_id: int, days: int = 365) -> pd.DataFrame:
    _require_pb()
    end = date.today()
    start = end - timedelta(days=days)
    try:
        df = pb.statcast_batter(start.strftime("%Y-%m-%d"),
                                 end.strftime("%Y-%m-%d"),
                                 player_id=player_id)
        logger.info("Fetched {} Statcast rows for batter {}", len(df), player_id)
        return df
    except Exception as e:
        logger.error("Statcast batter error for {}: {}", player_id, e)
        return pd.DataFrame()


def get_pitcher_statcast(player_id: int, days: int = 365) -> pd.DataFrame:
    _require_pb()
    end = date.today()
    start = end - timedelta(days=days)
    try:
        df = pb.statcast_pitcher(start.strftime("%Y-%m-%d"),
                                  end.strftime("%Y-%m-%d"),
                                  player_id=player_id)
        logger.info("Fetched {} Statcast rows for pitcher {}", len(df), player_id)
        return df
    except Exception as e:
        logger.error("Statcast pitcher error for {}: {}", player_id, e)
        return pd.DataFrame()


def get_season_batting_stats(season: int) -> pd.DataFrame:
    _require_pb()
    try:
        return pb.batting_stats(season, season, qual=50)
    except Exception as e:
        logger.error("Batting stats error: {}", e)
        return pd.DataFrame()


def get_season_pitching_stats(season: int) -> pd.DataFrame:
    _require_pb()
    try:
        return pb.pitching_stats(season, season, qual=50)
    except Exception as e:
        logger.error("Pitching stats error: {}", e)
        return pd.DataFrame()


def get_player_id(last: str, first: str) -> dict:
    """Return MLB AM / FanGraphs / BBRef IDs for a player."""
    _require_pb()
    try:
        df = pb.playerid_lookup(last, first)
        if df.empty:
            return {}
        row = df.iloc[0]
        return {
            "mlbam":      row.get("key_mlbam"),
            "bbref":      row.get("key_bbref"),
            "fangraphs":  row.get("key_fangraphs"),
            "retro":      row.get("key_retro"),
        }
    except Exception as e:
        logger.error("Player ID lookup error: {}", e)
        return {}


def compute_batter_rates(df: pd.DataFrame) -> dict[str, float]:
    """
    Compute per-PA event rates from Statcast data.
    Returns keys: K_rate, BB_rate, HBP_rate, 1B_rate, 2B_rate, 3B_rate, HR_rate, out_rate.
    """
    if df.empty:
        return {}
    pa = df[df["events"].notna()]
    if pa.empty:
        return {}
    total = len(pa)
    event_map = {
        "strikeout": "K", "walk": "BB", "hit_by_pitch": "HBP",
        "single": "1B", "double": "2B", "triple": "3B", "home_run": "HR",
    }
    rates: dict[str, float] = {}
    for ev, key in event_map.items():
        rates[f"{key}_rate"] = float((pa["events"] == ev).sum()) / total
    rates["out_rate"] = max(0.0, 1.0 - sum(
        rates[f"{k}_rate"] for k in ["K", "BB", "HBP", "1B", "2B", "3B", "HR"]
    ))
    return rates


def compute_batter_rates_by_hand(df: pd.DataFrame) -> dict[str, dict]:
    """Split batter rates by pitcher handedness (L/R)."""
    return {h: compute_batter_rates(df[df["p_throws"] == h]) for h in ("L", "R")}


def compute_pitcher_rates_by_hand(df: pd.DataFrame) -> dict[str, dict]:
    """Split pitcher rates by batter handedness (L/R)."""
    return {h: compute_batter_rates(df[df["stand"] == h]) for h in ("L", "R")}
