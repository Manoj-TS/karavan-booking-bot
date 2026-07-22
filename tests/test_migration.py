"""Smoke tests for the seed importer against the real legacy files."""
from pathlib import Path

from app.migration import parse_accounts, parse_treks, parse_trekkers

SEED = Path(__file__).resolve().parent.parent / "seed"


def _read(name: str) -> str:
    return (SEED / name).read_text(encoding="utf-8")


def test_accounts_yaml_clean_list():
    accts = parse_accounts(_read("accounts.yaml"))
    emails = {a["email"] for a in accts}
    assert "raadha@gmail.com" in emails
    assert len(accts) >= 50
    # Distinct password preserved.
    anant = next(a for a in accts if a["email"] == "anantabhat003@gmail.com")
    assert anant["password"] == "9342049935Aa@"
    # Shared password picked up.
    anu = next(a for a in accts if a["email"] == "anu@gmail.com")
    assert anu["password"] == "Koole21@#"


def test_accounts_comment_archive():
    accts = parse_accounts(_read("config.yaml"))
    emails = {a["email"] for a in accts}
    # Commented-out accounts must still be extracted.
    assert "sumeetpatil.svp18@gmail.com" in emails
    assert len(accts) >= 40
    booked = {a["email"] for a in accts if a["status"] == "booked"}
    assert "sumeetpatil.svp18@gmail.com" in booked


def test_treks():
    treks = parse_treks(_read("config.yaml"))
    names = {t["name"] for t in treks}
    assert {"Kudremukha", "Netravathi", "Kurinjal", "Bandaje"} <= names
    net = next(t for t in treks if t["name"] == "Netravathi")
    assert net["portal_trek_id"] == 113
    assert net["district_id"] == 24
    assert net["timeslot_mapping_id"] == 187


def test_trekkers():
    trekkers = parse_trekkers(_read("config.yaml"))
    names = {t["name"] for t in trekkers}
    assert "Azhad Singh" in names
    azhad = next(t for t in trekkers if t["name"] == "Azhad Singh")
    assert azhad["age"] == 28
    assert azhad["gender"] == "Male"
    assert azhad["mobile_no"] == "7899161862"
    assert azhad["govt_id_type"] == "pan"
    assert azhad["govt_id"] == "GDKPS7529K"
    # A voter_id and dl example from the commented blocks.
    types = {t["govt_id_type"] for t in trekkers if t["govt_id_type"]}
    assert "voter_id" in types
    assert "dl" in types
