import os
import re
import json
import time
from dotenv import load_dotenv
import httpx
from openai import OpenAI
from supabase import create_client, Client
from difflib import SequenceMatcher


load_dotenv()

# Config
FORCE_REPROCESS = (
    False  # True → alle vacatures opnieuw verwerken, False → alleen nieuwe
)
PAGE_SIZE = 50  # rijen per Supabase-pagina
MAX_RETRIES = 3
RETRY_BASE_WAIT = 5  # seconden (verdubbelt per poging)
REQUEST_DELAY = 1.0  # seconden tussen LLM-calls
MODEL = os.getenv("MODEL_NAME")

# Sliding window config voor 100% skill coverage
CHUNK_SIZE = 2000  # characters per chunk
CHUNK_OVERLAP = 500  # overlap tussen chunks om skills op grenzen niet te missen
LLM_TIMEOUT = 90.0  # seconden timeout per chunk

# Fuzzy matching threshold voor skill normalisatie
FUZZY_THRESHOLD = 0.85

print("📖 Inladen manual overrides...")
try:
    with open("overrides.json", "r", encoding="utf-8") as f:
        raw_overrides = json.load(f)
        MANUAL_OVERRIDES = {k.lower(): v for k, v in raw_overrides.items()}
    print(f"✅ {len(MANUAL_OVERRIDES)} manual overrides geladen")
except FileNotFoundError:
    print("⚠️ overrides.json niet gevonden, geen manual overrides geladen")
    MANUAL_OVERRIDES = {}


# Clients
llm = OpenAI(
    base_url=os.getenv("BASE_URL"),
    api_key=os.getenv("API_KEY"),
    http_client=httpx.Client(
        verify=False,  # Negeren van SSL errors
        timeout=LLM_TIMEOUT,  # Timeout per chunk
    ),
)

supabase: Client = create_client(
    os.environ["SUPABASE_URL"],
    os.environ["SUPABASE_KEY"],
)

VDAB_TYPE_MAP = {
    "technischecompetentie": "technischecompetentie",
    "softskill": "softskill",
    "talen": "taal",
    "studie": "studie",
    "rijbewijs": "rijbewijs",
}


def parse_vdab_skills(profiel_vereisten: str) -> list[dict]:
    """Parse VDAB profiel_vereisten JSON naar skill objecten"""
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


LLM_SYSTEM_PROMPT = """\
Extract ONLY explicitly mentioned technical IT tools from the job vacancy text.

Include specific proper nouns of:
- Programming languages
- Software Tools & Databases
- Frameworks & Libraries
- Hardware components & Protocols

CRITICAL RULES:
1. NO CONCEPTS: Do NOT extract theory, soft skills, diplomas, or descriptive sentences.
2. NO DUTCH: Skip all Dutch words.
3. EXTRACT AS-IS: Output the EXACT term as written (do NOT expand abbreviations).
4. Output EXACTLY a valid JSON array of strings using double quotes.

Example format:
["TechA", "ToolB", "FrameworkC"]
"""


def extract_llm_skills(text: str) -> list[str]:
    """
    Extract skills uit vacature tekst met sliding window voor 100% coverage.

    Verwerkt tekst in overlappende chunks om geen skills te missen.
    Gebruikt set() om automatisch duplicaten te verwijderen.
    """
    if len(text) < 50:
        return []

    all_skills = set()
    chunks_processed = 0
    start = 0

    print(f"   📄 Totale tekst: {len(text)} chars")

    # Sliding window: verwerk tekst in overlappende chunks
    while start < len(text):
        end = min(start + CHUNK_SIZE, len(text))
        chunk = text[start:end]

        if len(chunk) < 100:  # Skip te kleine restjes
            break

        chunks_processed += 1
        print(
            f"   🔍 Chunk {chunks_processed}: chars {start}-{end} ({len(chunk)} chars)..."
        )

        # Call LLM voor deze chunk
        chunk_skills = _extract_from_chunk(chunk)
        all_skills.update(chunk_skills)  # set() verwijdert automatisch duplicaten

        # Volgende chunk met overlap om skills op grenzen niet te missen
        start += CHUNK_SIZE - CHUNK_OVERLAP

        # Wacht tussen chunks (rate limiting), behalve na eerste chunk
        if chunks_processed > 1:
            time.sleep(REQUEST_DELAY)

    unique_skills = list(all_skills)
    print(
        f"   ✅ {chunks_processed} chunks verwerkt → {len(unique_skills)} unieke skills"
    )
    return unique_skills


