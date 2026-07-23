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


# --- Settings ---------------------------------------------------------------

class SettingsRead(BaseModel):
    booking_phone_number: Optional[str] = None
    shared_default_password: Optional[str] = None
    captcha_mode: str = "manual"
    ocr_space_api_key: Optional[str] = None
    proxy_enabled: bool = False
    proxy_host: Optional[str] = None
    proxy_port: Optional[int] = None
    proxy_user: Optional[str] = None
    proxy_pass: Optional[str] = None
    proxy_country: str = "IN"
    proxy_session_lifetime: str = "30m"
    proxy_use_sticky: bool = True
    require_country: str = "IN"
    ip_cooldown_days: int = 1
    account_cooldown_days: int = 1


class SettingsUpdate(BaseModel):
    booking_phone_number: Optional[str] = None
    shared_default_password: Optional[str] = None
    captcha_mode: Optional[str] = None
    ocr_space_api_key: Optional[str] = None
    proxy_enabled: Optional[bool] = None
    proxy_host: Optional[str] = None
    proxy_port: Optional[int] = None
    proxy_user: Optional[str] = None
    proxy_pass: Optional[str] = None
    proxy_country: Optional[str] = None
    proxy_session_lifetime: Optional[str] = None
    proxy_use_sticky: Optional[bool] = None
    require_country: Optional[str] = None
    ip_cooldown_days: Optional[int] = None
    account_cooldown_days: Optional[int] = None


class ProxyTestResult(BaseModel):
    enabled: bool
    ok: bool
    ip: Optional[str] = None
    country: Optional[str] = None
    mode: str = "direct"
    sticky_verified: bool = False
    probes: List[dict] = []
    error: Optional[str] = None
