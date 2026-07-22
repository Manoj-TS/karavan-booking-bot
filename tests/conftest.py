"""Shared fixtures: a TestClient bound to a throwaway DB per test."""
import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("BB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("BB_SEED_DIR", str(tmp_path / "seed"))
    # Reload config + all modules that captured config values at import time.
    import app.config as config
    importlib.reload(config)
    import app.db as db
    importlib.reload(db)
    import app.models  # noqa: F401
    import app.services as services
    importlib.reload(services)
    for name in ("app.import_.commit", "app.api.imports", "app.api.settings",
                 "app.api.events", "app.api.accounts", "app.api.trekkers",
                 "app.api.treks", "app.api.booking", "app.api.tickets",
                 "app.api.dashboard"):
        try:
            importlib.reload(importlib.import_module(name))
        except ModuleNotFoundError:
            pass
    import app.main as main
    importlib.reload(main)
    with TestClient(main.app) as c:
        yield c
