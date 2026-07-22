"""Cross-cutting query helpers: cooldowns, random account pick, proxy config."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from sqlalchemy import func
from sqlmodel import Session, select

from app.models import Account, AppSetting, UsedIp
from app.portal.proxy import ProxyConfig


def _cooldown_threshold(cooldown_days: int) -> date:
    # today-only == 1 day -> threshold is today.
    return date.today() - timedelta(days=max(0, cooldown_days - 1))


def is_ip_on_cooldown(session: Session, ip: str, cooldown_days: int = 1) -> bool:
    if not ip:
        return False
    threshold = _cooldown_threshold(cooldown_days)
    row = session.exec(
        select(UsedIp).where(UsedIp.ip == ip, UsedIp.used_date >= threshold)
    ).first()
    return row is not None


def record_used_ip(session: Session, ip: str, account_email: Optional[str] = None,
                   booking_id: Optional[int] = None) -> None:
    if not ip:
        return
    session.add(UsedIp(ip=ip, used_date=date.today(),
                       account_email=account_email, booking_id=booking_id))
    session.commit()


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


def mark_account_used(session: Session, account: Account, ip: Optional[str],
                      trek_name: Optional[str]) -> None:
    account.status = "booked"
    account.booked_date = date.today()
    account.last_used_date = date.today()
    account.last_used_ip = ip
    account.booked_trek = trek_name
    session.add(account)
    session.commit()


def proxy_config_from_settings(settings: AppSetting) -> ProxyConfig:
    return ProxyConfig(
        enabled=bool(settings.proxy_enabled),
        host=settings.proxy_host or "thehub.proxy-cheap.com",
        port=int(settings.proxy_port or 8080),
        user=settings.proxy_user or "",
        password=settings.proxy_pass or "",
        country=settings.proxy_country or "IN",
        session_lifetime=settings.proxy_session_lifetime or "30m",
        require_country=settings.require_country or "IN",
    )
