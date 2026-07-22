"""SQLModel tables for the booking bot.

One SQLite DB under data/. Models double as the persistence layer; API request/
response shapes live in schemas.py.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import List, Optional

from sqlalchemy import JSON, Column, UniqueConstraint
from sqlmodel import Field, SQLModel

# --- Enumerated string values (kept as plain constants for SQLite friendliness) ---

ACCOUNT_STATUSES = ("available", "booked", "disabled")
EVENT_STATUSES = ("open", "complete")
GENDERS = ("Male", "Female")
# Portal-accepted government id types (see portal/ids.py for normalization).
ID_TYPES = ("pan", "voter_id", "dl", "ration", "passport")
CAPTCHA_MODES = ("manual", "auto")
TICKET_SECTIONS = ("booked", "cancelled")


def _utcnow() -> datetime:
    # Local naive time. "Today" (account/IP cooldown, dashboard) must reset at the
    # portal's local midnight (IST for the user), so all date comparisons use the
    # machine's local date consistently — never mix UTC and local here.
    return datetime.now()


class Account(SQLModel, table=True):
    __tablename__ = "account"

    id: Optional[int] = Field(default=None, primary_key=True)
    email: str = Field(index=True, unique=True)
    # None -> fall back to AppSetting.shared_default_password at login time.
    password: Optional[str] = None
    status: str = Field(default="available", index=True)
    booked_date: Optional[date] = None
    booked_trek: Optional[str] = None
    last_used_ip: Optional[str] = None
    last_used_date: Optional[date] = Field(default=None, index=True)
    notes: Optional[str] = None
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class Trek(SQLModel, table=True):
    __tablename__ = "trek"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    portal_trek_id: int
    district_id: int
    timeslot_mapping_id: int
    timeslot_id: int
    check_in: Optional[str] = None  # default DD-MM-YYYY; usually set per-event
    is_active: bool = Field(default=True)
    notes: Optional[str] = None
    created_at: datetime = Field(default_factory=_utcnow)


class Trekker(SQLModel, table=True):
    __tablename__ = "trekker"
    __table_args__ = (UniqueConstraint("govt_id", name="uq_trekker_govt_id"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    age: Optional[int] = None
    gender: Optional[str] = None
    mobile_no: Optional[str] = None
    govt_id_type: Optional[str] = None
    govt_id: Optional[str] = Field(default=None, index=True)
    source_note: Optional[str] = None  # e.g. "parsed from whatsapp 2026-07-23"
    created_at: datetime = Field(default_factory=_utcnow)


class Event(SQLModel, table=True):
    """A booking event = trek + date + a roster of trekkers (+ booking phone)."""

    __tablename__ = "event"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    trek_id: int = Field(foreign_key="trek.id")
    check_in: str  # DD-MM-YYYY
    booking_phone: str  # dedicated OTP number, used for trekker #1 of every booking
    status: str = Field(default="open")
    created_at: datetime = Field(default_factory=_utcnow)


class EventTrekker(SQLModel, table=True):
    """Roster membership: which trekkers belong to an event and whether booked."""

    __tablename__ = "event_trekker"

    id: Optional[int] = Field(default=None, primary_key=True)
    event_id: int = Field(foreign_key="event.id", index=True)
    trekker_id: int = Field(foreign_key="trekker.id", index=True)
    booked: bool = Field(default=False)
    booking_id: Optional[int] = Field(default=None, foreign_key="booking.id")


class Booking(SQLModel, table=True):
    """One account booking (<=3 trekkers) toward an event."""

    __tablename__ = "booking"

    id: Optional[int] = Field(default=None, primary_key=True)
    event_id: Optional[int] = Field(default=None, foreign_key="event.id", index=True)
    account_id: Optional[int] = Field(default=None, foreign_key="account.id")
    account_email: Optional[str] = None
    trek_name: Optional[str] = None
    check_in: Optional[str] = None
    trekker_ids: List[int] = Field(default_factory=list, sa_column=Column(JSON))
    state: str = Field(default="idle", index=True)
    exit_ip: Optional[str] = None
    order_id: Optional[str] = None
    amount: Optional[str] = None
    portal_booking_id: Optional[str] = None
    ticket_path: Optional[str] = None
    receipt_path: Optional[str] = None
    error: Optional[str] = None
    created_at: datetime = Field(default_factory=_utcnow, index=True)


class Ticket(SQLModel, table=True):
    """A ticket as seen on the portal (for the dashboard + cancel flow)."""

    __tablename__ = "ticket"
    __table_args__ = (
        UniqueConstraint("account_email", "portal_ref", name="uq_ticket_acct_ref"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    account_email: str = Field(index=True)
    portal_ref: str = Field(index=True)  # /preview-ticket/{portal_ref}
    cancel_ref: Optional[str] = None  # /booking/{cancel_ref}/cancel
    section: str = Field(default="booked")  # booked | cancelled
    trek: Optional[str] = None
    check_in: Optional[str] = None
    trekker_names: List[str] = Field(default_factory=list, sa_column=Column(JSON))
    cancellable: bool = Field(default=False)
    amount: Optional[str] = None
    ticket_pdf_path: Optional[str] = None
    receipt_pdf_path: Optional[str] = None
    booked_on: Optional[date] = Field(default=None, index=True)
    updated_at: datetime = Field(default_factory=_utcnow)


class UsedIp(SQLModel, table=True):
    """Ledger of exit IPs used for a booking, to avoid same-day reuse."""

    __tablename__ = "used_ip"

    id: Optional[int] = Field(default=None, primary_key=True)
    ip: str = Field(index=True)
    used_date: date = Field(index=True)
    account_email: Optional[str] = None
    booking_id: Optional[int] = None


class AppSetting(SQLModel, table=True):
    """Singleton settings row (id == 1)."""

    __tablename__ = "app_setting"

    id: Optional[int] = Field(default=1, primary_key=True)

    booking_phone_number: Optional[str] = None
    shared_default_password: Optional[str] = None
    captcha_mode: str = Field(default="manual")  # manual | auto
    ocr_space_api_key: Optional[str] = None

    proxy_enabled: bool = Field(default=False)
    proxy_host: Optional[str] = "thehub.proxy-cheap.com"
    proxy_port: Optional[int] = 8080
    proxy_user: Optional[str] = None
    proxy_pass: Optional[str] = None
    proxy_country: str = Field(default="IN")
    proxy_session_lifetime: str = Field(default="30m")

    require_country: str = Field(default="IN")
    ip_cooldown_days: int = Field(default=1)      # today-only == 1
    account_cooldown_days: int = Field(default=1)  # today-only == 1

    updated_at: datetime = Field(default_factory=_utcnow)
