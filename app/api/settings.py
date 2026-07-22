"""Settings + proxy-test endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlmodel import Session

from app.db import get_session, get_settings
from app.schemas import ProxyTestResult, SettingsRead, SettingsUpdate
from app.services import is_ip_on_cooldown, proxy_config_from_settings
from app.portal.proxy import ProxyManager

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


@router.post("/proxy/test", response_model=ProxyTestResult)
def proxy_test(session: Session = Depends(get_session)) -> ProxyTestResult:
    s = get_settings(session)
    cfg = proxy_config_from_settings(s)
    mgr = ProxyManager(
        cfg,
        is_ip_on_cooldown=lambda ip: is_ip_on_cooldown(session, ip, s.ip_cooldown_days),
    )
    return ProxyTestResult(**mgr.test())
