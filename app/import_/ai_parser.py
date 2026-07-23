"""AI-backed trekker parser (Claude via Anthropic SDK).

Default smart-paste engine when an ANTHROPIC_API_KEY is present in the
environment; the caller falls back to the local heuristic parser if the key is
missing or the call fails. Uses forced tool-use for reliable structured output
(the installed SDK predates messages.parse / output_config), then runs the
result through the same deterministic normalizers as the local parser so the
Aadhaar->ration rule, 10-digit mobiles, and canonical id types always hold.
"""
from __future__ import annotations

import os
from typing import Dict, List, Optional

from app.import_.parser import _norm_gender, _norm_mobile
from app.portal.ids import detect_id_type, to_canonical

MODEL = "claude-opus-4-8"

SYSTEM_PROMPT = """You extract trekker records from messy human text (WhatsApp \
chats, tables, forms, OCR dumps, emails). Understand intent rather than relying \
on fixed formatting.

For each distinct person, produce: name, age, gender, mobile_no, govt_id_type, \
govt_id. Rules:
- One object per person. Never merge two people; never invent people.
- name: the person's full name; keep initials; trim extra spaces.
- age: integer only (from "28", "28 yrs", "28 years").
- gender: "Male" or "Female" (M->Male, F->Female).
- mobile_no: the final 10-digit Indian mobile number, digits only (strip +91, \
spaces, dashes, brackets).
- govt_id_type: one of pan, voter_id, dl, ration, passport. Detect by label OR \
pattern: PAN = AAAAA9999A; Voter/EPIC = ABC1234567; Driving Licence = state code \
+ digits; **Aadhaar (a 12-digit number or 'aadhaar'/'UID') maps to govt_id_type \
'ration'** (the booking portal has no Aadhaar option). First explicit id wins.
- govt_id: the id value, uppercase, no spaces where obvious.
- Any field you cannot determine: null. Do not guess or hallucinate.
Ignore form noise words (Mandatory, Optional, Upload, Document, Required, ID \
Proof, etc.). Fix only high-confidence OCR errors (Moblle->Mobile, Aadharr->\
Aadhaar, P4N->PAN).

Call the emit_trekkers tool with every person you found, in order."""

_TOOL = {
    "name": "emit_trekkers",
    "description": "Return the normalized list of trekkers extracted from the text.",
    "input_schema": {
        "type": "object",
        "properties": {
            "trekkers": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": ["string", "null"]},
                        "age": {"type": ["integer", "null"]},
                        "gender": {"type": ["string", "null"]},
                        "mobile_no": {"type": ["string", "null"]},
                        "govt_id_type": {"type": ["string", "null"]},
                        "govt_id": {"type": ["string", "null"]},
                    },
                    "required": ["name", "age", "gender", "mobile_no",
                                 "govt_id_type", "govt_id"],
                },
            }
        },
        "required": ["trekkers"],
    },
}


def ai_available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _normalize(row: Dict) -> Dict:
    """Run an AI row through the same deterministic normalizers as the heuristic."""
    name = (row.get("name") or "").strip() or None
    gid = (str(row.get("govt_id")).strip().upper() if row.get("govt_id") else None)
    gtype = to_canonical(row.get("govt_id_type")) if row.get("govt_id_type") else None
    if gid and not gtype:
        gtype = detect_id_type(gid)
    age = row.get("age")
    try:
        age = int(age) if age not in (None, "") else None
    except (ValueError, TypeError):
        age = None
    rec = {
        "name": name,
        "age": age,
        "gender": _norm_gender(str(row.get("gender"))) if row.get("gender") else None,
        "mobile_no": _norm_mobile(str(row.get("mobile_no"))) if row.get("mobile_no") else None,
        "govt_id_type": gtype,
        "govt_id": gid,
    }
    rec["issues"] = [f for f in ("name", "age", "gender", "mobile_no",
                                  "govt_id_type", "govt_id") if not rec[f]]
    return rec


def parse_trekkers_ai(text: str, timeout: float = 45.0) -> List[Dict]:
    """Parse text via Claude. Raises on any failure so the caller can fall back."""
    if not ai_available():
        raise RuntimeError("ANTHROPIC_API_KEY not set.")
    import anthropic  # imported lazily so the app runs without the SDK

    client = anthropic.Anthropic().with_options(timeout=timeout)
    resp = client.messages.create(
        model=MODEL,
        max_tokens=8000,
        system=SYSTEM_PROMPT,
        tools=[_TOOL],
        tool_choice={"type": "tool", "name": "emit_trekkers"},
        messages=[{"role": "user", "content": text}],
    )
    rows: List[Dict] = []
    for block in resp.content:
        if block.type == "tool_use" and block.name == "emit_trekkers":
            for r in (block.input or {}).get("trekkers", []):
                if not isinstance(r, dict):
                    continue
                rec = _normalize(r)
                if rec["name"] or rec["govt_id"] or rec["mobile_no"]:
                    rows.append(rec)
    return rows
