import os
import re
import json
import time
from dotenv import load_dotenv
import httpx
from openai import OpenAI
from supabase import create_client, Client


load_dotenv()

# Config
FORCE_REPROCESS = False  # True → alle vacatures opnieuw verwerken
PAGE_SIZE = 50  # rijen per Supabase-pagina
MAX_RETRIES = 3
RETRY_BASE_WAIT = 5  # seconden (verdubbelt per poging)
REQUEST_DELAY = 1.0  # seconden tussen LLM-calls
MODEL = os.getenv("MODEL_NAME")


# Clients
llm = OpenAI(
    api_key=os.environ["OPENROUTER_API_KEY"],
    base_url="https://openrouter.ai/api/v1",
    timeout=httpx.Timeout(connect=5.0, read=60.0, write=5.0, pool=5.0),
    max_retries=0,
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
You are an expert IT skill extractor building a tech ontology.
Extract ONLY concrete technical hard skills from the job vacancy text.

RULES:
- Include: programming languages, frameworks, tools, platforms, protocols, databases, cloud services, methodologies (e.g. Agile, Scrum, CI/CD).
- Exclude: soft skills, diplomas, driver's licenses, vague terms like "teamplayer" or "analytical".
- Normalize to standard English industry names: "kennis van netwerken" → "Networking", "C-sharp" → "C#".
- Atomic terms only: "Python and SQL" → ["Python", "SQL"].
- Do NOT infer or assume — only include what is explicitly mentioned.
- Return ONLY a raw JSON array of strings. No markdown, no explanation, no preamble.
- If nothing found, return [].

Example output: ["Python", "SQL", "Docker", "Azure", "Scrum"]
"""

def extract_llm_skills(text: str) -> list[str]:
    if len(text) < 50:
        return []

    prompt = f"Job vacancy text:\n\n{text}"

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
            return _parse_json_list(raw)

        except Exception as exc:
            wait = RETRY_BASE_WAIT * (2 ** (attempt - 1))
            print(
                f"   ⚠️ LLM fout (poging {attempt}/{MAX_RETRIES}): {exc} — wacht {wait}s"
            )
            if attempt < MAX_RETRIES:
                time.sleep(wait)

    print(f"   ❌ LLM gefaald na {MAX_RETRIES} pogingen")
    return []


def _parse_json_list(text: str) -> list[str]:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    text = re.sub(r"```(?:json)?", "", text).strip()
    match = re.search(r"\[.*?\]", text, re.DOTALL)
    
    if not match:
        return []
    
    try:
        result = json.loads(match.group(0))
        return [s for s in result if isinstance(s, str) and s.strip()]
    except json.JSONDecodeError:
        return []


def upsert_skill(naam: str, type_: str, vdab_code: str | None) -> str:
    payload = {"naam": naam, "type": type_}
    if vdab_code:
        payload["vdab_code"] = vdab_code

    try:
        if vdab_code:
            res = (
                supabase.table("skills")
                .upsert(payload, on_conflict="vdab_code")
                .execute()
            )
        else:
            existing = (
                supabase.table("skills")
                .select("id")
                .ilike("naam", naam)
                .eq("type", type_)
                .execute()
            )
            if existing.data:
                return existing.data[0]["id"]
            res = supabase.table("skills").insert(payload).execute()

        if res.data:
            return res.data[0]["id"]
        return None
    except Exception as exc:
        print(f"   ❌ Fout bij upsert skill '{naam}': {exc}")
        return None


def link_skill_to_vacancy(vacature_id: int, skill_id: str, bron: str):
    try:
        supabase.table("vacature_skills").upsert(
            {"vacature_id": vacature_id, "skill_id": skill_id, "bron": bron},
            on_conflict="vacature_id,skill_id",
        ).execute()
    except Exception as exc:
        print(
            f"   ❌ Fout bij koppelen skill {skill_id} aan vacature {vacature_id}: {exc}"
        )


def mark_processed(vacature_id: int):
    try:
        supabase.table("vdab_vacatures_all").update({"skills_processed": True}).eq(
            "id", vacature_id
        ).execute()
    except Exception as exc:
        print(f"   ❌ Fout bij markeren vacature {vacature_id}: {exc}")


def fetch_page(offset: int) -> list[dict]:
    query = supabase.table("vdab_vacatures_all").select(
        "id, titel, beschrijving, profiel_vereisten, vrije_vereiste, skills_processed"
    )

    if not FORCE_REPROCESS:
        # Als we processen, pakken we ALTIJD de top 50 die nog False zijn
        return (
            query.eq("skills_processed", False)
            .order("id")
            .limit(PAGE_SIZE)
            .execute()
            .data
        )
    else:
        # Alleen als we ALLES opnieuw forceren, gebruiken we de offset
        return query.order("id").range(offset, offset + PAGE_SIZE - 1).execute().data


def process_vacancy(vac: dict):
    v_id = vac["id"]
    vdab_skills = parse_vdab_skills(vac.get("profiel_vereisten"))
    tekst = "\n\n".join(
        filter(
            None,
            [
                vac.get("beschrijving", ""),
                vac.get("vrije_vereiste", ""),
            ],
        )
    )

    llm_skill_names = extract_llm_skills(tekst)
    linked = 0
    for s in vdab_skills:
        skill_id = upsert_skill(s["naam"], s["type"], s["vdab_code"])

        if skill_id:
            link_skill_to_vacancy(v_id, skill_id, "vdab_code")
            linked += 1

    for naam in llm_skill_names:
        skill_id = upsert_skill(naam, "tool", None)

        if skill_id:
            link_skill_to_vacancy(v_id, skill_id, "llm_extracted")
            linked += 1

    mark_processed(v_id)
    print(
        f"   ✅ {linked} skills gekoppeld: {len(llm_skill_names)}"
    )


def main():
    print(f"=== VDAB Skills Extractie Service gestart ===")
    print(
        f"Model: {MODEL} | FORCE_REPROCESS: {FORCE_REPROCESS} | PAGE_SIZE: {PAGE_SIZE}\n"
    )

    while True:
        # We hoeven geen offset bij te houden als we simpelweg altijd
        # de eerste 50 pakken die nog niet verwerkt zijn (skills_processed = False)
        page = fetch_page(0)

        if not page:
            print(
                "⏳ Geen onverwerkte vacatures. Wachten op nieuwe instroom (60 sec)..."
            )
            time.sleep(60)
            continue

        print(f"--- {len(page)} nieuwe vacatures gevonden om te verwerken ---")
        for vac in page:
            print(f"[{vac['id']}] {vac.get('titel', '')[:60]}")
            try:
                process_vacancy(vac)
            except Exception as exc:
                print(f"   ❌ Onverwachte fout bij vacature {vac['id']}: {exc}")

            # Wacht even om rate limits van de LLM te respecteren
            time.sleep(REQUEST_DELAY)

        print("✅ Batch klaar. Direct door naar de volgende controle...")


if __name__ == "__main__":

    main()
