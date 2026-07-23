"""Database engine, session helpers, and settings singleton access."""
from __future__ import annotations

from typing import Iterator, Optional

from sqlalchemy import inspect, text
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


def _default_sql(col) -> Optional[str]:
    """SQL literal for a column's scalar default, or None."""
    d = getattr(col, "default", None)
    if d is None or getattr(d, "is_callable", False):
        return None
    arg = getattr(d, "arg", None)
    if arg is None or callable(arg):
        return None
    if isinstance(arg, bool):
        return "1" if arg else "0"
    if isinstance(arg, (int, float)):
        return str(arg)
    return "'" + str(arg).replace("'", "''") + "'"


def _auto_migrate() -> None:
    """Additive-only migration: ADD COLUMN for any model field missing from an
    existing table. Keeps old SQLite DBs working as the schema grows (SQLite's
    create_all never alters existing tables)."""
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    with engine.begin() as conn:
        for table in SQLModel.metadata.sorted_tables:
            if table.name not in tables:
                continue
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in have:
                    continue
                coltype = col.type.compile(engine.dialect)
                ddl = f"ALTER TABLE {table.name} ADD COLUMN {col.name} {coltype}"
                default = _default_sql(col)
                if default is not None:
                    ddl += f" DEFAULT {default}"
                conn.execute(text(ddl))


def init_db() -> None:
    """Create tables, apply additive migrations, ensure the settings row exists."""
    SQLModel.metadata.create_all(engine)
    _auto_migrate()
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
