"""Optional Basic Auth gate for hosted deployments."""
import base64


def _basic(user: str, pw: str) -> dict:
    token = base64.b64encode(f"{user}:{pw}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def test_auth_disabled_by_default(client):
    assert client.get("/api/settings").status_code == 200


def test_auth_gate_blocks_without_credentials(client, monkeypatch):
    import app.config as config
    monkeypatch.setattr(config, "AUTH_USER", "admin")
    monkeypatch.setattr(config, "AUTH_PASS", "secret")

    r = client.get("/")
    assert r.status_code == 401
    assert r.headers["www-authenticate"].startswith("Basic")


def test_auth_gate_rejects_wrong_credentials(client, monkeypatch):
    import app.config as config
    monkeypatch.setattr(config, "AUTH_USER", "admin")
    monkeypatch.setattr(config, "AUTH_PASS", "secret")

    r = client.get("/", headers=_basic("admin", "wrong"))
    assert r.status_code == 401


def test_auth_gate_allows_correct_credentials(client, monkeypatch):
    import app.config as config
    monkeypatch.setattr(config, "AUTH_USER", "admin")
    monkeypatch.setattr(config, "AUTH_PASS", "secret")

    r = client.get("/", headers=_basic("admin", "secret"))
    assert r.status_code == 200


def test_health_check_stays_open(client, monkeypatch):
    import app.config as config
    monkeypatch.setattr(config, "AUTH_USER", "admin")
    monkeypatch.setattr(config, "AUTH_PASS", "secret")

    assert client.get("/api/health").status_code == 200
