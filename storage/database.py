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
    closing_line       TEXT,
    closing_fair_prob  REAL,
    clv                REAL,
    result             TEXT,
    profit_units       REAL,
    logged_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)
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
                          edge, stake_units, ev)
    VALUES (:sport, :event, :market, :book, :line, :model_prob, :fair_prob,
            :edge, :stake_units, :ev)
    """
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
