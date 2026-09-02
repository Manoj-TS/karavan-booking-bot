"""Application configuration, read from environment with sensible local defaults."""
from __future__ import annotations

import os
from pathlib import Path


def _data_dir() -> Path:
    d = Path(os.environ.get("BB_DATA_DIR", "data")).resolve()
    d.mkdir(parents=True, exist_ok=True)
    return d


DATA_DIR: Path = _data_dir()
DB_PATH: Path = DATA_DIR / "bookingbot.db"
DB_URL: str = f"sqlite:///{DB_PATH.as_posix()}"

# Legacy seed files (accounts.yaml / config.yaml) for the one-time import.
SEED_DIR: Path = Path(os.environ.get("BB_SEED_DIR", "seed")).resolve()

# Per-booking artifacts (captcha png, pay.html, ticket/receipt PDFs).
ARTIFACTS_DIR: Path = DATA_DIR / "artifacts"
ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)

# Per-account cached portal sessions (cookies), keyed by email.
SESSIONS_DIR: Path = DATA_DIR / "sessions"
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

BASE_URL: str = os.environ.get("BB_BASE_URL", "https://aranyavihaara.karnataka.gov.in")
SECRET_KEY: str = os.environ.get("BB_SECRET_KEY", "dev-secret-change-me")
OCR_SPACE_KEY: str = os.environ.get("BB_OCR_SPACE_KEY", "helloworld")

# Optional shared-login gate (Basic Auth) for hosted deployments. Unset (the
# local-dev default) leaves the app open, exactly as before this existed.
AUTH_USER: str = os.environ.get("BB_AUTH_USER", "")
AUTH_PASS: str = os.environ.get("BB_AUTH_PASS", "")

APP_NAME: str = "Karavan Booking Bot"
APP_VERSION: str = "0.1.0"

# DRY_RUN swaps the real portal client for an in-memory fake (no live requests).
DRY_RUN: bool = os.environ.get("BB_DRY_RUN", "").lower() in ("1", "true", "yes")
