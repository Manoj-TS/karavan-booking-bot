"""App settings endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlmodel import Session

from app.db import get_session, get_settings
from app.schemas import SettingsRead, SettingsUpdate

router = APIRouter(prefix="/api", tags=["settings"])


@router.get("/settings", response_model=SettingsRead)
def read_settings(session: Session = Depends(get_session)) -> SettingsRead:
    s = get_settings(session)
    return SettingsRead.model_validate(s, from_attributes=True)


@router.put("/settings", response_model=SettingsRead)
def update_settings(update: SettingsUpdate,
                    session: Session = Depends(get_session)) -> SettingsRead:
    s = get_settings(session)
    for field, value in update.model_dump(exclude_unset=True).items():
        setattr(s, field, value)
    session.add(s)
    session.commit()
    session.refresh(s)
    return SettingsRead.model_validate(s, from_attributes=True)
