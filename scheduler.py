"""
APScheduler-based daily runner.
Runs the full pipeline on a schedule; each job calls main.run_daily_card().
"""
from __future__ import annotations
from loguru import logger

try:
    from apscheduler.schedulers.blocking import BlockingScheduler
    _HAS_APS = True
except ImportError:
    _HAS_APS = False
    logger.warning("apscheduler not installed — run: pip install APScheduler")


def start() -> None:
    if not _HAS_APS:
        raise ImportError("pip install APScheduler")

    from main import run_daily_card

    scheduler = BlockingScheduler(timezone="America/New_York")
    # Morning: after schedules / probable pitchers are posted
    scheduler.add_job(run_daily_card, "cron", hour=9,  minute=0,  id="morning")
    # Midday: lineup confirmations start arriving
    scheduler.add_job(run_daily_card, "cron", hour=12, minute=0,  id="midday")
    # Pre-evening-games: confirmed lineups, fresh weather
    scheduler.add_job(run_daily_card, "cron", hour=18, minute=0,  id="evening")

    logger.info("Scheduler running. Jobs: {}", [j.id for j in scheduler.get_jobs()])
    scheduler.start()


if __name__ == "__main__":
    start()
