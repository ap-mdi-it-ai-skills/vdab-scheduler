from __future__ import annotations

import logging
import sys

from .config import AppConfig
from .repository import VacancyRepository
from .scheduler import start_scheduler
from .service import IngestionService
from .vdab_client import VdabClient


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def main() -> None:
    config = AppConfig.from_env()
    configure_logging(config.log_level)
    logger = logging.getLogger(__name__)

    client = VdabClient(
        client_id=config.vdab_client_id,
        client_secret=config.vdab_client_secret,
        ibm_client_id=config.vdab_ibm_client_id,
        env=config.vdab_env,
    )
    repository = VacancyRepository(config.database_url)
    service = IngestionService(config, client, repository)

    try:
        if config.daily_sync_enabled:
            start_scheduler(config, service)
        else:
            result = service.run_once()
            logger.info("One-off sync complete: fetched=%s inserted=%s", result.fetched, result.inserted)
    except KeyboardInterrupt:
        logger.info("Stopping scheduler")
    finally:
        repository.close()


if __name__ == "__main__":
    main()
