"""
SQLite database layer — tables, migrations, and helper functions.
"""
from __future__ import annotations
import sqlite3
import os
from contextlib import contextmanager
from loguru import logger

DB_PATH = os.getenv("DB_PATH", "betting_bot.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    sport       TEXT NOT NULL,
    game_pk     TEXT UNIQUE,
    home_team   TEXT,
    away_team   TEXT,
    game_date   TEXT,
    game_time   TEXT,
    venue       TEXT,
    status      TEXT,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS player_stats (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id   TEXT,
    season      INTEGER,
    stat_group  TEXT,
    stat_type   TEXT,
    value       REAL,
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(player_id, season, stat_group, stat_type)
);

CREATE TABLE IF NOT EXISTS odds_snapshots (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    game_pk        TEXT,
    sport          TEXT,
    market         TEXT,
    book           TEXT,
    outcome        TEXT,
    price          REAL,
    point          REAL,
    snapshot_time  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS weather_cache (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    venue               TEXT,
    game_date           TEXT,
    game_hour           INTEGER,
    temperature_f       REAL,
    wind_speed_mph      REAL,
    wind_direction_deg  REAL,
    wind_direction_name TEXT,
    humidity_pct        REAL,
    precipitation_mm    REAL,
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(venue, game_date, game_hour)
);

CREATE TABLE IF NOT EXISTS injuries (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id    TEXT UNIQUE,
    player_name  TEXT,
    team         TEXT,
    sport        TEXT,
    status       TEXT,
    description  TEXT,
    updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS bets_log (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    sport              TEXT,
    event              TEXT,
    market             TEXT,
    book               TEXT,
    line               TEXT,
    model_prob         REAL,
    fair_prob          REAL,
    edge               REAL,
    stake_units        REAL,
    ev                 REAL,
    projected_score    TEXT,
    closing_line       TEXT,
    closing_fair_prob  REAL,
    clv                REAL,
    result             TEXT,
    profit_units       REAL,
    logged_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS wc_results_cache (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    match_date  TEXT NOT NULL,
    home_team   TEXT NOT NULL,
    away_team   TEXT NOT NULL,
    home_goals  INTEGER NOT NULL,
    away_goals  INTEGER NOT NULL,
    UNIQUE(match_date, home_team, away_team)
);

CREATE TABLE IF NOT EXISTS wc_dates_fetched (
    fetch_date  TEXT PRIMARY KEY,
    fetched_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _migrate_db() -> None:
    """
    Safe forward-only migrations for existing databases.
    SQLite only supports ADD COLUMN, so we check before altering.
    """
    with get_conn() as conn:
        # bets_log: add projected_score if absent
        cols = {row[1] for row in conn.execute("PRAGMA table_info(bets_log)").fetchall()}
        if "projected_score" not in cols:
            conn.execute("ALTER TABLE bets_log ADD COLUMN projected_score TEXT")
            logger.info("Migration: bets_log.projected_score added")

        # Ensure WC cache tables exist (CREATE TABLE IF NOT EXISTS handles new installs;
        # this handles databases created before those tables were added)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS wc_results_cache (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                match_date  TEXT NOT NULL,
                home_team   TEXT NOT NULL,
                away_team   TEXT NOT NULL,
                home_goals  INTEGER NOT NULL,
                away_goals  INTEGER NOT NULL,
                UNIQUE(match_date, home_team, away_team)
            );
            CREATE TABLE IF NOT EXISTS wc_dates_fetched (
                fetch_date  TEXT PRIMARY KEY,
                fetched_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)
    _migrate_db()
    logger.info("Database initialized at {}", DB_PATH)


def upsert_game(game: dict) -> None:
    sql = """
    INSERT INTO games (sport, game_pk, home_team, away_team, game_date, game_time, venue, status)
    VALUES (:sport, :game_pk, :home_team, :away_team, :game_date, :game_time, :venue, :status)
    ON CONFLICT(game_pk) DO UPDATE SET
        status    = excluded.status,
        home_team = excluded.home_team,
        away_team = excluded.away_team
    """
    with get_conn() as conn:
        conn.execute(sql, game)


def insert_odds_snapshot(snap: dict) -> None:
    sql = """
    INSERT INTO odds_snapshots (game_pk, sport, market, book, outcome, price, point)
    VALUES (:game_pk, :sport, :market, :book, :outcome, :price, :point)
    """
    with get_conn() as conn:
        conn.execute(sql, snap)


def insert_bet_log(bet: dict) -> int:
    sql = """
    INSERT INTO bets_log (sport, event, market, book, line, model_prob, fair_prob,
                          edge, stake_units, ev, projected_score)
    VALUES (:sport, :event, :market, :book, :line, :model_prob, :fair_prob,
            :edge, :stake_units, :ev, :projected_score)
    """
    bet = {**bet, "projected_score": bet.get("projected_score", "")}
    with get_conn() as conn:
        cur = conn.execute(sql, bet)
        return cur.lastrowid


def update_bet_result(bet_id: int, closing_line: str, closing_fair_prob: float,
                      clv: float, result: str, profit_units: float) -> None:
    sql = """
    UPDATE bets_log
    SET closing_line=?, closing_fair_prob=?, clv=?, result=?, profit_units=?
    WHERE id=?
    """
    with get_conn() as conn:
        conn.execute(sql, (closing_line, closing_fair_prob, clv,
                           result, profit_units, bet_id))


# ---------------------------------------------------------------------------
# WC results cache helpers
# ---------------------------------------------------------------------------

def get_wc_dates_fetched() -> set[str]:
    """Return set of date strings (YYYY-MM-DD) whose ESPN data is already cached."""
    with get_conn() as conn:
        rows = conn.execute("SELECT fetch_date FROM wc_dates_fetched").fetchall()
    return {row[0] for row in rows}


def save_wc_date_fetched(date_str: str) -> None:
    """Mark a date as fully fetched so we don't re-hit ESPN for past days."""
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO wc_dates_fetched (fetch_date) VALUES (?)",
            (date_str,)
        )


def get_wc_results_cached() -> list[dict]:
    """Return all cached WC match results."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT home_team, away_team, home_goals, away_goals "
            "FROM wc_results_cache ORDER BY match_date"
        ).fetchall()
    return [
        {"home_team": r[0], "away_team": r[1],
         "home_goals": r[2], "away_goals": r[3]}
        for r in rows
    ]


def save_wc_results_cache(results: list[dict]) -> None:
    """Persist new WC match results (INSERT OR IGNORE — idempotent)."""
    if not results:
        return
    with get_conn() as conn:
        conn.executemany(
            """
            INSERT OR IGNORE INTO wc_results_cache
                (match_date, home_team, away_team, home_goals, away_goals)
            VALUES (:match_date, :home_team, :away_team, :home_goals, :away_goals)
            """,
            results,
        )
