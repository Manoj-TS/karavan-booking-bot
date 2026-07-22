"""End-to-end import API tests against a temp DB."""
import importlib
import io

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # Point the app at a throwaway data dir before importing app modules.
    monkeypatch.setenv("BB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("BB_SEED_DIR", str(tmp_path / "seed"))
    import app.config as config
    importlib.reload(config)
    import app.db as db
    importlib.reload(db)
    import app.models  # noqa: F401
    import app.import_.commit as commit
    importlib.reload(commit)
    import app.api.imports as imports_api
    importlib.reload(imports_api)
    import app.main as main
    importlib.reload(main)
    with TestClient(main.app) as c:
        yield c


def test_parse_text_endpoint(client):
    r = client.post("/api/import/parse-text",
                    json={"text": "Name: Test One\nAge: 30\nMale\n9876543210\nPAN ABCDE1234F"})
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["rows"][0]["govt_id_type"] == "pan"


def test_commit_accounts_and_dedupe(client):
    rows = [
        {"email": "a@x.com", "password": "p1", "status": "available"},
        {"email": "b@x.com", "password": None, "status": "booked"},
    ]
    r = client.post("/api/import/commit/accounts", json={"rows": rows})
    assert r.status_code == 200
    assert r.json()["created"] == 2
    # Re-commit same emails with a new password -> updated, not duplicated.
    rows[0]["password"] = "p2"
    r2 = client.post("/api/import/commit/accounts", json={"rows": rows})
    assert r2.json()["updated"] == 1


def test_upload_accounts_csv(client):
    csv_bytes = b"email,password,status\nx@y.com,secret,available\nz@y.com,,booked\n"
    r = client.post(
        "/api/import/upload?kind=accounts",
        files={"file": ("accts.csv", io.BytesIO(csv_bytes), "text/csv")},
    )
    assert r.status_code == 200
    assert r.json()["count"] == 2
    emails = {row["email"] for row in r.json()["rows"]}
    assert emails == {"x@y.com", "z@y.com"}


def test_commit_trekkers_dedupe_by_govt_id(client):
    rows = [{"name": "P One", "govt_id": "ABCDE1234F", "govt_id_type": "pan",
             "age": 30, "gender": "Male", "mobile_no": "9876543210"}]
    r = client.post("/api/import/commit/trekkers", json={"rows": rows})
    assert r.json()["created"] == 1
    # Same PAN again -> update, not a second row.
    r2 = client.post("/api/import/commit/trekkers", json={"rows": rows})
    assert r2.json()["updated"] == 1
