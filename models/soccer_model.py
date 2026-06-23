"""
Soccer goal-probability model.
- DixonColesModel: uses penaltyblog (recommended; pip install penaltyblog).
- SimplePoisson: plain Poisson regression fallback when penaltyblog is absent.
"""
from __future__ import annotations
import math
import numpy as np
from loguru import logger

try:
    import penaltyblog
    _HAS_PENALTY = True
except ImportError:
    _HAS_PENALTY = False
    logger.warning("penaltyblog not installed; using SimplePoisson fallback")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pmf(k: int, lam: float) -> float:
    return math.exp(-lam) * (lam ** k) / math.factorial(k)


def build_score_matrix(lambda_home: float, lambda_away: float,
                        max_goals: int = 10) -> np.ndarray:
    """Independent-Poisson score probability matrix (h x a)."""
    m = np.array([[_pmf(h, lambda_home) * _pmf(a, lambda_away)
                   for a in range(max_goals + 1)]
                  for h in range(max_goals + 1)])
    return m / m.sum()


def matrix_to_markets(m: np.ndarray) -> dict[str, float]:
    """Convert score matrix to 1X2, over/unders, and BTTS."""
    max_g = m.shape[0] - 1
    home_win = float(np.tril(m, -1).sum())
    draw     = float(np.diag(m).sum())
    away_win = float(np.triu(m, 1).sum())
    over_1_5 = over_2_5 = btts = 0.0
    for h in range(max_g + 1):
        for a in range(max_g + 1):
            p = m[h, a]
            if h + a > 1.5: over_1_5 += p
            if h + a > 2.5: over_2_5 += p
            if h > 0 and a > 0: btts += p
    return {
        "home_win": home_win, "draw": draw, "away_win": away_win,
        "over_1_5": over_1_5, "over_2_5": over_2_5, "btts": btts,
    }


# ---------------------------------------------------------------------------
# Dixon-Coles model via penaltyblog
# ---------------------------------------------------------------------------

class DixonColesModel:
    """
    Dixon-Coles Poisson model with time-decay weighting.
    Requires: pip install penaltyblog
    """

    def __init__(self):
        if not _HAS_PENALTY:
            raise ImportError("pip install penaltyblog")
        self._model = None

    def fit(self, matches_df) -> None:
        """
        Fit on a DataFrame with columns:
        home_team, away_team, home_goals, away_goals, [date].
        """
        from penaltyblog.models import DixonColes
        model = DixonColes(
            goals_home=matches_df["home_goals"],
            goals_away=matches_df["away_goals"],
            teams_home=matches_df["home_team"],
            teams_away=matches_df["away_team"],
        )
        model.fit()
        self._model = model
        logger.info("Dixon-Coles fitted on {} matches", len(matches_df))

    def predict(self, home_team: str, away_team: str,
                max_goals: int = 10) -> dict:
        if self._model is None:
            raise RuntimeError("Call .fit() first")
        try:
            pred  = self._model.predict(home_team, away_team, max_goals=max_goals)
            probs = pred.get_probabilities()
            return {
                "home_win": probs.get("home_win", 0),
                "draw":     probs.get("draw", 0),
                "away_win": probs.get("away_win", 0),
                "over_2_5": probs.get("over_2.5", 0),
                "over_1_5": probs.get("over_1.5", 0),
                "btts":     probs.get("btts", 0),
            }
        except Exception as e:
            logger.error("Dixon-Coles prediction error: {}", e)
            return {}


# ---------------------------------------------------------------------------
# Simple Poisson fallback
# ---------------------------------------------------------------------------

class SimplePoisson:
    """Basic Poisson model built from season attack/defense ratings."""

    def __init__(self, home_advantage: float = 0.25):
        self.home_advantage = home_advantage
        self._attack:  dict[str, float] = {}
        self._defense: dict[str, float] = {}
        self._league_avg: float = 1.4

    def fit(self, matches_df) -> None:
        teams = set(matches_df["home_team"].tolist() + matches_df["away_team"].tolist())
        scored:   dict[str, list] = {t: [] for t in teams}
        conceded: dict[str, list] = {t: [] for t in teams}
        for _, row in matches_df.iterrows():
            scored[row["home_team"]].append(row["home_goals"])
            scored[row["away_team"]].append(row["away_goals"])
            conceded[row["home_team"]].append(row["away_goals"])
            conceded[row["away_team"]].append(row["home_goals"])
        self._league_avg = matches_df[["home_goals", "away_goals"]].values.mean()
        for t in teams:
            self._attack[t]  = (sum(scored[t]) / len(scored[t])) / self._league_avg   if scored[t]   else 1.0
            self._defense[t] = (sum(conceded[t]) / len(conceded[t])) / self._league_avg if conceded[t] else 1.0
        logger.info("SimplePoisson fitted on {} matches, {} teams",
                    len(matches_df), len(teams))

    def predict(self, home_team: str, away_team: str, max_goals: int = 10) -> dict:
        lg = self._league_avg
        lh = self._attack.get(home_team, 1.0) * self._defense.get(away_team, 1.0) * lg * (1 + self.home_advantage)
        la = self._attack.get(away_team, 1.0) * self._defense.get(home_team, 1.0) * lg
        m  = build_score_matrix(lh, la, max_goals)
        result = matrix_to_markets(m)
        result["lambda_home"] = lh
        result["lambda_away"] = la
        return result


def get_soccer_model():
    """Return the best available model."""
    if _HAS_PENALTY:
        return DixonColesModel()
    return SimplePoisson()
