"""Tests for the heuristic trekker parser across messy input shapes."""
from app.import_.parser import parse_trekkers_text


def _by_name(rows, name):
    return next(r for r in rows if r["name"] and name.lower() in r["name"].lower())


def test_labeled_block_single():
    text = """
    Name: Manoj TS
    Age: 28 yrs
    Gender: Male
    Mobile: +91 9876543210
    PAN: ABCDE1234F
    """
    rows = parse_trekkers_text(text)
    assert len(rows) == 1
    r = rows[0]
    assert r["name"] == "Manoj TS"
    assert r["age"] == 28
    assert r["gender"] == "Male"
    assert r["mobile_no"] == "9876543210"
    assert r["govt_id_type"] == "pan"
    assert r["govt_id"] == "ABCDE1234F"
    assert r["issues"] == []


def test_multiple_people_labeled():
    text = """
    Name: Ravi Kumar
    Age 30
    M
    Contact: (+91)-98765 43210
    Aadhaar 4123 4567 8901

    Name: Priya S
    Female, 25
    Mob 9123456780
    Voter: ABC1234567
    """
    rows = parse_trekkers_text(text)
    assert len(rows) == 2
    ravi = _by_name(rows, "Ravi")
    assert ravi["gender"] == "Male"
    assert ravi["age"] == 30
    assert ravi["mobile_no"] == "9876543210"
    # Aadhaar (12 digits) -> ration business rule.
    assert ravi["govt_id_type"] == "ration"
    priya = _by_name(rows, "Priya")
    assert priya["gender"] == "Female"
    assert priya["age"] == 25
    assert priya["govt_id_type"] == "voter_id"
    assert priya["govt_id"] == "ABC1234567"


def test_unlabeled_ids_blank_separated():
    text = """Suresh Rao
40
Male
9998887776
BSIPN0247B

Divya Menon
34
Female
8887776665
TN38 20210002037
"""
    rows = parse_trekkers_text(text)
    assert len(rows) == 2
    suresh = _by_name(rows, "Suresh")
    assert suresh["govt_id_type"] == "pan"  # PAN pattern, no label
    assert suresh["govt_id"] == "BSIPN0247B"
    divya = _by_name(rows, "Divya")
    assert divya["govt_id_type"] == "dl"     # DL with embedded space
    assert "20210002037" in divya["govt_id"]


def test_table_pipe_with_header():
    text = """name | age | gender | mobile | id
Anil Kumar | 29 | M | 9876500011 | ABCDE9999K
Sunita Devi | 41 | F | 9876500022 | XYZAB1234C
"""
    rows = parse_trekkers_text(text)
    assert len(rows) == 2
    anil = _by_name(rows, "Anil")
    assert anil["age"] == 29 and anil["gender"] == "Male"
    assert anil["govt_id_type"] == "pan"


def test_order_independence():
    text = """
    PAN ABCDE1234F
    Female
    Age: 22
    Name - Kavya R
    9871112223
    """
    rows = parse_trekkers_text(text)
    assert len(rows) == 1
    r = rows[0]
    assert r["name"] == "Kavya R"
    assert r["gender"] == "Female"
    assert r["age"] == 22
    assert r["govt_id_type"] == "pan"


def test_missing_fields_flagged():
    text = "Name: Lone Trekker\nPAN: ABCDE1234F"
    rows = parse_trekkers_text(text)
    r = rows[0]
    assert r["age"] is None
    assert r["gender"] is None
    assert r["mobile_no"] is None
    assert set(["age", "gender", "mobile_no"]).issubset(set(r["issues"]))


def test_ocr_recovery():
    text = "Name: OCR Person\nMob1le: 9876543210\nAadharr 412345678901\nGend3r: Male"
    rows = parse_trekkers_text(text)
    r = rows[0]
    assert r["mobile_no"] == "9876543210"
    assert r["gender"] == "Male"
    assert r["govt_id_type"] == "ration"  # aadhaar -> ration


def test_whatsapp_style():
    text = """Guys here are the details
1. Arjun Nair 27 M 9812345678 pan CUZPR8774D
2. Meera Iyer 24 F 9812345679 pan DWAPP6989J
"""
    rows = parse_trekkers_text(text)
    # At least both people detected with their PANs.
    pans = {r["govt_id"] for r in rows if r["govt_id"]}
    assert "CUZPR8774D" in pans
    assert "DWAPP6989J" in pans


def test_inline_numbered_names():
    text = ("1. Ravi Kumar 30 M 9876543210 ABCDE1234F\n"
            "2. Priya S, Female, 25, 9123456780, voter ABC1234567")
    rows = parse_trekkers_text(text)
    assert len(rows) == 2
    ravi = _by_name(rows, "Ravi Kumar")
    assert ravi["age"] == 30 and ravi["gender"] == "Male"
    assert ravi["govt_id_type"] == "pan" and ravi["govt_id"] == "ABCDE1234F"
    priya = _by_name(rows, "Priya S")
    assert priya["govt_id_type"] == "voter_id"
