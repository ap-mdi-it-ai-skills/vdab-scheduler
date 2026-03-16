from __future__ import annotations

import logging

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import AppConfig
from .service import IngestionService

LOGGER = logging.getLogger(__name__)


def _run_sync(service: IngestionService) -> None:
    result = service.run_once()
    LOGGER.info(
        "Sync completed: fetched=%s inserted=%s completed_at=%s",
        result.fetched,
        result.inserted,
        result.completed_at.isoformat(),
    )


def start_scheduler(config: AppConfig, service: IngestionService) -> None:
    scheduler = BlockingScheduler(timezone=config.daily_sync_timezone)
    trigger = CronTrigger.from_crontab(
        config.daily_sync_cron,
        timezone=config.daily_sync_timezone,
    )
    scheduler.add_job(
        func=lambda: _run_sync(service),
        trigger=trigger,
        id="vdab_daily_sync",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )

    LOGGER.info(
        "Scheduler started with cron='%s' timezone='%s'",
        config.daily_sync_cron,
        config.daily_sync_timezone,
    )
    if config.daily_sync_run_on_startup:
        _run_sync(service)
    scheduler.start()