def _extract_from_chunk(chunk_text: str) -> list[str]:
    """
    Extract skills van 1 text chunk met retry logic.

    Interne helper functie voor extract_llm_skills().
    """
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

            # Post-processing: Gooi zinnen (meer dan 3 woorden) direct weg
            skills = [
                s.strip()
                for s in raw_list
                if isinstance(s, str) and len(s.split()) <= 3
            ]

            if skills:
                print(f"      ✓ {len(skills)} skills gevonden in chunk")

            return skills

        except Exception as exc:
            # Check voor Rate Limits
            error_str = str(exc)
            wait_match = re.search(r"try again in (\d+)m([\d.]+)s", error_str)
            if wait_match:
                wait = int(wait_match.group(1)) * 60 + float(wait_match.group(2)) + 5
            else:
                wait = RETRY_BASE_WAIT * (2 ** (attempt - 1))

            print(f"      ⚠️ Chunk LLM fout (poging {attempt}/{MAX_RETRIES}): {exc}")

            if attempt < MAX_RETRIES:
                print(f"      ⏳ Wacht {wait:.0f}s...")
                time.sleep(wait)

    print(f"      ❌ Chunk gefaald na {MAX_RETRIES} pogingen")
    return []


def _parse_json_list(raw_text: str) -> list[str]:
    """Parse LLM response naar list van strings, met fallback parsing"""
    if not raw_text:
        return []

    # Remove thinking tags en markdown formatting
    text_clean = re.sub(r"<think>.*?</think>", "", raw_text, flags=re.DOTALL)
    text_clean = re.sub(r"```json", "", text_clean, flags=re.IGNORECASE)
    text_clean = re.sub(r"```", "", text_clean)
    text_clean = text_clean.replace("[JSON]", "").strip()

    # Find JSON array
    start = text_clean.find("[")
    end = text_clean.rfind("]")

    if start == -1 or end == -1 or end <= start:
        return []

    json_str = text_clean[start : end + 1]

    try:
        # Probeer correcte JSON parsing
        parsed = json.loads(json_str)
        return [s for s in parsed if isinstance(s, str)]
    except json.JSONDecodeError:
        # Fallback: split op komma's
        content = text_clean[start + 1 : end]
        items = [
            item.strip().strip("'").strip('"')
            for item in content.split(",")
            if item.strip()
        ]
        return items


def normalize_skill_name(skill: str) -> str:
    """
    Normaliseer skill naam via manual overrides.

    Voorkomt bekende varianten zoals "JS" → "JavaScript"
    """
    skill_lower = skill.lower().strip()

    if skill_lower in MANUAL_OVERRIDES:
        canonical = MANUAL_OVERRIDES[skill_lower]
        if canonical != skill.strip():
            print(f"   🔄 Normaliseer: '{skill}' → '{canonical}'")
        return canonical

    return skill.strip()


