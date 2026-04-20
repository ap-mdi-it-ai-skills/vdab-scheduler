import os
import json
from sentence_transformers import SentenceTransformer, util
from supabase import create_client
from dotenv import load_dotenv

# --- 1. SETUP ---
load_dotenv()

print("🤖 Inladen JobBERT-v2 model...")
model = SentenceTransformer("TechWolf/JobBERT-v2")
supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])

# Laad de handmatige overrides veilig in vanuit JSON
print("📖 Inladen manual overrides...")
try:
    with open("overrides.json", "r", encoding="utf-8") as f:
        raw_overrides = json.load(f)
        MANUAL_OVERRIDES = {k.lower(): v for k, v in raw_overrides.items()}
except FileNotFoundError:
    print("⚠️ Geen overrides.json gevonden, we gaan door zonder manuele mappings.")
    MANUAL_OVERRIDES = {}


def get_skill_mapping(raw_skills, taxonomy_embeddings, taxonomy_data, threshold=0.8):
    """Vertaal een lijst unieke skills naar Lightcast in bulk via JobBERT."""
    if not raw_skills:
        return {}

    raw_embeds = model.encode(
        raw_skills, convert_to_tensor=True, show_progress_bar=True
    )
    cosine_scores = util.cos_sim(raw_embeds, taxonomy_embeddings)

    results = {}
    for i, skill in enumerate(raw_skills):
        best_idx = cosine_scores[i].argmax().item()
        score = cosine_scores[i][best_idx].item()

        if score >= threshold:
            match = taxonomy_data[best_idx]
            results[skill] = {
                "lightcast_name": match["name"],
                "lightcast_id": match["id"],
                "score": round(score, 3),
            }
        else:
            results[skill] = {"lightcast_name": None, "score": round(score, 3)}
    return results


# --- 2. HAAL LIGHTCAST TAXONOMY OP ---
print("\n📚 Lightcast Taxonomy ophalen uit database...")
taxonomy = []
offset = 0
page_size = 1000

while True:
    batch = (
        supabase.table("lightcast_taxonomy")
        .select("id, name")
        .range(offset, offset + page_size - 1)
        .execute()
        .data
    )
    taxonomy.extend(batch)
    if len(batch) < page_size:
        break
    offset += page_size

tax_names = [r["name"] for r in taxonomy]
print(f"✅ {len(tax_names)} taxonomy skills geladen.")

print("🤖 Taxonomy embedden (dit duurt even)...")
tax_embeds = model.encode(tax_names, convert_to_tensor=True, show_progress_bar=True)

# Snelle lookup dictionary voor overrides
name_to_id_map = {r["name"].lower(): r["id"] for r in taxonomy}


# --- 3. HAAL VDAB SKILLS OP ---
print("\n🌍 VDAB Skills ophalen uit de database...")
# We halen alleen de skills op die we nog niet (of mislukt) genormaliseerd hebben
vdab_skills = []
offset = 0
while True:
    batch = (
        supabase.table("vdab_extracted_skills")
        .select("*")
        .is_("lightcast_name", "null")  # Alleen de nieuwe of ongekoppelde pakken
        .range(offset, offset + page_size - 1)
        .execute()
        .data
    )
    vdab_skills.extend(batch)
    if len(batch) < page_size:
        break
    offset += page_size

print(f"📊 {len(vdab_skills)} ongenormaliseerde skills gevonden.")

if not vdab_skills:
    print("✅ Alle skills zijn al genormaliseerd. Klaar!")
    exit()

# --- 4. VERZAMEL EN MATCH SKILLS ---
mapping = {}
skills_to_embed = []

for row in vdab_skills:
    skill_orig = row["naam"].strip()
    skill_lower = skill_orig.lower()  # Onzichtbare lowercase voor robuust zoeken!

    # Voorkom dubbel werk
    if skill_lower in mapping:
        continue

    # Check in je overrides via de lowercase naam
    if skill_lower in MANUAL_OVERRIDES:
        lc_name = MANUAL_OVERRIDES[skill_lower]
        mapping[skill_lower] = {
            "lightcast_name": lc_name,  # Originele Lightcast naam mét hoofdletters
            "lightcast_id": name_to_id_map.get(lc_name.lower()),
            "score": 1.0,
        }
    else:
        # Stuur wél de originele naam naar JobBERT, dat helpt de AI soms
        skills_to_embed.append(skill_orig)

# Verwerk de onbekende termen met JobBERT
if skills_to_embed:
    print(f"\n🔍 {len(skills_to_embed)} unieke termen matchen via JobBERT...")
    model_mapping = get_skill_mapping(skills_to_embed, tax_embeds, taxonomy)

    # Voeg de JobBERT resultaten toe aan onze mapping,
    # maar forceer de 'sleutel' weer naar lowercase zodat Stap 5 nooit faalt!
    for k, v in model_mapping.items():
        mapping[k.lower()] = v
else:
    print("\n🤖 Geen nieuwe skills om via AI te embedden!")


# --- 5. BATCH UPDATE NAAR DATABASE ---
print("\n💾 Data voorbereiden voor update...")
updates = []

for row in vdab_skills:
    # Pak weer de lowercase versie om hem veilig op te zoeken in onze mapping
    skill_lower = row["naam"].strip().lower()
    res = mapping.get(skill_lower, {})

    lc_name = res.get("lightcast_name")

    # We sturen alleen een update als er daadwerkelijk een match is gevonden
    if lc_name:
        row["lightcast_name"] = lc_name
        row["lightcast_id"] = res.get("lightcast_id")
        updates.append(row)

if updates:
    print(f"🚀 {len(updates)} succesvolle matches naar Supabase pushen...")
    for i in range(0, len(updates), 100):
        batch = updates[i : i + 100]
        supabase.table("vdab_extracted_skills").upsert(batch).execute()
    print("🎉 Succes! Skills zijn genormaliseerd.")
else:
    print(
        "⚠️ JobBERT kon voor deze batch geen zekere Lightcast matches vinden (score < 0.8)."
    )
