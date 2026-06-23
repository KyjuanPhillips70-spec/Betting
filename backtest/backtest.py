"""
Backtesting and performance evaluation.
Log picks against closing lines; compute CLV, ROI, Brier score.
"""
from __future__ import annotations
import sqlite3
import numpy as np
import pandas as pd
from loguru import logger


def compute_clv(bet_american: float, closing_american: float) -> float:
    """
    Closing Line Value: positive = you got a better price than the close.
    CLV = close_implied - bet_implied (positive is good for the bettor).
    """
    from edge.odds_math import american_to_implied
    return american_to_implied(closing_american) - american_to_implied(bet_american)


def compute_brier_score(probs: list[float], outcomes: list[int]) -> float:
    """Lower is better (0 = perfect, 0.25 = coin-flip baseline)."""
    return float(np.mean((np.array(probs) - np.array(outcomes)) ** 2))


def compute_roi(df: pd.DataFrame) -> float:
    staked = df["stake_units"].sum()
    return df["profit_units"].sum() / staked if staked else 0.0


def generate_report(db_path: str = "betting_bot.db") -> dict:
    """
    Pull all resolved bets from the database and compute summary statistics.
    Returns dict: n_bets, win_rate, roi, avg_clv, avg_edge, brier per sport.
    """
    conn = sqlite3.connect(db_path)
    df = pd.read_sql("""
        SELECT sport, event, market, book, model_prob, fair_prob, edge,
               stake_units, ev, closing_fair_prob, clv, result, profit_units, logged_at
        FROM bets_log
        WHERE result IS NOT NULL
    """, conn)
    conn.close()

    if df.empty:
        logger.info("No resolved bets in log yet.")
        return {}

    won = df["result"] == "win"
    report: dict = {
        "n_bets":            len(df),
        "n_wins":            int(won.sum()),
        "win_rate":          float(won.mean()),
        "roi":               compute_roi(df),
        "avg_clv":           float(df["clv"].mean()) if "clv" in df.columns else None,
        "avg_edge":          float(df["edge"].mean()),
        "total_profit_units": float(df["profit_units"].sum()),
    }

    for sport in df["sport"].unique():
        sub = df[df["sport"] == sport]
        if len(sub) >= 10:
            outs = (sub["result"] == "win").astype(int).tolist()
            report[f"{sport.lower()}_brier"] = compute_brier_score(
                sub["model_prob"].tolist(), outs
            )

    logger.info("Backtest report: {}", report)
    return report


def record_closing_line(db_path: str, bet_id: int, closing_american: float,
                        result: str, profit_units: float) -> None:
    """Update a logged bet with its closing line and outcome after the game."""
    from edge.odds_math import american_to_implied
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT line FROM bets_log WHERE id=?", (bet_id,)).fetchone()
    if not row:
        conn.close()
        return
    try:
        bet_american = float(row[0].replace("+", ""))
    except (ValueError, AttributeError):
        conn.close()
        return
    clv = compute_clv(bet_american, closing_american)
    conn.execute("""
        UPDATE bets_log
        SET closing_line=?, closing_fair_prob=?, clv=?, result=?, profit_units=?
        WHERE id=?
    """, (f"{int(closing_american):+d}",
          american_to_implied(closing_american),
          clv, result, profit_units, bet_id))
    conn.commit()
    conn.close()
