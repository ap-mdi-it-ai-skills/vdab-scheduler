from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class VacancyInsert:
    vdab_id: str
    vdab_referentie: int | None
    titel: str | None
    bedrijf: str | None
    beschrijving: str | None
    locatie: str | None
    postcode: str | None
    publicatie_datum: str | None
    depublicatie_datum: str | None
    ervaring_code: str | None
    ervaring_label: str | None
    profiel_vereisten: list[dict[str, Any]]
    vrije_vereiste: str | None
    ingested_at: datetime
    processed: bool = False


@dataclass(frozen=True)
class SyncResult:
    fetched: int
    inserted: int
    completed_at: datetime
