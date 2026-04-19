from __future__ import annotations

import json
import logging
from datetime import datetime

import psycopg2
from psycopg2.extras import execute_values

from .models import VacancyInsert

LOGGER = logging.getLogger(__name__)


class VacancyRepository:
    def __init__(self, database_url: str) -> None:
        self._database_url = database_url
        self._conn: psycopg2.extensions.connection | None = None
        self._connect()
        self._ensure_state_table()

    def _connect(self) -> None:
        self._conn = psycopg2.connect(self._database_url)
        LOGGER.info("Connected to PostgreSQL")

    def _ensure_connection(self) -> None:
        if self._conn is None or self._conn.closed != 0:
            self._connect()
            return
        with self._conn.cursor() as cursor:
            cursor.execute("SELECT 1")

    def _ensure_state_table(self) -> None:
        query = (
            "CREATE TABLE IF NOT EXISTS ingestion_state ("
            "state_key TEXT PRIMARY KEY,"
            "state_value TEXT NOT NULL,"
            "updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()"
            ")"
        )
        self._ensure_connection()
        with self._conn.cursor() as cursor:
            cursor.execute(query)
        self._conn.commit()

    def get_existing_ids(self, ids: list[str]) -> set[str]:
        if not ids:
            return set()
        self._ensure_connection()
        with self._conn.cursor() as cursor:
            cursor.execute(
                "SELECT vdab_id FROM vdab_vacatures WHERE vdab_id = ANY(%s)",
                (ids,),
            )
            return {str(row[0]) for row in cursor.fetchall()}

    def insert_vacancies(self, vacancies: list[VacancyInsert]) -> int:
        if not vacancies:
            return 0
        self._ensure_connection()
        query = """
            INSERT INTO vdab_vacatures (
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
        with self._conn.cursor() as cursor:
            execute_values(cursor, query, rows)
            inserted = cursor.rowcount
        self._conn.commit()
        return inserted

    def get_last_run_timestamp(self, key: str = "vdab_daily_last_run") -> datetime | None:
        self._ensure_connection()
        with self._conn.cursor() as cursor:
            cursor.execute(
                "SELECT state_value FROM ingestion_state WHERE state_key = %s",
                (key,),
            )
            row = cursor.fetchone()
        if not row:
            return None
        return datetime.fromisoformat(str(row[0]))

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
        self._ensure_connection()
        with self._conn.cursor() as cursor:
            cursor.execute(query, (key, timestamp.isoformat()))
        self._conn.commit()

    def close(self) -> None:
        if self._conn is None:
            return
        self._conn.close()
        self._conn = None
