"""Heuristic trekker parser: messy text -> normalized trekker rows.

Local, offline, no LLM. Turns WhatsApp dumps / OCR text / tables / forms into
the fixed schema {name, age, gender, mobile_no, govt_id_type, govt_id}, matching
by meaning rather than fixed layout. Every row carries an `issues` list naming
fields that are missing or low-confidence, so the UI can flag them for review.

Design follows the user's parsing spec, extended with regex id auto-typing via
portal.ids and confidence tracking. Nothing here writes to the DB.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from app.portal.ids import detect_id_type, to_canonical

# --- Vocabulary -------------------------------------------------------------

_LABELS = {
    "name": ("name", "full name", "fullname", "applicant", "trekker", "passenger",
             "person", "traveller", "traveler", "candidate", "member"),
    "age": ("age", "years", "yrs"),
    "gender": ("gender", "sex"),
    "mobile_no": ("mobile no", "mobile number", "mobile", "phone number", "phone",
                  "contact number", "contact", "mob", "cell", "whatsapp"),
    "govt_id_type": ("govt id type", "id type", "id proof", "document type"),
    "govt_id": ("govt id", "government id", "id number", "id no", "id",
                "pan", "pan card", "pancard", "pan no", "permanent account number",
                "aadhaar", "aadhar", "uid", "voter id", "epic", "elector id",
                "dl", "driving licence", "driving license", "licence no",
                "license no", "dl number", "ration", "ration card", "passport"),
}

# Words that are form chrome, not data.
_NOISE = {
    "mandatory", "optional", "verified", "upload", "document", "choose file",
    "attachment", "required", "govt", "government", "id proof", "proof",
    "details", "trekker details", "form", "submit", "please", "note",
}

# High-confidence OCR fixups only.
_OCR_FIXES = (
    (re.compile(r"\bmob[il1]le\b", re.I), "mobile"),
    (re.compile(r"\bmob[il1]le?\s*n[o0]\b", re.I), "mobile no"),
    (re.compile(r"\baadha?rr?\b", re.I), "aadhaar"),
    (re.compile(r"\bp\W?a\W?n\b", re.I), "PAN"),
    (re.compile(r"\bgend[e3]r\b", re.I), "gender"),
    (re.compile(r"\bfema1e\b", re.I), "female"),
    (re.compile(r"\bma1e\b", re.I), "male"),
)

_LABEL_LOOKUP = {}
for _canon, _syns in _LABELS.items():
    for _s in _syns:
        _LABEL_LOOKUP[_s] = _canon
# Longest labels first so "mobile number" wins over "mobile".
_LABEL_KEYS = sorted(_LABEL_LOOKUP, key=len, reverse=True)

_GENDER_RE = re.compile(r"\b(male|female|m|f)\b", re.I)
_AGE_RE = re.compile(r"\b(1[0-1]?\d|\d{1,2})\s*(?:yrs?|years?|y/o)\b", re.I)
_MOBILE_RE = re.compile(r"(?:\+?91[\s\-]*)?(?:\(\+?91\)[\s\-]*)?([6-9]\d[\d\s\-]{7,}\d)")
_DL_SPACED_RE = re.compile(r"\b[A-Z]{2}[0-9]{2}\s?[0-9]{6,12}\b")


@dataclass
class ParsedTrekker:
    name: Optional[str] = None
    age: Optional[int] = None
    gender: Optional[str] = None
    mobile_no: Optional[str] = None
    govt_id_type: Optional[str] = None
    govt_id: Optional[str] = None
    issues: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict:
        d = {
            "name": self.name, "age": self.age, "gender": self.gender,
            "mobile_no": self.mobile_no, "govt_id_type": self.govt_id_type,
            "govt_id": self.govt_id, "issues": self.issues,
        }
        return d


# --- Cleaning ---------------------------------------------------------------

def _apply_ocr_fixes(text: str) -> str:
    for rx, repl in _OCR_FIXES:
        text = rx.sub(repl, text)
    return text


def _clean_line(line: str) -> str:
    line = line.replace("\t", " ")
    line = re.sub(r"[•·▪◦*]+", " ", line)  # bullets/decoration
    line = re.sub(r"\s{2,}", " ", line)
    return line.strip()


def _strip_label(line: str) -> tuple[Optional[str], str]:
    """If the line starts with a known label, return (canonical, value).

    Handles both separator form ("Age: 28", "Name - Ravi") and space form
    ("Age 30", "Mob 9123456780", "PAN ABCDE1234F") common in WhatsApp text.
    """
    # Separator form.
    m = re.match(r"^\s*([A-Za-z][A-Za-z /_.]*?)\s*[:\-=]\s*(.+)$", line)
    if m:
        key = re.sub(r"\s{2,}", " ", m.group(1).strip().lower())
        if key in _LABEL_LOOKUP:
            return _LABEL_LOOKUP[key], m.group(2).strip()

    # Space form: line begins with a known label followed by whitespace + value.
    low = line.strip().lower()
    for lk in _LABEL_KEYS:
        if low.startswith(lk + " "):
            value = line.strip()[len(lk):].strip()
            if value:
                return _LABEL_LOOKUP[lk], value
    return None, line


# --- Field extractors -------------------------------------------------------

def _norm_gender(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    m = _GENDER_RE.search(raw)
    if not m:
        return None
    g = m.group(1).lower()
    return "Male" if g in ("m", "male") else "Female"


def _norm_mobile(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    m = _MOBILE_RE.search(raw)
    if not m:
        digits = re.sub(r"\D", "", raw)
        return digits[-10:] if len(digits) >= 10 else None
    digits = re.sub(r"\D", "", m.group(1))
    return digits[-10:] if len(digits) >= 10 else None


def _extract_age(block: str) -> Optional[int]:
    m = _AGE_RE.search(block)  # "28 yrs" / "28 years"
    if m:
        return int(m.group(1))
    m = re.search(r"\bage\b[:\-=\s]+(\d{1,2})\b", block, re.I)  # "age 30"
    if m:
        return int(m.group(1))
    m = re.search(r"(?m)^\s*(\d{1,2})\s*$", block)  # a line that is just an age
    if m:
        return int(m.group(1))
    # Last resort: an isolated 1-2 digit number (not part of a phone/id run) in a
    # plausible age range. In trekker context a lone 2-digit number is the age.
    for tok in re.findall(r"(?<!\d)(\d{1,2})(?!\d)", block):
        if 5 <= int(tok) <= 99:
            return int(tok)
    return None


def _find_id(block: str) -> tuple[Optional[str], Optional[str]]:
    """Return (canonical_type, value). First explicit/pattern id wins."""
    # DL with an embedded space (e.g. "TN38 20210002037") — join before token scan.
    for m in _DL_SPACED_RE.finditer(block.upper()):
        val = m.group(0).strip()
        return "dl", re.sub(r"\s+", " ", val)
    # Token scan: run pattern detection on each alnum token.
    tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9]{5,}", block)
    best = None
    for tok in tokens:
        t = detect_id_type(tok)
        if t:
            # PAN/voter are unambiguous — take immediately.
            if t in ("pan", "voter_id"):
                return t, tok.upper()
            if best is None:
                best = (t, tok.upper())
    return best if best else (None, None)


def _looks_like_name(line: str) -> bool:
    if not line:
        return False
    low = line.lower()
    if low in _NOISE:
        return False
    if any(ch.isdigit() for ch in line):
        return False
    # 1-4 words, mostly alphabetic, allow initials and dots.
    words = line.split()
    if not (1 <= len(words) <= 5):
        return False
    return all(re.match(r"^[A-Za-z][A-Za-z.]*$", w) for w in words)


# --- Person splitting -------------------------------------------------------

def _split_persons(text: str) -> List[str]:
    """Break the blob into one string per person, using the strongest cue found."""
    text = _apply_ocr_fixes(text)
    raw_lines = [_clean_line(l) for l in text.splitlines()]
    lines = [l for l in raw_lines if l]

    if not lines:
        return []

    # Strategy A: two or more explicit Name labels anchor each record.
    # (With a single name label, fields may sit before it — don't split here.)
    name_idxs = [i for i, l in enumerate(lines)
                 if _strip_label(l)[0] == "name"]
    if len(name_idxs) >= 2:
        blocks = []
        first = name_idxs[0]
        for j, start in enumerate(name_idxs):
            end = name_idxs[j + 1] if j + 1 < len(name_idxs) else len(lines)
            blocks.append("\n".join(lines[start:end]))
        # attach any preamble before the first name to that first record.
        if first > 0:
            blocks[0] = "\n".join(lines[:first]) + "\n" + blocks[0]
        return blocks

    # Strategy B: delimiter-separated table (comma/pipe/tab), one row per person.
    delim_rows = [l for l in lines if re.search(r"[|,\t]", l)]
    if len(delim_rows) >= 2 and len(delim_rows) >= len(lines) - 1:
        rows = _strip_header(delim_rows)
        return rows

    # Strategy C: blank-line separated blocks (use the raw, unfiltered lines).
    blocks, cur = [], []
    for l in raw_lines:
        if l:
            cur.append(l)
        elif cur:
            blocks.append("\n".join(cur))
            cur = []
    if cur:
        blocks.append("\n".join(cur))
    if len(blocks) >= 2:
        return blocks

    # Strategy D: one id per person — split when a second id appears.
    return _split_by_id_runs(lines)


def _strip_header(rows: List[str]) -> List[str]:
    """Drop a header row if the first row has no digits (labels only)."""
    if rows and not any(ch.isdigit() for ch in rows[0]):
        header_words = re.split(r"[|,\t]", rows[0].lower())
        if any(w.strip() in _LABEL_LOOKUP for w in header_words):
            return rows[1:]
    return rows


def _split_by_id_runs(lines: List[str]) -> List[str]:
    """When everything is one block, start a new person at each detected id."""
    blocks, cur, seen_id = [], [], False
    for l in lines:
        _, idval = _find_id(l)
        if idval and seen_id:
            blocks.append("\n".join(cur))
            cur, seen_id = [l], True
        else:
            cur.append(l)
            if idval:
                seen_id = True
    if cur:
        blocks.append("\n".join(cur))
    return blocks or ["\n".join(lines)]


# --- Per-person field assembly ---------------------------------------------

def _parse_block(block: str) -> ParsedTrekker:
    rec = ParsedTrekker()
    labeled: Dict[str, str] = {}
    leftover_lines: List[str] = []

    for line in block.splitlines():
        canon, value = _strip_label(line)
        if canon and value:
            labeled.setdefault(canon, value)
        else:
            leftover_lines.append(line)

    hay = block

    # Name: prefer a labeled name, else the first name-looking leftover line.
    rec.name = labeled.get("name")
    if not rec.name:
        for l in leftover_lines:
            if _looks_like_name(l):
                rec.name = l.strip()
                break

    # Age / gender / mobile — labels first, then scan the whole block.
    rec.age = (int(re.search(r"\d{1,3}", labeled["age"]).group())
               if labeled.get("age") and re.search(r"\d", labeled["age"])
               else _extract_age(hay))
    rec.gender = _norm_gender(labeled.get("gender")) or _norm_gender(hay)
    rec.mobile_no = _norm_mobile(labeled.get("mobile_no")) or _norm_mobile(hay)

    # Government id: explicit type + value, else pattern detection.
    id_type = to_canonical(labeled.get("govt_id_type"))
    id_val = labeled.get("govt_id")
    if id_val:
        # value may still be a label like "PAN ABCDE1234F"
        det_t, det_v = _find_id(id_val)
        id_val = det_v or id_val.strip()
        id_type = id_type or det_t
    if not id_val:
        det_t, det_v = _find_id(hay)
        id_type, id_val = (id_type or det_t), det_v
    if id_val and not id_type:
        id_type = detect_id_type(id_val)
    rec.govt_id_type = id_type
    rec.govt_id = id_val.upper() if id_val else None

    _set_issues(rec)
    return rec


def _set_issues(rec: ParsedTrekker) -> None:
    rec.issues = [
        f for f in ("name", "age", "gender", "mobile_no", "govt_id_type", "govt_id")
        if getattr(rec, f) in (None, "")
    ]


# --- Delimited table (header + rows) ---------------------------------------

_TABLE_ID_HEADERS = {"id", "govt id", "government id", "id no", "id number",
                     "pan", "pan no", "aadhaar", "voter id", "dl"}


def _parse_table(text: str) -> Optional[List[ParsedTrekker]]:
    """Parse a delimited table with a header row into records, or None."""
    lines = [_clean_line(l) for l in text.splitlines() if _clean_line(l)]
    if len(lines) < 2:
        return None
    delim = next((d for d in ("|", "\t", ";", ",")
                  if all(d in l for l in lines)), None)
    if not delim:
        return None
    header = [h.strip().lower() for h in lines[0].split(delim)]
    cols: List[Optional[str]] = []
    for h in header:
        if h in _LABEL_LOOKUP:
            cols.append(_LABEL_LOOKUP[h])
        elif h in _TABLE_ID_HEADERS:
            cols.append("govt_id")
        else:
            cols.append(None)
    if "name" not in cols:
        return None

    records: List[ParsedTrekker] = []
    for row in lines[1:]:
        cells = [c.strip() for c in row.split(delim)]
        rec = ParsedTrekker()
        for i, cell in enumerate(cells):
            if i >= len(cols) or not cols[i] or not cell:
                continue
            c = cols[i]
            if c == "name":
                rec.name = cell
            elif c == "age":
                m = re.search(r"\d{1,3}", cell)
                rec.age = int(m.group()) if m else None
            elif c == "gender":
                rec.gender = _norm_gender(cell)
            elif c == "mobile_no":
                rec.mobile_no = _norm_mobile(cell)
            elif c == "govt_id":
                t, v = _find_id(cell)
                rec.govt_id = (v or cell).upper()
                rec.govt_id_type = t or detect_id_type(cell)
            elif c == "govt_id_type":
                rec.govt_id_type = to_canonical(cell)
        _set_issues(rec)
        records.append(rec)
    return records


def parse_trekkers_text(text: str) -> List[Dict]:
    """Parse a blob into a list of trekker dicts (with an `issues` field each)."""
    if not text or not text.strip():
        return []
    table = _parse_table(text)
    blocks_recs = table if table is not None else [_parse_block(b)
                                                   for b in _split_persons(text)]
    out: List[Dict] = []
    for rec in blocks_recs:
        # Skip empty noise blocks (no name and no id and no mobile).
        if not (rec.name or rec.govt_id or rec.mobile_no):
            continue
        out.append(rec.as_dict())
    return out