def find_similar_skill(
    naam: str, type_: str, threshold: float = FUZZY_THRESHOLD
) -> str | None:
    """
    Vind bestaande skill met intelligente fuzzy matching.
    """
    existing = (
        supabase.table("vdab_extracted_skills")
        .select("id, naam")
        .eq("type", type_)
        .execute()
        .data
    )

    if not existing:
        return None

    naam_lower = naam.lower().strip()

    for skill in existing:
        skill_lower = skill["naam"].lower().strip()

        # CRITICAL: Lengte check voorkomt false positives
        len_diff = abs(len(naam_lower) - len(skill_lower))
        if len_diff > 5:  # Max 5 characters verschil toegestaan
            continue

        # Strategie 1: Substring match (Vue in Vue.js)
        if len(naam_lower) > 3 and len(skill_lower) > 3:
            if naam_lower in skill_lower or skill_lower in naam_lower:
                print(f"   🔗 Substring: '{naam}' → '{skill['naam']}'")
                return skill["id"]

        # Strategie 2: Fuzzy similarity (PostgreSQL ≈ Postgres)
        ratio = SequenceMatcher(None, naam_lower, skill_lower).ratio()
        if ratio >= threshold:
            print(f"   🔗 Fuzzy ({ratio:.2f}): '{naam}' → '{skill['naam']}'")
            return skill["id"]

    return None


def upsert_skill(naam: str, type_: str, vdab_code: str | None) -> str:
    """
    Insert of update skill met 4-staps duplicate prevention.

    BELANGRIJKE FLOW:
    1. Normaliseer naam via overrides
    2. Check exact match op genormaliseerde naam
    3. Check exact match op originele naam
    4. Check fuzzy match op ORIGINELE naam (niet genormaliseerd!)
    5. Insert nieuwe skill met genormaliseerde naam

    Waarom fuzzy op origineel?
    - Voorkomt dat "C (Programming Language)" matcht met "Python (Programming Language)"
    - De shared suffix "(Programming Language)" zou anders hoge similarity geven

    Returns skill ID of None bij fout.
    """
    original_naam = naam.strip()  # Bewaar origineel voor fuzzy matching

    payload = {"naam": original_naam, "type": type_}
    if vdab_code:
        payload["vdab_code"] = vdab_code

    try:
        if vdab_code:
            # Upsert op basis van vdab_code
            res = (
                supabase.table("vdab_extracted_skills")
                .upsert(payload, on_conflict="vdab_code")
                .execute()
            )
        else:
            # STAP 1: Normaliseer via overrides
            normalized_naam = normalize_skill_name(original_naam)

            # STAP 2: Check exact match op genormaliseerde naam
            # Dit matcht "C" met "C (Programming Language)" als die al bestaat
            existing = (
                supabase.table("vdab_extracted_skills")
                .select("id")
                .ilike("naam", normalized_naam)
                .eq("type", type_)
                .execute()
            )
            if existing.data:
                return existing.data[0]["id"]

            # STAP 3: Check exact match op origineel (case-insensitive)
            # Fallback voor skills die nog niet genormaliseerd zijn
            if normalized_naam.lower() != original_naam.lower():
                existing = (
                    supabase.table("vdab_extracted_skills")
                    .select("id")
                    .ilike("naam", original_naam)
                    .eq("type", type_)
                    .execute()
                )
                if existing.data:
                    return existing.data[0]["id"]

            # STAP 4: Check fuzzy match op ORIGINEEL (niet normalized!)
            # CRITICAL: Dit voorkomt C# vs Python false matches
            # Fuzzy werkt op "C#" vs "C", niet op "C# (Programming Language)" vs "C (Programming Language)"
            similar_id = find_similar_skill(original_naam, type_)
            if similar_id:
                return similar_id

            # STAP 5: Insert nieuwe skill met NORMALIZED naam
            payload["naam"] = normalized_naam
            res = supabase.table("vdab_extracted_skills").insert(payload).execute()

        if res.data:
            return res.data[0]["id"]
        return None
    except Exception as exc:
        print(f"   ❌ Fout bij upsert skill '{naam}': {exc}")
        return None


