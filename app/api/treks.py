"""Trek preset CRUD."""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from app.db import get_session
from app.models import Trek

router = APIRouter(prefix="/api/treks", tags=["treks"])


class TrekIn(BaseModel):
    name: str
    portal_trek_id: int
    district_id: int
    timeslot_mapping_id: int
    timeslot_id: int
    check_in: Optional[str] = None
    is_active: bool = True


@router.get("", response_model=List[Trek])
def list_treks(session: Session = Depends(get_session)) -> List[Trek]:
    return session.exec(select(Trek).order_by(Trek.name)).all()


@router.post("", response_model=Trek)
def create_trek(body: TrekIn, session: Session = Depends(get_session)) -> Trek:
    trek = Trek(**body.model_dump())
    session.add(trek)
    session.commit()
    session.refresh(trek)
    return trek


@router.patch("/{trek_id}", response_model=Trek)
def update_trek(trek_id: int, body: TrekIn, session: Session = Depends(get_session)) -> Trek:
    trek = session.get(Trek, trek_id)
    if not trek:
        raise HTTPException(404, "Trek not found.")
    for field, value in body.model_dump().items():
        setattr(trek, field, value)
    session.add(trek)
    session.commit()
    session.refresh(trek)
    return trek


@router.delete("/{trek_id}")
def delete_trek(trek_id: int, session: Session = Depends(get_session)) -> dict:
    trek = session.get(Trek, trek_id)
    if not trek:
        raise HTTPException(404, "Trek not found.")
    session.delete(trek)
    session.commit()
    return {"deleted": trek_id}
