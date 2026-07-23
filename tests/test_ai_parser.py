"""AI parser normalization + parse-text engine/fallback behavior."""
from app.import_.ai_parser import _normalize


def test_normalize_aadhaar_to_ration():
    rec = _normalize({"name": "Ravi Kumar", "age": "30", "gender": "M",
                      "mobile_no": "+91 98765 43210", "govt_id_type": "aadhaar",
                      "govt_id": "412345678901"})
    assert rec["name"] == "Ravi Kumar"
    assert rec["age"] == 30
    assert rec["gender"] == "Male"
    assert rec["mobile_no"] == "9876543210"
    assert rec["govt_id_type"] == "ration"  # aadhaar -> ration
    assert rec["issues"] == []


def test_normalize_detects_type_from_value():
    rec = _normalize({"name": "P", "age": None, "gender": None,
                      "mobile_no": None, "govt_id_type": None, "govt_id": "abcde1234f"})
    assert rec["govt_id_type"] == "pan"
    assert rec["govt_id"] == "ABCDE1234F"
    assert set(["age", "gender", "mobile_no"]).issubset(set(rec["issues"]))


def test_parse_text_falls_back_to_local_without_key(client):
    # No ANTHROPIC_API_KEY in the test env -> local engine.
    r = client.post("/api/import/parse-text",
                    json={"text": "Name: Test One\nAge: 30\nMale\n9876543210\nPAN ABCDE1234F"})
    assert r.status_code == 200
    body = r.json()
    assert body["engine"] == "local"
    assert body["count"] == 1
    assert body["rows"][0]["govt_id_type"] == "pan"


def test_parse_text_uses_ai_when_available(client, monkeypatch):
    import app.api.imports as imports_api
    monkeypatch.setattr(imports_api, "ai_available", lambda: True)
    monkeypatch.setattr(imports_api, "parse_trekkers_ai",
                        lambda text: [{"name": "AI Person", "age": 25, "gender": "Female",
                                       "mobile_no": "9123456780", "govt_id_type": "voter_id",
                                       "govt_id": "ABC1234567", "issues": []}])
    r = client.post("/api/import/parse-text", json={"text": "whatever"})
    body = r.json()
    assert body["engine"] == "ai"
    assert body["rows"][0]["name"] == "AI Person"


def test_parse_text_ai_error_falls_back(client, monkeypatch):
    import app.api.imports as imports_api

    def boom(text):
        raise RuntimeError("api down")

    monkeypatch.setattr(imports_api, "ai_available", lambda: True)
    monkeypatch.setattr(imports_api, "parse_trekkers_ai", boom)
    # engine=ai explicit -> falls back to local and notes the failure.
    r = client.post("/api/import/parse-text?engine=ai",
                    json={"text": "Ravi Kumar 30 M 9876543210 ABCDE1234F"})
    body = r.json()
    assert body["engine"] == "local"
    assert "api down" in (body["note"] or "")
    assert body["count"] == 1
