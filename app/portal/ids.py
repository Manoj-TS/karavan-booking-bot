"""Government id-type normalization and pattern detection.

Two vocabularies:
  * canonical  (UI/DB): pan | voter_id | dl | ration | passport
  * portal     (what the booking form submits): Pancard | VoterId | RationCard |
    DrivingLicense | Passport

Ported from legacy/booker.py (normalize_id_type / _ID_TYPE_MAP), extended with
regex detection so an unlabeled id like "ABCDE1234F" is typed as PAN.
"""
from __future__ import annotations

import re
from typing import Optional

PORTAL_ID_TYPES = {"VoterId", "RationCard", "DrivingLicense", "Pancard", "Passport"}

_ID_TYPE_MAP = {
    "pan": "Pancard", "pancard": "Pancard", "pancardno": "Pancard",
    "permanentaccountnumber": "Pancard",
    "dl": "DrivingLicense", "drivinglicense": "DrivingLicense",
    "drivinglicence": "DrivingLicense", "driving": "DrivingLicense",
    "drivinglicensenumber": "DrivingLicense", "licenseno": "DrivingLicense",
    "licenceno": "DrivingLicense",
    "voter": "VoterId", "voterid": "VoterId", "voteridcard": "VoterId",
    "epic": "VoterId", "voterscard": "VoterId", "electorid": "VoterId",
    "ration": "RationCard", "rationcard": "RationCard",
    # Aadhaar is not accepted by the portal; business rule stores it as ration.
    "aadhaar": "RationCard", "aadhar": "RationCard", "uid": "RationCard",
    "uidai": "RationCard", "uniqueid": "RationCard",
    "passport": "Passport",
}

# portal form -> canonical
_PORTAL_TO_CANONICAL = {
    "Pancard": "pan",
    "VoterId": "voter_id",
    "DrivingLicense": "dl",
    "RationCard": "ration",
    "Passport": "passport",
}

_CANONICAL = set(_PORTAL_TO_CANONICAL.values())


def normalize_id_type(raw: Optional[str]) -> Optional[str]:
    """Return the portal-form id type (Pancard/VoterId/...) or None. Verbatim
    behavior from booker.py, with a few extra aliases."""
    if raw is None:
        return None
    if raw in PORTAL_ID_TYPES:
        return raw
    key = re.sub(r"[^a-z0-9]", "", str(raw).lower())
    if not key:
        return None
    if key in _ID_TYPE_MAP:
        return _ID_TYPE_MAP[key]
    for k, v in _ID_TYPE_MAP.items():
        if k in key:
            return v
    return None


def to_canonical(raw: Optional[str]) -> Optional[str]:
    """Return the canonical UI/DB id type (pan/voter_id/dl/ration/passport)."""
    if raw is None:
        return None
    if raw in _CANONICAL:
        return raw
    portal = normalize_id_type(raw)
    return _PORTAL_TO_CANONICAL.get(portal) if portal else None


def to_portal(canonical_or_raw: Optional[str]) -> Optional[str]:
    """Return the portal-form id type for submission."""
    return normalize_id_type(canonical_or_raw)


# --- Pattern detection (for unlabeled ids) ---

_PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
_VOTER_RE = re.compile(r"^[A-Z]{3}[0-9]{7}$")
_AADHAAR_RE = re.compile(r"^[2-9][0-9]{11}$")  # 12 digits, not starting 0/1
# DL: 2-letter state + digits, often with a space/embedded chars, 10-16 chars.
_DL_RE = re.compile(r"^[A-Z]{2}[0-9]{2}[A-Z0-9 ]{6,13}$")


def detect_id_type(value: Optional[str]) -> Optional[str]:
    """Best-effort canonical id type from the value's shape alone.

    Order matters: PAN and Voter are unambiguous; Aadhaar (12 digits) -> ration;
    DL is the loosest so it is tried last. Returns None if nothing matches.
    """
    if not value:
        return None
    v = str(value).strip().upper()
    compact = v.replace(" ", "").replace("-", "")
    if _PAN_RE.match(compact):
        return "pan"
    if _VOTER_RE.match(compact):
        return "voter_id"
    if _AADHAAR_RE.match(compact):
        return "ration"  # Aadhaar -> ration (portal has no Aadhaar)
    if _DL_RE.match(v) or _DL_RE.match(compact):
        return "dl"
    return None
