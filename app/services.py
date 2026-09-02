"""Cross-cutting query helpers: cooldowns, random account pick."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from sqlalchemy import func
from sqlmodel import Session, select

from app.models import Account


def _cooldown_threshold(cooldown_days: int) -> date:
    # today-only == 1 day -> threshold is today.
    return date.today() - timedelta(days=max(0, cooldown_days - 1))


def pick_random_available_account(session: Session, cooldown_days: int = 1
                                  ) -> Optional[Account]:
    """A random account that is available and not used within the cooldown."""
    threshold = _cooldown_threshold(cooldown_days)
    stmt = (
        select(Account)
        .where(Account.status == "available")
        .where((Account.last_used_date == None) | (Account.last_used_date < threshold))  # noqa: E711
        .order_by(func.random())
        .limit(1)
    )
    return session.exec(stmt).first()


def mark_account_used(session: Session, account: Account, trek_name: Optional[str]) -> None:
    account.status = "booked"
    account.booked_date = date.today()
    account.last_used_date = date.today()
    account.booked_trek = trek_name
    session.add(account)
    session.commit()
