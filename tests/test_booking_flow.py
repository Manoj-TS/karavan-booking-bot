"""End-to-end DRY_RUN booking: exercises the full pausable state machine + API."""
import time

import pytest


@pytest.fixture()
def dry_client(tmp_path, monkeypatch):
    import importlib
    monkeypatch.setenv("BB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("BB_SEED_DIR", str(tmp_path / "seed"))
    monkeypatch.setenv("BB_DRY_RUN", "1")
    from fastapi.testclient import TestClient
    import app.config as config
    importlib.reload(config)
    assert config.DRY_RUN is True
    import app.db as db
    importlib.reload(db)
    import app.models  # noqa: F401
    import app.services as services
    importlib.reload(services)
    for name in ("app.booking.state", "app.booking.controller", "app.api.events",
                 "app.api.accounts", "app.api.trekkers", "app.api.treks",
                 "app.api.booking", "app.api.settings", "app.api.imports"):
        importlib.reload(importlib.import_module(name))
    import app.main as main
    importlib.reload(main)
    with TestClient(main.app) as c:
        yield c


def _wait_for(client, state, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        snap = client.get("/api/booking/status").json()
        if snap["state"] == state:
            return snap
        if snap["state"] == "failed":
            raise AssertionError(f"Booking failed: {snap.get('error')}")
        time.sleep(0.1)
    raise AssertionError(f"Timed out waiting for {state}; "
                         f"last={client.get('/api/booking/status').json()['state']}")


def _seed(client):
    trek = client.post("/api/treks", json={
        "name": "Netravathi", "portal_trek_id": 113, "district_id": 24,
        "timeslot_mapping_id": 187, "timeslot_id": 45, "check_in": "01-08-2026",
    }).json()
    client.put("/api/settings", json={"shared_default_password": "pw"})
    acc = client.post("/api/accounts", json={"email": "a@x.com", "password": "pw"}).json()
    t1 = client.post("/api/trekkers", json={"name": "One", "age": 30, "gender": "Male",
                                            "mobile_no": "9876543210", "govt_id_type": "pan",
                                            "govt_id": "ABCDE1234F"}).json()
    event = client.post("/api/events", json={
        "name": "Netra Aug 1", "trek_id": trek["id"], "check_in": "01-08-2026",
        "booking_phone": "9876543210", "trekker_ids": [t1["id"]],
    }).json()
    return trek, acc, t1, event


def test_full_dry_run_booking(dry_client):
    client = dry_client
    trek, acc, t1, event = _seed(client)

    # Plan splits the single trekker into one chunk with a suggested account.
    plan = client.get(f"/api/events/{event['id']}/plan").json()
    assert plan["needs_accounts"] == 1
    assert plan["chunks"][0]["suggested_account"]["id"] == acc["id"]

    # Start.
    r = client.post("/api/booking/start", json={
        "event_id": event["id"], "account_id": acc["id"], "trekker_ids": [t1["id"]],
    })
    assert r.status_code == 200

    # OTP pause.
    _wait_for(client, "awaiting_otp")
    # Second start while busy -> 409.
    assert client.post("/api/booking/start", json={
        "event_id": event["id"], "account_id": acc["id"], "trekker_ids": [t1["id"]]}).status_code == 409
    assert client.post("/api/booking/otp", json={"otp": "123456"}).status_code == 200

    # Captcha pause.
    snap = _wait_for(client, "awaiting_captcha")
    assert snap["payload"]["captcha_guess"] == "AB12"
    assert client.post("/api/booking/captcha", json={"value": "AB12"}).status_code == 200

    # Payment pause -> pay page available.
    _wait_for(client, "awaiting_payment")
    assert "orderId" in client.get("/api/booking/pay").text
    assert client.post("/api/booking/continue").status_code == 200

    # Completed.
    snap = _wait_for(client, "completed")
    assert snap["portal_booking_id"] == "DRYRUN999"

    # Account marked booked; event completed; a booking row exists.
    acc_after = next(a for a in client.get("/api/accounts").json() if a["id"] == acc["id"])
    assert acc_after["status"] == "booked"
    ev_after = client.get(f"/api/events/{event['id']}").json()
    assert ev_after["status"] == "complete"
    assert ev_after["booked"] == 1


def test_wrong_otp_then_retry(dry_client):
    client = dry_client
    trek, acc, t1, event = _seed(client)
    client.post("/api/booking/start", json={
        "event_id": event["id"], "account_id": acc["id"], "trekker_ids": [t1["id"]]})
    _wait_for(client, "awaiting_otp")
    client.post("/api/booking/otp", json={"otp": "000000"})  # wrong -> back to awaiting_otp
    _wait_for(client, "awaiting_otp")
    client.post("/api/booking/otp", json={"otp": "123456"})
    _wait_for(client, "awaiting_captcha")
