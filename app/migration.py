"""Comment-aware importer for the legacy seed files.

`accounts.yaml` is a clean `accounts:` list. `config.yaml` is a scratchpad where
~80 accounts, several trek presets, and trekker groups are mostly commented out.
Standard yaml.safe_load only sees the one active entry, so account/trek/trekker
extraction is done with line/regex scanning over the raw text.

Every function returns plain dicts for a review-and-commit preview; nothing here
touches the DB.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from app.portal.ids import detect_id_type, to_canonical

# --- Accounts ---------------------------------------------------------------

_EMAIL_RE = re.compile(
    r"""^\s*\#?\s*                 # optional leading comment
        (?:-\s*)?                  # optional yaml list dash
        email\s*:\s*["']?          # key
        ([^\s"'#]+@[^\s"'#]+)      # the email
    """,
    re.VERBOSE,
)
_PASSWORD_RE = re.compile(r"""^\s*\#?\s*(?:-\s*)?password\s*:\s*(.+?)\s*$""")


def _extract_scalar(raw: str) -> str:
    """Value from a `key: value` tail. Honors quotes (so a '#' inside a quoted
    password is kept), else strips a trailing inline comment."""
    raw = raw.strip()
    if raw and raw[0] in "\"'":
        quote = raw[0]
        end = raw.find(quote, 1)
        if end != -1:
            return raw[1:end]
        return raw[1:]
    # Unquoted: an inline "# comment" is a real comment.
    return re.split(r"\s+#", raw, maxsplit=1)[0].strip()
_BOOKED_RE = re.compile(r"--\s*booked", re.IGNORECASE)
_WRONG_RE = re.compile(r"wrong\s*creds", re.IGNORECASE)


def parse_accounts(text: str) -> List[Dict]:
    """Extract accounts from either the clean list format or the comment archive.

    Strategy: walk line by line. Each `email:` starts a record; a `password:` on a
    later line (before the next email) attaches to it. Inline `-- Booked` marks the
    account booked; a nearby "Wrong creds" marks it disabled. Deduped by email,
    preferring the entry that carries a password / booked status.
    """
    records: Dict[str, Dict] = {}
    order: List[str] = []
    current: Optional[str] = None

    for raw in text.splitlines():
        m = _EMAIL_RE.match(raw)
        if m:
            email = m.group(1).strip().lower()
            current = email
            status = "booked" if _BOOKED_RE.search(raw) else "available"
            if _WRONG_RE.search(raw):
                status = "disabled"
            rec = records.get(email, {"email": email, "password": None,
                                      "status": "available", "notes": None})
            # Upgrade status if this mention is more specific.
            if status != "available":
                rec["status"] = status
            records[email] = rec
            if email not in order:
                order.append(email)
            continue

        if current is None:
            continue
        pm = _PASSWORD_RE.match(raw)
        if pm and records.get(current) and not records[current]["password"]:
            pw = _extract_scalar(pm.group(1))
            if pw and pw.lower() not in ("", "null", "none"):
                records[current]["password"] = pw

    # Clean-list format via yaml as a cross-check (adds any we missed, fills pw).
    try:
        data = yaml.safe_load(text)
        if isinstance(data, dict) and isinstance(data.get("accounts"), list):
            for item in data["accounts"]:
                if not isinstance(item, dict):
                    continue
                email = str(item.get("email", "")).strip().lower()
                if not email:
                    continue
                rec = records.get(email, {"email": email, "password": None,
                                          "status": "available", "notes": None})
                if item.get("password") and not rec["password"]:
                    rec["password"] = str(item["password"])
                records[email] = rec
                if email not in order:
                    order.append(email)
    except yaml.YAMLError:
        pass

    return [records[e] for e in order]


# --- Treks ------------------------------------------------------------------

_TREK_RE = re.compile(
    r"""id\s*:\s*(\d+)\s*
        (?:\#.*)?\s*
        name\s*:\s*["']?([^"'\n#]+?)["']?\s*
        (?:\#.*)?\s*
        district_id\s*:\s*(\d+)\s*
        (?:\#.*)?\s*
        timeslot_mapping_id\s*:\s*(\d+)\s*
        (?:\#.*)?\s*
        timeslot_id\s*:\s*(\d+)\s*
        (?:\#.*)?\s*
        check_in\s*:\s*["']?([0-9]{2}-[0-9]{2}-[0-9]{4})["']?
    """,
    re.VERBOSE,
)


def _decomment(text: str) -> str:
    """Strip a single leading '# ' from commented lines so blocks parse."""
    out = []
    for line in text.splitlines():
        out.append(re.sub(r"^(\s*)#\s?", r"\1", line))
    return "\n".join(out)


def parse_treks(text: str) -> List[Dict]:
    """Extract every trek preset (active or commented) in the file."""
    body = _decomment(text)
    treks: List[Dict] = []
    seen = set()
    for m in _TREK_RE.finditer(body):
        rec = {
            "portal_trek_id": int(m.group(1)),
            "name": m.group(2).strip(),
            "district_id": int(m.group(3)),
            "timeslot_mapping_id": int(m.group(4)),
            "timeslot_id": int(m.group(5)),
            "check_in": m.group(6),
        }
        key = (rec["portal_trek_id"], rec["name"])
        if key not in seen:
            seen.add(key)
            treks.append(rec)
    return treks


# --- Trekkers ---------------------------------------------------------------

_FIELD_RES = {
    "name": re.compile(r"name\s*:\s*['\"]?([^'\"#\n]+?)['\"]?\s*$", re.M),
    "age": re.compile(r"age\s*:\s*(\d{1,3})", re.I),
    "gender": re.compile(r"gender\s*:\s*['\"]?(male|female|m|f)\b", re.I),
    "mobile_no": re.compile(r"mobile_no\s*:\s*['\"]?([+\d][\d\s\-()]{7,}\d)", re.I),
    "govt_id_type": re.compile(r"govt_id_type\s*:\s*['\"]?([a-z_ ]+?)['\"]?\s*$", re.I | re.M),
    "govt_id": re.compile(r"govt_id\s*:\s*['\"]?([A-Za-z0-9 ]+?)['\"]?\s*$", re.M),
}


def _norm_gender(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    r = raw.strip().lower()
    if r in ("m", "male"):
        return "Male"
    if r in ("f", "female"):
        return "Female"
    return None


def _norm_mobile(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    return digits[-10:] if len(digits) >= 10 else (digits or None)


def parse_trekkers(text: str) -> List[Dict]:
    """Extract trekker blocks (active or commented). Splits on `- name:`."""
    body = _decomment(text)
    # Isolate the region after a `trekkers:` marker if present, else whole file.
    blocks = re.split(r"(?m)^\s*-\s*name\s*:", body)
    trekkers: List[Dict] = []
    for chunk in blocks[1:]:
        block = "- name:" + chunk
        rec: Dict = {}
        for field, rx in _FIELD_RES.items():
            m = rx.search(block)
            rec[field] = m.group(1).strip() if m else None
        if not rec.get("name"):
            continue
        rec["age"] = int(rec["age"]) if rec.get("age") else None
        rec["gender"] = _norm_gender(rec.get("gender"))
        rec["mobile_no"] = _norm_mobile(rec.get("mobile_no"))
        # Canonicalize the declared id type; if absent, sniff from the value.
        rec["govt_id_type"] = (
            to_canonical(rec.get("govt_id_type"))
            or detect_id_type(rec.get("govt_id"))
        )
        trekkers.append(rec)
    return trekkers


# --- Convenience ------------------------------------------------------------

def load_seed(path: str | Path) -> str:
    return Path(path).read_text(encoding="utf-8")
