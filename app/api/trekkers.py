"""Trekker CRUD."""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, or_, select

from app.db import get_session
from app.models import Trekker
from app.portal.ids import detect_id_type, to_canonical

router = APIRouter(prefix="/api/trekkers", tags=["trekkers"])


class TrekkerIn(BaseModel):
    name: str
    age: Optional[int] = None
    gender: Optional[str] = None
    mobile_no: Optional[str] = None
    govt_id_type: Optional[str] = None
    govt_id: Optional[str] = None


def _canon(body: TrekkerIn) -> dict:
    d = body.model_dump()
    gid = (d.get("govt_id") or "").strip().upper() or None
    gtype = to_canonical(d.get("govt_id_type")) if d.get("govt_id_type") else None
    if gid and not gtype:
        gtype = detect_id_type(gid)
    d["govt_id"] = gid
    d["govt_id_type"] = gtype
    return d


@router.get("", response_model=List[Trekker])
def list_trekkers(q: Optional[str] = None,
                  session: Session = Depends(get_session)) -> List[Trekker]:
    stmt = select(Trekker)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(Trekker.name.ilike(like), Trekker.govt_id.ilike(like)))
    return session.exec(stmt.order_by(Trekker.name)).all()


@router.post("", response_model=Trekker)
def create_trekker(body: TrekkerIn, session: Session = Depends(get_session)) -> Trekker:
    d = _canon(body)
    if d["govt_id"]:
        existing = session.exec(select(Trekker).where(Trekker.govt_id == d["govt_id"])).first()
        if existing:
            raise HTTPException(409, "A trekker with that government id already exists.")
    trekker = Trekker(**d)
    session.add(trekker)
    session.commit()
    session.refresh(trekker)
    return trekker


@router.patch("/{trekker_id}", response_model=Trekker)
def update_trekker(trekker_id: int, body: TrekkerIn,
                   session: Session = Depends(get_session)) -> Trekker:
    trekker = session.get(Trekker, trekker_id)
    if not trekker:
        raise HTTPException(404, "Trekker not found.")
    for field, value in _canon(body).items():
        setattr(trekker, field, value)
    session.add(trekker)
    session.commit()
    session.refresh(trekker)
    return trekker


@router.delete("/{trekker_id}")
def delete_trekker(trekker_id: int, session: Session = Depends(get_session)) -> dict:
    trekker = session.get(Trekker, trekker_id)
    if not trekker:
        raise HTTPException(404, "Trekker not found.")
    session.delete(trekker)
    session.commit()
    return {"deleted": trekker_id}
