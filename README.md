# Karavan Booking Bot

A local-first web app for booking Karnataka trek tickets (`aranyavihaara.karnataka.gov.in`)
across a pool of consented accounts, over your regular network connection, with an
event/roster model, a mobile-friendly guided booking wizard, and ticket download + cancel.

> Successor to the standalone `booker.py` (booking) and `dashv2.py` (dashboard) scripts,
> now unified into one FastAPI app. The originals are kept under `legacy/` for reference.

## Quick start (local)

```powershell
# 1. Create the venv (Python 3.12)
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1

# 2. Install deps
pip install -r requirements.txt

# 3. Run
python run.py            # opens http://127.0.0.1:8000 in your browser
```

First run creates `data/bookingbot.db`. Use the **Import** screen to load accounts
(`seed/accounts.yaml`) and treks/trekkers (`seed/config.yaml`), or upload Excel/CSV.

## Hosting (Docker)

```bash
docker compose up --build      # serves on :8000, data persisted in a named volume
```

Set `BB_SECRET_KEY` (and any overrides) via a `.env` file — see `.env.example`.

## Layout

```
app/
  main.py            FastAPI app + startup
  db.py config.py    DB engine, settings
  models.py schemas.py migration.py
  portal/            booking + ticket logic (ported from booker.py / dashv2.py)
  import_/           heuristic trekker parser + spreadsheet readers
  booking/           pausable booking state machine
  api/               REST endpoints
  static/            mobile-first web UI
legacy/              original booker.py / btt.py / dashv2.py (reference only)
seed/                config.yaml / accounts.yaml (gitignored — contain secrets)
data/                SQLite DB + artifacts (gitignored)
```

## Notes

- **Secrets:** `seed/`, `data/`, and `.env` hold live credentials and personal data and are
  gitignored. Never commit them.
- **OCR:** captcha OCR pre-fill needs Tesseract (bundled in the Docker image); manual entry
  always works without it.
