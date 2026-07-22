"""Pydantic request/response schemas for the API (kept separate from DB models)."""
from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel


# --- Import -----------------------------------------------------------------

class ParseTextRequest(BaseModel):
    text: str


class AccountRow(BaseModel):
    email: str
    password: Optional[str] = None
    status: str = "available"
    notes: Optional[str] = None


class TrekkerRow(BaseModel):
    name: Optional[str] = None
    age: Optional[int] = None
    gender: Optional[str] = None
    mobile_no: Optional[str] = None
    govt_id_type: Optional[str] = None
    govt_id: Optional[str] = None
    issues: List[str] = []


class TrekRow(BaseModel):
    name: str
    portal_trek_id: int
    district_id: int
    timeslot_mapping_id: int
    timeslot_id: int
    check_in: Optional[str] = None


class PreviewResponse(BaseModel):
    kind: str
    rows: List[dict]
    count: int


class CommitAccountsRequest(BaseModel):
    rows: List[AccountRow]


class CommitTrekkersRequest(BaseModel):
    rows: List[TrekkerRow]


class CommitTreksRequest(BaseModel):
    rows: List[TrekRow]


class CommitResult(BaseModel):
    created: int = 0
    updated: int = 0
    skipped: int = 0
    messages: List[str] = []
