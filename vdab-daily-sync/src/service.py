from __future__ import annotations

import logging
import re
from html import unescape
from datetime import date, datetime
from typing import Any

from .config import AppConfig
from .models import SyncResult, VacancyInsert
from .repository import VacancyRepository
from .vdab_client import VdabClient

LOGGER = logging.getLogger(__name__)
HTML_TAG_PATTERN = re.compile(r"<[^>]+>")
WHITESPACE_PATTERN = re.compile(r"\s+")
BULLET_PATTERN = re.compile(r"[\u00B7\u2022\u2023\u2043\u204C\u204D\u2219\u25AA-\u25FF\u2605\u2606]")
LINE_BULLET_PREFIX_PATTERN = re.compile(r"(?m)^\s*[-*+]\s+")
EMOJI_PATTERN = re.compile(
    "["
    "\u2600-\u26FF"
    "\u2700-\u27BF"
    "\uFE0F"
    "\U0001F300-\U0001F5FF"
    "\U0001F600-\U0001F64F"
    "\U0001F680-\U0001F6FF"
    "\U0001F700-\U0001F77F"
    "\U0001F780-\U0001F7FF"
    "\U0001F800-\U0001F8FF"
    "\U0001F900-\U0001F9FF"
    "\U0001FA00-\U0001FAFF"
    "\U0001FB00-\U0001FBFF"
    "\U0001F1E6-\U0001F1FF"
    "]",
    flags=re.UNICODE,
)


class IngestionService:
    def __init__(
        self,
        config: AppConfig,
        vdab_client: VdabClient,
        repository: VacancyRepository,
    ) -> None:
        self._config = config
        self._vdab_client = vdab_client
        self._repository = repository

    def _compute_sinds_days(self, since_timestamp: datetime | None) -> int:
        if since_timestamp is None:
            return max((date.today() - self._config.historical_start_date).days, 1)
        return max((date.today() - since_timestamp.date()).days + 1, 1)

    def _search_params(self, sinds_days: int, vanaf: int) -> dict[str, Any]:
        return {
            "jobdomein": self._config.ict_job_domain,
            "postcode": self._config.antwerp_postcodes,
            "ervaring": self._config.junior_experience_codes,
            "sinds": sinds_days,
            "aantal": self._config.max_search_vacancies_per_request,
            "vanaf": vanaf,
            "sorteerveld": "PUBLICATIE_DATUM",
        }

    def _extract_internal_id(self, item: dict[str, Any]) -> str | None:
        reference = item.get("vacatureReferentie", {})
        internal_id = reference.get("interneReferentie")
        return str(internal_id) if internal_id else None

    def _strip_html_tags(self, value: str | None) -> str | None:
        if value is None:
            return None
        unescaped = unescape(value)
        without_tags = HTML_TAG_PATTERN.sub(" ", unescaped)
        without_line_bullets = LINE_BULLET_PREFIX_PATTERN.sub("", without_tags)
        without_bullets = BULLET_PATTERN.sub(" ", without_line_bullets)
        without_emojis = EMOJI_PATTERN.sub(" ", without_bullets)
        return WHITESPACE_PATTERN.sub(" ", without_emojis).strip()

    def fetch_filtered_vacancies(self, since_timestamp: datetime | None) -> list[dict[str, Any]]:
        sinds_days = self._compute_sinds_days(since_timestamp)
        page_size = self._config.max_search_vacancies_per_request
        details: list[dict[str, Any]] = []
        offset = 0

        while True:
            response = self._vdab_client.search_vacancies(self._search_params(sinds_days, offset))
            results = response.get("resultaten", [])
            total_results = int(response.get("totaalAantalResultaten", 0))
            if not results:
                break

            ids = [vacancy_id for item in results if (vacancy_id := self._extract_internal_id(item))]
            existing_ids = self._repository.get_existing_ids(ids)
            for vacancy_id in [item_id for item_id in ids if item_id not in existing_ids]:
                detail = self._vdab_client.get_vacancy_detail(vacancy_id)
                if detail:
                    details.append(detail)

            LOGGER.info(
                "Fetched page offset=%s, page_results=%s, details=%s",
                offset,
                len(results),
                len(details),
            )
            if offset + len(results) >= total_results:
                break
            offset += page_size

        return details

    def _to_insert_model(self, vacancy: dict[str, Any]) -> VacancyInsert | None:
        reference = vacancy.get("vacatureReferentie", {})
        internal_id = reference.get("interneReferentie")
        if not internal_id:
            return None

        functie = vacancy.get("functie", {})
        profiel = vacancy.get("profiel", {})
        ervaring = functie.get("beroepsprofiel", {}).get("ervaring", {})
        leverancier = vacancy.get("leverancier", {})
        adres = vacancy.get("tewerkstellingsadres", {})

        return VacancyInsert(
            vdab_id=str(internal_id),
            vdab_referentie=reference.get("vdabReferentie"),
            titel=functie.get("functieTitel"),
            bedrijf=leverancier.get("naam"),
            beschrijving=self._strip_html_tags(functie.get("omschrijving")),
            locatie=adres.get("gemeente"),
            postcode=adres.get("postcode"),
            publicatie_datum=vacancy.get("publicatieDatum"),
            depublicatie_datum=vacancy.get("depublicatieDatum"),
            ervaring_code=str(ervaring.get("code")) if ervaring.get("code") else None,
            ervaring_label=ervaring.get("label"),
            profiel_vereisten=profiel.get("vereisten", []),
            vrije_vereiste=self._strip_html_tags(vacancy.get("vrijeVereiste")),
            ingested_at=datetime.utcnow(),
        )

    def save_new_vacancies(self, vacancies: list[dict[str, Any]]) -> int:
        inserts = [item for vacancy in vacancies if (item := self._to_insert_model(vacancy))]
        return self._repository.insert_vacancies(inserts)

    def run_once(self) -> SyncResult:
        started_at = datetime.utcnow()
        LOGGER.info("Daily vacancy sync started at %s", started_at.isoformat())
        last_run = self._repository.get_last_run_timestamp()
        vacancies = self.fetch_filtered_vacancies(last_run)
        inserted = self.save_new_vacancies(vacancies)
        completed_at = datetime.utcnow()
        self._repository.set_last_run_timestamp(completed_at)
        return SyncResult(fetched=len(vacancies), inserted=inserted, completed_at=completed_at)
