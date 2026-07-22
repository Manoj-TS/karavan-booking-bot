"""Dashboard: rollups, bookings history with time-range/search, calendar counts."""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from sqlmodel import Session, or_, select

from app.db import get_session
from app.models import Booking

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


def _amount(val: Optional[str]) -> float:
    if not val:
        return 0.0
    m = re.search(r"[\d,]+(?:\.\d+)?", str(val))
    return float(m.group().replace(",", "")) if m else 0.0


def _day(dt: datetime) -> date:
    return dt.date() if isinstance(dt, datetime) else dt


@router.get("/summary")
def summary(session: Session = Depends(get_session)) -> dict:
    today = date.today()
    all_bookings = session.exec(select(Booking)).all()
    completed = [b for b in all_bookings if b.state == "completed"]
    today_b = [b for b in completed if _day(b.created_at) == today]
    return {
        "today": {
            "bookings": len(today_b),
            "people": sum(len(b.trekker_ids or []) for b in today_b),
            "amount": round(sum(_amount(b.amount) for b in today_b), 2),
            "accounts": len({b.account_email for b in today_b if b.account_email}),
        },
        "all_time": {
            "bookings": len(completed),
            "people": sum(len(b.trekker_ids or []) for b in completed),
            "amount": round(sum(_amount(b.amount) for b in completed), 2),
        },
    }


def _range_start(range_: str) -> Optional[date]:
    today = date.today()
    if range_ == "today":
        return today
    if range_ == "week":
        return today - timedelta(days=today.weekday())
    if range_ == "month":
        return today.replace(day=1)
    return None  # all


@router.get("/bookings")
def bookings(range: str = Query("month", pattern="^(today|week|month|all)$"),
             q: Optional[str] = None, trek: Optional[str] = None,
             day: Optional[str] = None,
             session: Session = Depends(get_session)) -> List[dict]:
    stmt = select(Booking)
    if trek:
        stmt = stmt.where(Booking.trek_name == trek)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(Booking.trek_name.ilike(like),
                              Booking.account_email.ilike(like),
                              Booking.portal_booking_id.ilike(like)))
    rows = session.exec(stmt.order_by(Booking.created_at.desc())).all()

    start = _range_start(range)
    out = []
    for b in rows:
        d = _day(b.created_at)
        if day:
            if d.isoformat() != day:
                continue
        elif start and d < start:
            continue
        out.append({
            "id": b.id, "created_at": b.created_at.isoformat(), "date": d.isoformat(),
            "account_email": b.account_email, "trek_name": b.trek_name,
            "check_in": b.check_in, "state": b.state, "amount": b.amount,
            "portal_booking_id": b.portal_booking_id, "people": len(b.trekker_ids or []),
        })
    return out


@router.get("/calendar")
def calendar(year: int, month: int, session: Session = Depends(get_session)) -> dict:
    rows = session.exec(select(Booking)).all()
    counts = defaultdict(int)
    for b in rows:
        d = _day(b.created_at)
        if d.year == year and d.month == month and b.state == "completed":
            counts[d.isoformat()] += 1
    return {"year": year, "month": month, "counts": dict(counts)}
