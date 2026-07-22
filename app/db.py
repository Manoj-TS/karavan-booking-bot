"""Database engine, session helpers, and settings singleton access."""
from __future__ import annotations

from typing import Iterator

from sqlmodel import Session, SQLModel, create_engine, select

from app import config
from app.models import AppSetting

# check_same_thread=False: the booking worker thread shares the engine with the
# request threads. Each unit of work still opens its own Session.
engine = create_engine(
    config.DB_URL,
    echo=False,
    connect_args={"check_same_thread": False},
)


def init_db() -> None:
    """Create tables and ensure the singleton settings row exists."""
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        existing = session.get(AppSetting, 1)
        if existing is None:
            session.add(AppSetting(id=1))
            session.commit()


def get_session() -> Iterator[Session]:
    """FastAPI dependency: a request-scoped DB session."""
    with Session(engine) as session:
        yield session


def get_settings(session: Session) -> AppSetting:
    """Return the singleton settings row, creating it if missing."""
    settings = session.get(AppSetting, 1)
    if settings is None:
        settings = AppSetting(id=1)
        session.add(settings)
        session.commit()
        session.refresh(settings)
    return settings
