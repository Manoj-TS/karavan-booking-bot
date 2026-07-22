"""Settings API read/update tests."""


def test_settings_defaults(client):
    r = client.get("/api/settings")
    assert r.status_code == 200
    body = r.json()
    assert body["proxy_enabled"] is False
    assert body["ip_cooldown_days"] == 1
    assert body["proxy_country"] == "IN"


def test_settings_update_partial(client):
    r = client.put("/api/settings", json={
        "booking_phone_number": "9876543210",
        "proxy_enabled": True,
        "proxy_user": "u1",
        "proxy_pass": "p1",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["booking_phone_number"] == "9876543210"
    assert body["proxy_enabled"] is True
    # Untouched fields keep defaults.
    assert body["proxy_country"] == "IN"
    # Persisted across requests.
    r2 = client.get("/api/settings")
    assert r2.json()["proxy_user"] == "u1"
