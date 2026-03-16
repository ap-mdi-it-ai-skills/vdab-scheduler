from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(_ENV_PATH)

DEFAULT_ANTWERP_POSTCODES = [
    "2000",
    "2018",
    "2020",
    "2030",
    "2040",
    "2050",
    "2060",
    "2100",
    "2130",
    "2140",
    "2150",
    "2170",
    "2180",
    "2600",
    "2610",
    "2660",
]


def _parse_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_csv(value: str | None, default: list[str]) -> list[str]:
    if not value:
        return default
    return [item.strip() for item in value.split(",") if item.strip()]


@dataclass(frozen=True)
class AppConfig:
    database_url: str
    vdab_client_id: str
    vdab_client_secret: str
    vdab_ibm_client_id: str
    vdab_env: str
    daily_sync_enabled: bool
    daily_sync_cron: str
    daily_sync_timezone: str
    daily_sync_run_on_startup: bool
    max_search_vacancies_per_request: int
    log_level: str
    historical_start_date: date
    antwerp_postcodes: list[str]
    junior_experience_codes: list[str]
    ict_job_domain: str

    @classmethod
    def from_env(cls) -> "AppConfig":
        database_url = os.getenv("DATABASE_URL") or os.getenv("SUPABASE_DB_URL")
        required = {
            "DATABASE_URL or SUPABASE_DB_URL": database_url,
            "VDAB_CLIENT_ID": os.getenv("VDAB_CLIENT_ID"),
            "VDAB_CLIENT_SECRET": os.getenv("VDAB_CLIENT_SECRET"),
            "VDAB_IBM_CLIENT_ID": os.getenv("VDAB_IBM_CLIENT_ID"),
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ValueError(f"Missing required environment variables: {', '.join(missing)}")

        return cls(
            database_url=database_url or "",
            vdab_client_id=os.getenv("VDAB_CLIENT_ID", ""),
            vdab_client_secret=os.getenv("VDAB_CLIENT_SECRET", ""),
            vdab_ibm_client_id=os.getenv("VDAB_IBM_CLIENT_ID", ""),
            vdab_env=os.getenv("VDAB_ENV", "production").lower(),
            daily_sync_enabled=_parse_bool(os.getenv("DAILY_SYNC_ENABLED"), True),
            daily_sync_cron=os.getenv("DAILY_SYNC_CRON", "0 10 * * *"),
            daily_sync_timezone=os.getenv("DAILY_SYNC_TIMEZONE", "Europe/Brussels"),
            daily_sync_run_on_startup=_parse_bool(
                os.getenv("DAILY_SYNC_RUN_ON_STARTUP"),
                True,
            ),
            max_search_vacancies_per_request=int(
                os.getenv("MAX_SEARCH_VACANCIES_PER_REQUEST", "100")
            ),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            historical_start_date=date.fromisoformat(
                os.getenv("HISTORICAL_START_DATE", "2025-06-24")
            ),
            antwerp_postcodes=_parse_csv(
                os.getenv("VDAB_POSTCODES"),
                DEFAULT_ANTWERP_POSTCODES,
            ),
            junior_experience_codes=_parse_csv(
                os.getenv("VDAB_EXPERIENCE_CODES"),
                ["1", "2", "3"],
            ),
            ict_job_domain=os.getenv("VDAB_JOB_DOMAIN", "JOBCAT10"),
        )
