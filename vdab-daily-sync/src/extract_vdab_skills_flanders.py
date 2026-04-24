import json
import os
import re
import time
import argparse
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx
import psycopg2
from psycopg2 import sql
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv
from openai import OpenAI


load_dotenv()

# Source/target tables
SOURCE_TABLE = "vdab_vacancies_flanders"
TEMPLATE_TABLE = "vdab_vacancies_with_skills"
TARGET_TABLE = "vdab_vacancies_flanders_skills"

# Processing config
PAGE_SIZE = 50
MAX_RETRIES = 3
RETRY_BASE_WAIT = 5
REQUEST_DELAY = 1.0
MODEL = os.getenv("MODEL_NAME")
START_VACANCY_ID = os.getenv("START_VACANCY_ID")

# Sliding window config
CHUNK_SIZE = 2000
CHUNK_OVERLAP = 500
LLM_TIMEOUT = 90.0

VDAB_TYPE_MAP = {
    "technischecompetentie": "technischecompetentie",
    "softskill": "softskill",
    "talen": "taal",
    "studie": "studie",
    "rijbewijs": "rijbewijs",
}

ID_COLUMN_CANDIDATES = ["vacature_id", "vacancy_id", "id", "vdab_id"]

llm = OpenAI(
    base_url=os.getenv("BASE_URL"),
    api_key=os.getenv("API_KEY"),
    http_client=httpx.Client(
        verify=False,
        timeout=LLM_TIMEOUT,
    ),
)


LLM_SYSTEM_PROMPT = """\
Extract ONLY explicitly mentioned technical IT tools from the job vacancy text.

Include specific proper nouns of:
- Programming languages
- Software Tools & Databases
- Frameworks & Libraries
- Hardware components & Protocols

CRITICAL RULES:
1. NO CONCEPTS: Do NOT extract theory, soft skills, diplomas, or descriptive sentences (e.g., skip words like "teamplayer", "analytical", "communication").
2. NO DUTCH: Skip all Dutch words.
3. EXPAND ABBREVIATIONS: If you extract an abbreviation, always write out its full canonical name if you are 100% sure what it means in an IT context. For example: extract "JS" as "JavaScript", "AWS" as "Amazon Web Services", and "K8s" as "Kubernetes".
4. Output EXACTLY a valid JSON array of strings using double quotes.

Example format:
["TechA", "ToolB", "FrameworkC"]
"""


def _normalize_database_url(database_url: str) -> str:
    parsed = urlparse(database_url)
    query_params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    hostname = (parsed.hostname or "").lower()
    is_local_host = hostname in {"localhost", "127.0.0.1", "::1", "db"}

    if not is_local_host and "sslmode" not in query_params:
        query_params["sslmode"] = "require"

    if query_params:
        return urlunparse(parsed._replace(query=urlencode(query_params)))
    return database_url


def _get_database_url() -> str:
    database_url = os.getenv("SUPABASE_DB_URL") or os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("SUPABASE_DB_URL or DATABASE_URL is required for table creation")
    return _normalize_database_url(database_url)


def _connect_database() -> psycopg2.extensions.connection:
    return psycopg2.connect(_get_database_url(), connect_timeout=10)


def _parse_start_vacancy_id(value: str | None) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        parsed_value = int(value)
    except ValueError as exc:
        raise ValueError(f"START_VACANCY_ID must be an integer, got: {value}") from exc
    if parsed_value < 0:
        raise ValueError("START_VACANCY_ID must be >= 0")
    return parsed_value