def link_skill_to_vacancy(vacature_id: int, skill_id: str, bron: str):
    """Koppel skill aan vacature in junction table"""
    try:
        supabase.table("vdab_vacancy_skills_legacy").upsert(
            {"vacature_id": vacature_id, "skill_id": skill_id, "bron": bron},
            on_conflict="vacature_id,skill_id",
        ).execute()
    except Exception as exc:
        print(
            f"   ❌ Fout bij koppelen skill {skill_id} aan vacature {vacature_id}: {exc}"
        )


def mark_processed(vacature_id: int):
    """Markeer vacature als verwerkt in database"""
    try:
        supabase.table("vdab_vacancies_antwerp").update({"skills_processed": True}).eq(
            "id", vacature_id
        ).execute()
    except Exception as exc:
        print(f"   ❌ Fout bij markeren vacature {vacature_id}: {exc}")


def fetch_page(offset: int) -> list[dict]:
    """Haal batch vacatures op uit database"""
    query = supabase.table("vdab_vacancies_antwerp").select(
        "id, titel, beschrijving, profiel_vereisten, vrije_vereiste, skills_processed"
    )

    if not FORCE_REPROCESS:
        # Alleen onverwerkte vacatures
        return (
            query.eq("skills_processed", False)
            .order("id")
            .limit(PAGE_SIZE)
            .execute()
            .data
        )
    else:
        # Alle vacatures (voor reprocessing)
        return query.order("id").range(offset, offset + PAGE_SIZE - 1).execute().data


def process_vacancy(vac: dict):
    """
    Verwerk één vacature: extract skills en link aan database.

    Combineert VDAB skills (uit profiel_vereisten) met LLM-extracted skills
    uit de beschrijving + vrije_vereiste tekst.
    """
    v_id = vac["id"]

    # Parse VDAB skills uit profiel_vereisten JSON
    vdab_skills = parse_vdab_skills(vac.get("profiel_vereisten"))

    # Combineer beschrijving + vrije_vereiste voor LLM extractie
    tekst = "\n\n".join(
        filter(
            None,
            [
                vac.get("beschrijving", ""),
                vac.get("vrije_vereiste", ""),
            ],
        )
    )

    # Extract skills met LLM (sliding window voor 100% coverage)
    llm_skill_names = extract_llm_skills(tekst)

    # Link VDAB skills
    linked = 0
    for s in vdab_skills:
        skill_id = upsert_skill(s["naam"], s["type"], s["vdab_code"])

        if skill_id:
            link_skill_to_vacancy(v_id, skill_id, "vdab_code")
            linked += 1

    # Link LLM-extracted skills
    for naam in llm_skill_names:
        skill_id = upsert_skill(naam, "tool", None)

        if skill_id:
            link_skill_to_vacancy(v_id, skill_id, "llm_extracted")
            linked += 1

    # Markeer als verwerkt
    mark_processed(v_id)
    print(
        f"   ✅ {linked} skills gekoppeld (VDAB: {len(vdab_skills)}, LLM: {len(llm_skill_names)})"
    )


def main():
    """Main loop: verwerk alle onverwerkte vacatures en sluit dan af"""
    print(f"=== VDAB Skills Extractie Service gestart ===")

    total_processed = 0

    while True:
        # Haal de volgende batch van 50 op
        page = fetch_page(0)

        # Als de pagina leeg is, zijn we helemaal bij!
        if not page:
            print(
                f"\n🎉 KLAAR! Alle wachtende vacatures zijn geëxtraheerd (totaal verwerkt in deze run: {total_processed})."
            )
            break

        print(f"--- {len(page)} nieuwe vacatures gevonden om te verwerken ---")
        for vac in page:
            print(f"[{vac['id']}] {vac.get('titel', '')[:60]}")
            try:
                process_vacancy(vac)
                total_processed += 1
            except Exception as exc:
                print(f"   ❌ Onverwachte fout bij vacature {vac['id']}: {exc}")

            # Wacht even om rate limits van de LLM te respecteren
            time.sleep(REQUEST_DELAY)

        print("✅ Batch klaar. Direct door naar de volgende controle...")


if __name__ == "__main__":
    main()
