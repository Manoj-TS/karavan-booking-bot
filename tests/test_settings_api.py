"""Settings API read/update tests."""


def test_settings_defaults(client):
    r = client.get("/api/settings")
    assert r.status_code == 200
    body = r.json()
    assert body["captcha_mode"] == "manual"
    assert body["account_cooldown_days"] == 1


def test_settings_update_partial(client):
    r = client.put("/api/settings", json={
        "booking_phone_number": "9876543210",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["booking_phone_number"] == "9876543210"
    # Untouched fields keep defaults.
    assert body["captcha_mode"] == "manual"
    # Persisted across requests.
    r2 = client.get("/api/settings")
    assert r2.json()["booking_phone_number"] == "9876543210"
