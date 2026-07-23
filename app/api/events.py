"""Events: an event = trek + date + trekker roster (+ booking phone).

Planning auto-splits the unbooked roster into <=3-person chunks and suggests a
random available account per chunk. The wizard lets the user adjust both before
starting each booking.
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func
from sqlmodel import Session, select

from app.db import get_session, get_settings
from app.models import Account, Event, EventTrekker, Trek, Trekker
from app.services import _cooldown_threshold

router = APIRouter(prefix="/api/events", tags=["events"])


class EventIn(BaseModel):
    name: str
    trek_id: int
    check_in: str
    booking_phone: str
    trekker_ids: List[int] = []


def _roster(session: Session, event_id: int) -> List[dict]:
    rows = session.exec(
        select(EventTrekker, Trekker)
        .where(EventTrekker.event_id == event_id, EventTrekker.trekker_id == Trekker.id)
    ).all()
    out = []
    for et, tk in rows:
        out.append({
            "trekker_id": tk.id, "name": tk.name, "age": tk.age, "gender": tk.gender,
            "govt_id_type": tk.govt_id_type, "govt_id": tk.govt_id,
            "mobile_no": tk.mobile_no, "booked": et.booked, "booking_id": et.booking_id,
        })
    return out


def _event_view(session: Session, event: Event) -> dict:
    trek = session.get(Trek, event.trek_id)
    roster = _roster(session, event.id)
    booked = sum(1 for r in roster if r["booked"])
    return {
        "id": event.id, "name": event.name, "trek_id": event.trek_id,
        "trek_name": trek.name if trek else None,
        "check_in": event.check_in, "booking_phone": event.booking_phone,
        "status": event.status, "roster": roster,
        "total": len(roster), "booked": booked, "remaining": len(roster) - booked,
    }


@router.get("")
def list_events(session: Session = Depends(get_session)) -> List[dict]:
    events = session.exec(select(Event).order_by(Event.created_at.desc())).all()
    return [_event_view(session, e) for e in events]


@router.get("/{event_id}")
def get_event(event_id: int, session: Session = Depends(get_session)) -> dict:
    event = session.get(Event, event_id)
    if not event:
        raise HTTPException(404, "Event not found.")
    return _event_view(session, event)


@router.post("")
def create_event(body: EventIn, session: Session = Depends(get_session)) -> dict:
    if not session.get(Trek, body.trek_id):
        raise HTTPException(400, "Unknown trek.")
    event = Event(name=body.name, trek_id=body.trek_id, check_in=body.check_in,
                  booking_phone=body.booking_phone)
    session.add(event)
    session.commit()
    session.refresh(event)
    for tid in dict.fromkeys(body.trekker_ids):  # de-dup, preserve order
        if session.get(Trekker, tid):
            session.add(EventTrekker(event_id=event.id, trekker_id=tid))
    session.commit()
    return _event_view(session, event)


class EventPatch(BaseModel):
    name: Optional[str] = None
    trek_id: Optional[int] = None
    check_in: Optional[str] = None
    booking_phone: Optional[str] = None


@router.patch("/{event_id}")
def update_event(event_id: int, patch: EventPatch,
                 session: Session = Depends(get_session)) -> dict:
    event = session.get(Event, event_id)
    if not event:
        raise HTTPException(404, "Event not found.")
    data = patch.model_dump(exclude_unset=True)
    if "trek_id" in data and data["trek_id"] and not session.get(Trek, data["trek_id"]):
        raise HTTPException(400, "Unknown trek.")
    for field, value in data.items():
        setattr(event, field, value)
    session.add(event)
    session.commit()
    return _event_view(session, event)


class RosterPatch(BaseModel):
    add: List[int] = []
    remove: List[int] = []


@router.patch("/{event_id}/roster")
def edit_roster(event_id: int, patch: RosterPatch,
                session: Session = Depends(get_session)) -> dict:
    event = session.get(Event, event_id)
    if not event:
        raise HTTPException(404, "Event not found.")
    existing = {et.trekker_id for et in session.exec(
        select(EventTrekker).where(EventTrekker.event_id == event_id)).all()}
    for tid in patch.add:
        if tid not in existing and session.get(Trekker, tid):
            session.add(EventTrekker(event_id=event_id, trekker_id=tid))
    for tid in patch.remove:
        row = session.exec(select(EventTrekker).where(
            EventTrekker.event_id == event_id, EventTrekker.trekker_id == tid,
            EventTrekker.booked == False)).first()  # noqa: E712 - don't remove booked
        if row:
            session.delete(row)
    session.commit()
    return _event_view(session, event)


@router.get("/{event_id}/plan")
def plan_event(event_id: int, session: Session = Depends(get_session)) -> dict:
    """Split the unbooked roster into <=3 chunks with a random account each."""
    event = session.get(Event, event_id)
    if not event:
        raise HTTPException(404, "Event not found.")
    roster = [r for r in _roster(session, event_id) if not r["booked"]]
    chunks = [roster[i:i + 3] for i in range(0, len(roster), 3)]

    # Pick distinct random available accounts (today-only exclusion).
    settings = get_settings(session)
    threshold = _cooldown_threshold(settings.account_cooldown_days)
    accounts = session.exec(
        select(Account)
        .where(Account.status == "available")
        .where((Account.last_used_date == None) | (Account.last_used_date < threshold))  # noqa: E711
        .order_by(func.random())
        .limit(len(chunks))
    ).all()

    plan = []
    for i, chunk in enumerate(chunks):
        acc = accounts[i] if i < len(accounts) else None
        plan.append({
            "chunk_index": i,
            "trekkers": chunk,
            "suggested_account": ({"id": acc.id, "email": acc.email} if acc else None),
        })
    return {
        "event_id": event_id, "booking_phone": event.booking_phone,
        "chunks": plan, "available_accounts": len(accounts),
        "needs_accounts": len(chunks),
    }


@router.delete("/{event_id}")
def delete_event(event_id: int, session: Session = Depends(get_session)) -> dict:
    event = session.get(Event, event_id)
    if not event:
        raise HTTPException(404, "Event not found.")
    for et in session.exec(select(EventTrekker).where(EventTrekker.event_id == event_id)).all():
        session.delete(et)
    session.delete(event)
    session.commit()
    return {"deleted": event_id}
