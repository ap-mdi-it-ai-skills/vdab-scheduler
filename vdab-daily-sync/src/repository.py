from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import psycopg2
from psycopg2.extras import execute_values

from .models import VacancyInsert

LOGGER = logging.getLogger(__name__)

RETRYABLE_DB_ERRORS = (psycopg2.OperationalError, psycopg2.InterfaceError)


def _normalize_database_url(database_url: str) -> str:
    parsed = urlparse(database_url)
    query_params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    hostname = (parsed.hostname or "").lower()
    is_local_host = hostname in {"localhost", "127.0.0.1", "::1", "db"}

    # Supabase and most managed Postgres providers require TLS for remote hosts.
    if not is_local_host and "sslmode" not in query_params:
        query_params["sslmode"] = "require"

    if query_params:
        return urlunparse(parsed._replace(query=urlencode(query_params)))
    return database_url


class VacancyRepository:
    def __init__(self, database_url: str) -> None:
        self._database_url = _normalize_database_url(database_url)
        self._conn: psycopg2.extensions.connection | None = None
        self._connect()
        self._ensure_state_table()

    def _connect(self) -> None:
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                self._conn = psycopg2.connect(
                    self._database_url,
                    connect_timeout=10,
                    application_name="vdab-daily-sync",
                    keepalives=1,
                    keepalives_idle=30,
                    keepalives_interval=10,
                    keepalives_count=5,
                )
                LOGGER.info("Connected to PostgreSQL")
                return
            except RETRYABLE_DB_ERRORS as exc:
                last_error = exc
                self._conn = None
                if attempt == 3:
                    break
                backoff_seconds = attempt
                LOGGER.warning(
                    "PostgreSQL connection failed (attempt %s/3): %s. Retrying in %ss",
                    attempt,
                    exc,
                    backoff_seconds,
                )
                time.sleep(backoff_seconds)

        if last_error is not None:
            raise last_error

    def _ensure_connection(self) -> None:
        if self._conn is None or self._conn.closed != 0:
            self._connect()
            return
        try:
            with self._conn.cursor() as cursor:
                cursor.execute("SELECT 1")
        except RETRYABLE_DB_ERRORS:
            self._connect()

    def _run_with_retry(self, operation: callable, *, commit: bool = False):
        last_error: Exception | None = None
        for attempt in range(1, 3):
            self._ensure_connection()
            try:
                result = operation()
                if commit:
                    self._conn.commit()
                return result
            except RETRYABLE_DB_ERRORS as exc:
                last_error = exc
                if self._conn is not None and self._conn.closed == 0:
                    self._conn.rollback()
                self._connect()
                if attempt == 2:
                    break
                LOGGER.warning(
                    "Transient PostgreSQL error during operation (attempt %s/2): %s",
                    attempt,
                    exc,
                )

        if last_error is not None:
            raise last_error
        raise RuntimeError("Unexpected database operation failure")

    def _ensure_state_table(self) -> None:
        query = (
            "CREATE TABLE IF NOT EXISTS ingestion_state ("
            "state_key TEXT PRIMARY KEY,"
            "state_value TEXT NOT NULL,"
            "updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()"
            ")"
        )

        def operation() -> None:
            with self._conn.cursor() as cursor:
                cursor.execute(query)

        self._run_with_retry(operation, commit=True)

    def get_existing_ids(self, ids: list[str]) -> set[str]:
        if not ids:
            return set()

        def operation() -> set[str]:
            with self._conn.cursor() as cursor:
                cursor.execute(
                    "SELECT vdab_id FROM vdab_vacancies_antwerp WHERE vdab_id = ANY(%s)",
                    (ids,),
                )
                return {str(row[0]) for row in cursor.fetchall()}

        return self._run_with_retry(operation)

    def insert_vacancies(self, vacancies: list[VacancyInsert]) -> int:
        if not vacancies:
            return 0
        query = """
            INSERT INTO vdab_vacancies_antwerp (
                vdab_id,
                vdab_referentie,
                titel,
                bedrijf,
                beschrijving,
                locatie,
                postcode,
                publicatie_datum,
                depublicatie_datum,
                ervaring_code,
                ervaring_label,
                profiel_vereisten,
                vrije_vereiste,
                ingested_at
            ) VALUES %s
            ON CONFLICT (vdab_id) DO NOTHING
        """
        rows = [
            (
                vacancy.vdab_id,
                vacancy.vdab_referentie,
                vacancy.titel,
                vacancy.bedrijf,
                vacancy.beschrijving,
                vacancy.locatie,
                vacancy.postcode,
                vacancy.publicatie_datum,
                vacancy.depublicatie_datum,
                vacancy.ervaring_code,
                vacancy.ervaring_label,
                json.dumps(vacancy.profiel_vereisten),
                vacancy.vrije_vereiste,
                vacancy.ingested_at,
            )
            for vacancy in vacancies
        ]

        def operation() -> int:
            with self._conn.cursor() as cursor:
                execute_values(cursor, query, rows)
                return cursor.rowcount

        return self._run_with_retry(operation, commit=True)

    def get_last_run_timestamp(self, key: str = "vdab_daily_last_run") -> datetime | None:

        def operation() -> datetime | None:
            with self._conn.cursor() as cursor:
                cursor.execute(
                    "SELECT state_value FROM ingestion_state WHERE state_key = %s",
                    (key,),
                )
                row = cursor.fetchone()
            if not row:
                return None
            return datetime.fromisoformat(str(row[0]))

        return self._run_with_retry(operation)

    def set_last_run_timestamp(
        self,
        timestamp: datetime,
        key: str = "vdab_daily_last_run",
    ) -> None:
        query = """
            INSERT INTO ingestion_state (state_key, state_value, updated_at)
            VALUES (%s, %s, NOW())
            ON CONFLICT (state_key)
            DO UPDATE SET state_value = EXCLUDED.state_value, updated_at = NOW()
        """

        def operation() -> None:
            with self._conn.cursor() as cursor:
                cursor.execute(query, (key, timestamp.isoformat()))

        self._run_with_retry(operation, commit=True)

    def close(self) -> None:
        if self._conn is None:
            return
        self._conn.close()
        self._conn = None
