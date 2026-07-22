"""Tickets: refresh from the portal, list/search, download, per-visitor cancel."""
from __future__ import annotations

from datetime import date
from typing import List, Optional

import requests
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlmodel import Session, or_, select

from app import config
from app.db import get_session, get_settings
from app.models import Account, Ticket
from app.portal import tickets as tickets_mod
from app.portal.client import TrekPortalClient

router = APIRouter(prefix="/api/tickets", tags=["tickets"])


def _login_client(account: Account, settings) -> Optional[TrekPortalClient]:
    if config.DRY_RUN:
        return None
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0", "Accept-Language": "en-US,en;q=0.9"})
    client = TrekPortalClient(session, config.BASE_URL, account_email=account.email,
                              sessions_dir=config.SESSIONS_DIR,
                              ocr_api_key=settings.ocr_space_api_key or config.OCR_SPACE_KEY)
    pwd = account.password or settings.shared_default_password or ""
    if not client.ensure_logged_in(account.email, pwd):
        return None
    return client


def _upsert_ticket(session: Session, email: str, tk: dict) -> None:
    existing = session.exec(
        select(Ticket).where(Ticket.account_email == email,
                             Ticket.portal_ref == tk["portal_ref"])
    ).first()
    fields = dict(
        cancel_ref=tk.get("cancel_ref"), section=tk.get("section", "booked"),
        trek=tk.get("trek"), check_in=tk.get("check_in"),
        trekker_names=tk.get("trekker_names") or [],
        cancellable=bool(tk.get("cancellable")),
    )
    if existing:
        for k, v in fields.items():
            setattr(existing, k, v)
        session.add(existing)
    else:
        session.add(Ticket(account_email=email, portal_ref=tk["portal_ref"], **fields))


class RefreshResult(BaseModel):
    account: str
    found: int
    error: Optional[str] = None


@router.post("/refresh/{account_id}", response_model=RefreshResult)
def refresh_account(account_id: int, session: Session = Depends(get_session)) -> RefreshResult:
    account = session.get(Account, account_id)
    if not account:
        raise HTTPException(404, "Account not found.")
    if config.DRY_RUN:
        raise HTTPException(400, "Ticket refresh needs live mode (DRY_RUN is on).")
    settings = get_settings(session)
    client = _login_client(account, settings)
    if not client:
        return RefreshResult(account=account.email, found=0, error="Login failed.")
    found = tickets_mod.fetch_account_tickets(client.session, config.BASE_URL)
    for tk in found:
        _upsert_ticket(session, account.email, tk)
    session.commit()
    return RefreshResult(account=account.email, found=len(found))


@router.get("", response_model=List[Ticket])
def list_tickets(q: Optional[str] = None, section: Optional[str] = None,
                 session: Session = Depends(get_session)) -> List[Ticket]:
    stmt = select(Ticket)
    if section:
        stmt = stmt.where(Ticket.section == section)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(Ticket.trek.ilike(like), Ticket.portal_ref.ilike(like),
                              Ticket.account_email.ilike(like)))
    return session.exec(stmt.order_by(Ticket.updated_at.desc())).all()


@router.get("/{ticket_id}/download")
def download_ticket(ticket_id: int, session: Session = Depends(get_session)):
    ticket = session.get(Ticket, ticket_id)
    if not ticket:
        raise HTTPException(404, "Ticket not found.")
    if ticket.ticket_pdf_path and _exists(ticket.ticket_pdf_path):
        return FileResponse(ticket.ticket_pdf_path, filename=f"ticket_{ticket.portal_ref}.pdf")
    account = session.exec(select(Account).where(Account.email == ticket.account_email)).first()
    if not account:
        raise HTTPException(404, "Owning account not found.")
    settings = get_settings(session)
    client = _login_client(account, settings)
    if not client:
        raise HTTPException(502, "Could not log in the ticket's account.")
    files = client.download_files(ticket.portal_ref, config.ARTIFACTS_DIR / "tickets")
    ticket.ticket_pdf_path = files.get("ticket")
    ticket.receipt_pdf_path = files.get("receipt")
    session.add(ticket)
    session.commit()
    if not ticket.ticket_pdf_path:
        raise HTTPException(502, "Ticket download failed.")
    return FileResponse(ticket.ticket_pdf_path, filename=f"ticket_{ticket.portal_ref}.pdf")


class CancelInfo(BaseModel):
    ref: str
    visitors: List[dict]
    error: Optional[str] = None


@router.get("/{ticket_id}/cancel-info", response_model=CancelInfo)
def cancel_info(ticket_id: int, session: Session = Depends(get_session)) -> CancelInfo:
    ticket, account, settings = _ticket_ctx(session, ticket_id)
    ref = ticket.cancel_ref or ticket.portal_ref
    client = _login_client(account, settings)
    if not client:
        raise HTTPException(502, "Could not log in the ticket's account.")
    token, visitors, err = tickets_mod.fetch_cancel_page(client.session, config.BASE_URL, ref)
    return CancelInfo(ref=ref, visitors=visitors, error=err)


class CancelRequest(BaseModel):
    visitor_ids: List[str]


@router.post("/{ticket_id}/cancel")
def cancel_ticket(ticket_id: int, req: CancelRequest,
                  session: Session = Depends(get_session)) -> dict:
    ticket, account, settings = _ticket_ctx(session, ticket_id)
    ref = ticket.cancel_ref or ticket.portal_ref
    client = _login_client(account, settings)
    if not client:
        raise HTTPException(502, "Could not log in the ticket's account.")
    ok, msg = tickets_mod.cancel_visitors(client.session, config.BASE_URL, ref, req.visitor_ids)
    if ok:
        # Re-check remaining visitors; if none, mark the ticket cancelled.
        _, remaining, _ = tickets_mod.fetch_cancel_page(client.session, config.BASE_URL, ref)
        if not remaining:
            ticket.section = "cancelled"
            ticket.cancellable = False
            session.add(ticket)
            session.commit()
    return {"ok": ok, "message": msg}


def _ticket_ctx(session: Session, ticket_id: int):
    ticket = session.get(Ticket, ticket_id)
    if not ticket:
        raise HTTPException(404, "Ticket not found.")
    if config.DRY_RUN:
        raise HTTPException(400, "Ticket actions need live mode (DRY_RUN is on).")
    account = session.exec(select(Account).where(Account.email == ticket.account_email)).first()
    if not account:
        raise HTTPException(404, "Owning account not found.")
    return ticket, account, get_settings(session)


def _exists(path: str) -> bool:
    from pathlib import Path
    try:
        return Path(path).exists()
    except Exception:
        return False
