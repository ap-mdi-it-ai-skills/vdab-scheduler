## Vereisten

- Python 3.11+
- Toegang tot PostgreSQL/Supabase
- VDAB API credentials

## Setup (lokaal)

1. Ga naar deze map:

```powershell
cd vdab-daily-sync
```

2. Maak en activeer een virtual environment:

```powershell
python -m venv .venv
.\.venv\Scripts\activate
```

3. Installeer dependencies:

```powershell
pip install -r requirements.txt
```

4. Maak je omgevingsbestand:

```powershell
copy .env.example .env
```

5. Vul in `.env` je echte waarden in:

- `SUPABASE_DB_URL`
- `VDAB_CLIENT_ID`
- `VDAB_CLIENT_SECRET`
- `VDAB_IBM_CLIENT_ID`

## Starten

```powershell
python -m src.app
```

## Flanders Skills Backfill

Gebruik dit voor de statische tabel `vdab_vacancies_flanders`.

```powershell
python -m src.extract_vdab_skills_flanders
```

Vanaf een specifieke vacancy id:

```powershell
python -m src.extract_vdab_skills_flanders --start-vacancy-id 5000
```

Dit script:

- leest vacatures uit `vdab_vacancies_flanders`
- maakt `vdab_vacancies_flanders_skills` aan als clone van `vdab_vacancies_with_skills` (indien nodig)
- schrijft de geëxtraheerde skills naar `vdab_vacancies_flanders_skills`
- start standaard vanaf het begin van de tabel als je geen `--start-vacancy-id` meegeeft
- gebruikt `--start-vacancy-id` of `START_VACANCY_ID` om verderop in de tabel te beginnen

Standaard:

- draait de scheduler dagelijks (`DAILY_SYNC_CRON=0 10 * * *`)
- timezone is `Europe/Brussels`
- bij opstart wordt direct een sync uitgevoerd (`DAILY_SYNC_RUN_ON_STARTUP=true`)

## 24/7 op server

Gebruik Docker Compose zodat de service automatisch herstart.

```powershell
cd vdab-daily-sync
copy .env.example .env
docker compose up -d --build
```

Check logs:

```powershell
docker compose logs -f vdab-daily-sync
```

Stoppen:

```powershell
docker compose down
```
