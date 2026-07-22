"""Dashboard API: summary, bookings filtering, calendar counts."""
from datetime import date

from sqlmodel import Session

import app.db as db
from app.models import Booking


def _add_booking(**kw):
    with Session(db.engine) as s:  # db.engine is the reloaded temp-DB engine
        s.add(Booking(**kw))
        s.commit()


def test_summary_and_calendar(client):
    # Two completed bookings today.
    _add_booking(account_email="a@x.com", trek_name="Netravathi", trekker_ids=[1, 2],
                 state="completed", amount="500.00", portal_booking_id="B1")
    _add_booking(account_email="b@x.com", trek_name="Kudremukha", trekker_ids=[3],
                 state="completed", amount="250.00", portal_booking_id="B2")

    s = client.get("/api/dashboard/summary").json()
    assert s["today"]["bookings"] == 2
    assert s["today"]["people"] == 3
    assert s["today"]["amount"] == 750.0
    assert s["today"]["accounts"] == 2

    today = date.today()
    cal = client.get(f"/api/dashboard/calendar?year={today.year}&month={today.month}").json()
    assert cal["counts"][today.isoformat()] == 2

    # Search filter.
    rows = client.get("/api/dashboard/bookings?range=all&q=Netravathi").json()
    assert len(rows) == 1
    assert rows[0]["trek_name"] == "Netravathi"

    # Day filter.
    day_rows = client.get(f"/api/dashboard/bookings?range=all&day={today.isoformat()}").json()
    assert len(day_rows) == 2