def _get_table_columns(conn: psycopg2.extensions.connection, table_name: str) -> set[str]:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = %s
            """,
            (table_name,),
        )
        return {row[0] for row in cursor.fetchall()}


def ensure_target_table_exists() -> tuple[set[str], str | None]:
    """
    Create target table as a structural clone of TEMPLATE_TABLE if needed.

    Returns target columns and the best identifier column for dedup checks.
    """
    with _connect_database() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                sql.SQL("CREATE TABLE IF NOT EXISTS {} (LIKE {} INCLUDING ALL)").format(
                    sql.Identifier(TARGET_TABLE),
                    sql.Identifier(TEMPLATE_TABLE),
                )
            )
        conn.commit()

        target_columns = _get_table_columns(conn, TARGET_TABLE)

    id_column = next((col for col in ID_COLUMN_CANDIDATES if col in target_columns), None)
    if id_column is None:
        print(
            "Geen ID kolom uit kandidaten gevonden in target table;"
            "skip bestaande-rij check"
        )

    return target_columns, id_column


def mark_source_processed(vacancy_id: int) -> None:
    """Markeer de bronvacature als verwerkt nadat de skills succesvol zijn opgeslagen."""
    query = sql.SQL("UPDATE {} SET skills_processed = TRUE WHERE id = %s").format(
        sql.Identifier(SOURCE_TABLE)
    )
    with _connect_database() as conn:
        with conn.cursor() as cursor:
            cursor.execute(query, (vacancy_id,))
        conn.commit()


def parse_vdab_skills(profiel_vereisten: str) -> list[dict]:
    if not profiel_vereisten:
        return []

    try:
        items = json.loads(profiel_vereisten)
    except (json.JSONDecodeError, TypeError):
        return []

    skills = []
    for item in items:
        raw_type = item.get("type", "overig")
        mapped_type = VDAB_TYPE_MAP.get(raw_type, "overig")
        label = item.get("label", "").split("\n")[0].strip()

        if not label:
            continue

        if raw_type == "talen":
            score = item.get("score", {}).get("label", "")
            label = f"{label} ({score})" if score else label

        if raw_type == "studie":
            niveau = item.get("diplomaNiveau", {}).get("label", "")
            label = f"{niveau}: {label}" if niveau else label

        skills.append(
            {
                "naam": label,
                "type": mapped_type,
                "vdab_code": item.get("code"),
            }
        )

    return skills


def _parse_json_list(raw_text: str) -> list[str]:
    if not raw_text:
        return []

    text_clean = re.sub(r"<think>.*?</think>", "", raw_text, flags=re.DOTALL)
    text_clean = re.sub(r"```json", "", text_clean, flags=re.IGNORECASE)
    text_clean = re.sub(r"```", "", text_clean)
    text_clean = text_clean.replace("[JSON]", "").strip()

    start = text_clean.find("[")
    end = text_clean.rfind("]")

    if start == -1 or end == -1 or end <= start:
        return []

    json_str = text_clean[start : end + 1]

    try:
        parsed = json.loads(json_str)
        return [s for s in parsed if isinstance(s, str)]
    except json.JSONDecodeError:
        content = text_clean[start + 1 : end]
        return [
            item.strip().strip("'").strip('"')
            for item in content.split(",")
            if item.strip()
        ]


def _extract_from_chunk(chunk_text: str) -> list[str]:
    prompt = f"Job vacancy text:\n\n{chunk_text}"

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = llm.chat.completions.create(
                model=MODEL,
                messages=[
                    {"role": "system", "content": LLM_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                temperature=0,
            )
            raw = resp.choices[0].message.content or ""
            raw_list = _parse_json_list(raw)

            skills = [
                s.strip()
                for s in raw_list
                if isinstance(s, str) and len(s.split()) <= 3
            ]

            if skills:
                print(f"{len(skills)} skills gevonden in chunk")

            return skills

        except Exception as exc:
            error_str = str(exc)
            wait_match = re.search(r"try again in (\d+)m([\d.]+)s", error_str)
            if wait_match:
                wait = int(wait_match.group(1)) * 60 + float(wait_match.group(2)) + 5
            else:
                wait = RETRY_BASE_WAIT * (2 ** (attempt - 1))

            print(f"Chunk LLM fout (poging {attempt}/{MAX_RETRIES}): {exc}")
            if attempt < MAX_RETRIES:
                print(f"Wacht {wait:.0f}s...")
                time.sleep(wait)

    print(f"Chunk gefaald na {MAX_RETRIES} pogingen")
    return []


def extract_llm_skills(text: str) -> list[str]:
    if len(text) < 50:
        return []

    all_skills = set()
    chunks_processed = 0
    start = 0

    print(f"Totale tekst: {len(text)} chars")

    while start < len(text):
        end = min(start + CHUNK_SIZE, len(text))
        chunk = text[start:end]

        if len(chunk) < 100:
            break

        chunks_processed += 1
        print(
            f"Chunk {chunks_processed}: chars {start}-{end} ({len(chunk)} chars)..."
        )

        chunk_skills = _extract_from_chunk(chunk)
        all_skills.update(chunk_skills)

        start += CHUNK_SIZE - CHUNK_OVERLAP

        if chunks_processed > 1:
            time.sleep(REQUEST_DELAY)

    unique_skills = list(all_skills)
    print(
        f"{chunks_processed} chunks verwerkt → {len(unique_skills)} unieke skills"
    )
    return unique_skills


def fetch_page(last_seen_id: int | None, start_vacancy_id: int | None) -> list[dict]:
    if last_seen_id is None:
        if start_vacancy_id is None:
            query = sql.SQL("SELECT * FROM {} ORDER BY id LIMIT %s").format(
                sql.Identifier(SOURCE_TABLE)
            )
            params = (PAGE_SIZE,)
        else:
            query = sql.SQL("SELECT * FROM {} WHERE id >= %s ORDER BY id LIMIT %s").format(
                sql.Identifier(SOURCE_TABLE)
            )
            params = (start_vacancy_id, PAGE_SIZE)
    else:
        query = sql.SQL("SELECT * FROM {} WHERE id > %s ORDER BY id LIMIT %s").format(
            sql.Identifier(SOURCE_TABLE)
        )
        params = (last_seen_id, PAGE_SIZE)

    with _connect_database() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]


def _source_row_id(vacancy: dict) -> str | int | None:
    for col in ID_COLUMN_CANDIDATES:
        if col in vacancy and vacancy[col] is not None:
            return vacancy[col]
    return None


def _is_already_processed(vacancy: dict, id_column: str | None) -> bool:
    if not id_column:
        return False

    row_id = _source_row_id(vacancy)
    if row_id is None:
        return False

    try:
        query = sql.SQL("SELECT 1 FROM {} WHERE {} = %s LIMIT 1").format(
            sql.Identifier(TARGET_TABLE),
            sql.Identifier(id_column),
        )
        with _connect_database() as conn:
            with conn.cursor() as cursor:
                cursor.execute(query, (row_id,))
                return cursor.fetchone() is not None
    except Exception as exc:
        print(f"Kon processed-check niet doen voor id={row_id}: {exc}")
        return False


def _dedupe_skills(vdab_skills: list[dict], llm_skill_names: list[str]) -> list[dict]:
    merged = []
    seen = set()

    for skill in vdab_skills:
        key = (skill.get("naam", "").strip().lower(), skill.get("type", "overig"))
        if key in seen:
            continue
        seen.add(key)
        merged.append(
            {
                "naam": skill.get("naam", "").strip(),
                "type": skill.get("type", "overig"),
                "bron": "vdab_code",
                "vdab_code": skill.get("vdab_code"),
            }
        )

    for name in llm_skill_names:
        normalized = name.strip()
        if not normalized:
            continue
        key = (normalized.lower(), "tool")
        if key in seen:
            continue
        seen.add(key)
        merged.append(
            {
                "naam": normalized,
                "type": "tool",
                "bron": "llm_extracted",
                "vdab_code": None,
            }
        )

    return merged


def _skills_as_text_array(vdab_skills: list[dict], llm_skill_names: list[str]) -> list[str]:
    skills = []
    seen = set()

    for skill in vdab_skills:
        name = skill.get("naam", "").strip()
        key = name.lower()
        if not name or key in seen:
            continue
        seen.add(key)
        skills.append(name)

    for name in llm_skill_names:
        normalized = name.strip()
        key = normalized.lower()
        if not normalized or key in seen:
            continue
        seen.add(key)
        skills.append(normalized)

    return skills


def _build_target_payload(
    vacancy: dict,
    vdab_skills: list[dict],
    llm_skill_names: list[str],
    target_columns: set[str],
) -> dict:
    payload = {}

    if "vacature_id" in target_columns:
        payload["vacature_id"] = vacancy.get("id")
    if "vacature_titel" in target_columns:
        payload["vacature_titel"] = vacancy.get("titel")
    if "beschrijving" in target_columns:
        payload["beschrijving"] = vacancy.get("beschrijving")
    if "skills" in target_columns:
        payload["skills"] = _skills_as_text_array(vdab_skills, llm_skill_names)

    return payload


def _upsert_target_payload(payload: dict, id_column: str | None) -> None:
    if not payload:
        return

    try:
        columns = list(payload.keys())
        values = [payload[column] for column in columns]

        delete_query = None
        if id_column and id_column in payload and payload[id_column] is not None:
            delete_query = sql.SQL("DELETE FROM {} WHERE {} = %s").format(
                sql.Identifier(TARGET_TABLE),
                sql.Identifier(id_column),
            )

        insert_query = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
            sql.Identifier(TARGET_TABLE),
            sql.SQL(", ").join(sql.Identifier(column) for column in columns),
            sql.SQL(", ").join(sql.Placeholder() for _ in columns),
        )

        with _connect_database() as conn:
            with conn.cursor() as cursor:
                if delete_query is not None:
                    cursor.execute(delete_query, (payload[id_column],))
                cursor.execute(insert_query, values)
            conn.commit()
    except Exception as exc:
        print(f"Fout bij schrijven naar {TARGET_TABLE}: {exc}")


def process_vacancy(vacancy: dict, target_columns: set[str], id_column: str | None) -> bool:
    row_id = _source_row_id(vacancy)
    print(f"[{row_id}] {str(vacancy.get('titel', ''))[:60]}")

    if _is_already_processed(vacancy, id_column):
        print("Al verwerkt in target table, overslaan")
        return False

    vdab_skills = parse_vdab_skills(vacancy.get("profiel_vereisten"))
    text = "\n\n".join(
        filter(
            None,
            [
                vacancy.get("beschrijving", ""),
                vacancy.get("vrije_vereiste", ""),
            ],
        )
    )
    llm_skill_names = extract_llm_skills(text)

    payload = _build_target_payload(vacancy, vdab_skills, llm_skill_names, target_columns)
    _upsert_target_payload(payload, id_column)
    if vacancy.get("id") is not None:
        mark_source_processed(int(vacancy["id"]))

    print(
        "Skills geschreven"
        f" (VDAB: {len(vdab_skills)}, LLM: {len(llm_skill_names)}, totaal: {len(_dedupe_skills(vdab_skills, llm_skill_names))})"
    )
    return True


def main(start_vacancy_id: int | None = None) -> None:
    print("=== VDAB Flanders Skills Extractie gestart ===")
    print(f"Bron: {SOURCE_TABLE}")
    print(f"Doel: {TARGET_TABLE} (template: {TEMPLATE_TABLE})")

    if start_vacancy_id is not None:
        print(f"Start vanaf vacancy id: {start_vacancy_id}")

    target_columns, id_column = ensure_target_table_exists()

    total_processed = 0
    last_seen_id: int | None = None

    while True:
        page = fetch_page(last_seen_id, start_vacancy_id)
        if not page:
            print(
                f"\nKLAAR! Verwerking afgerond."
                f" Totaal nieuw/updated in deze run: {total_processed}."
            )
            break

        for vacancy in page:
            try:
                did_write = process_vacancy(vacancy, target_columns, id_column)
                if did_write:
                    total_processed += 1
                if vacancy.get("id") is not None:
                    last_seen_id = int(vacancy["id"])
            except Exception as exc:
                print(f"Onverwachte fout bij vacature {vacancy.get('id')}: {exc}")

            time.sleep(REQUEST_DELAY)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Extract skills for vdab_vacancies_flanders")
    parser.add_argument(
        "--start-vacancy-id",
        type=int,
        default=_parse_start_vacancy_id(START_VACANCY_ID),
        help="Start extracting from this vacancy id (inclusive).",
    )
    args = parser.parse_args()
    main(start_vacancy_id=args.start_vacancy_id)
