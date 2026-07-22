"""Account CRUD + next-available (random) endpoint."""
from __future__ import annotations

from datetime import date
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, func, select

from app.db import get_session, get_settings
from app.models import Account
from app.services import pick_random_available_account

router = APIRouter(prefix="/api/accounts", tags=["accounts"])


class AccountIn(BaseModel):
    email: str
    password: Optional[str] = None
    status: str = "available"
    notes: Optional[str] = None


class AccountPatch(BaseModel):
    password: Optional[str] = None
    status: Optional[str] = None
    notes: Optional[str] = None


@router.get("", response_model=List[Account])
def list_accounts(status: Optional[str] = None,
                  session: Session = Depends(get_session)) -> List[Account]:
    stmt = select(Account)
    if status:
        stmt = stmt.where(Account.status == status)
    return session.exec(stmt.order_by(Account.email)).all()


@router.get("/summary")
def accounts_summary(session: Session = Depends(get_session)) -> dict:
    total = session.exec(select(func.count()).select_from(Account)).one()
    by_status = {}
    for st in ("available", "booked", "disabled"):
        by_status[st] = session.exec(
            select(func.count()).select_from(Account).where(Account.status == st)
        ).one()
    used_today = session.exec(
        select(func.count()).select_from(Account).where(Account.last_used_date == date.today())
    ).one()
    return {"total": total, "by_status": by_status, "used_today": used_today}


@router.get("/next-available", response_model=Optional[Account])
def next_available(session: Session = Depends(get_session)) -> Optional[Account]:
    settings = get_settings(session)
    return pick_random_available_account(session, settings.account_cooldown_days)


@router.post("", response_model=Account)
def create_account(body: AccountIn, session: Session = Depends(get_session)) -> Account:
    email = body.email.strip().lower()
    if session.exec(select(Account).where(Account.email == email)).first():
        raise HTTPException(409, "An account with that email already exists.")
    acc = Account(email=email, password=body.password, status=body.status, notes=body.notes)
    session.add(acc)
    session.commit()
    session.refresh(acc)
    return acc


@router.patch("/{account_id}", response_model=Account)
def update_account(account_id: int, patch: AccountPatch,
                   session: Session = Depends(get_session)) -> Account:
    acc = session.get(Account, account_id)
    if not acc:
        raise HTTPException(404, "Account not found.")
    for field, value in patch.model_dump(exclude_unset=True).items():
        setattr(acc, field, value)
    session.add(acc)
    session.commit()
    session.refresh(acc)
    return acc


@router.post("/{account_id}/reset", response_model=Account)
def reset_account(account_id: int, session: Session = Depends(get_session)) -> Account:
    """Clear today's used-flag so the account is available again."""
    acc = session.get(Account, account_id)
    if not acc:
        raise HTTPException(404, "Account not found.")
    acc.status = "available"
    acc.booked_date = None
    acc.last_used_date = None
    session.add(acc)
    session.commit()
    session.refresh(acc)
    return acc


@router.delete("/{account_id}")
def delete_account(account_id: int, session: Session = Depends(get_session)) -> dict:
    acc = session.get(Account, account_id)
    if not acc:
        raise HTTPException(404, "Account not found.")
    session.delete(acc)
    session.commit()
    return {"deleted": account_id}
