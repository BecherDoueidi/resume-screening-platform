"""Database engine/session setup.

Reads DATABASE_URL from the environment. Point it at Postgres for both local
development and production, e.g.:

    postgresql+psycopg2://screener:screener@localhost:5432/screener        (local)
    postgresql+psycopg2://user:pass@prod-host:5432/screener?sslmode=require (prod)

No default value is provided for DATABASE_URL in production-shaped code —
failing loudly on a missing DB config beats silently falling back to a file.
A sqlite fallback is offered ONLY when SCREENER_ALLOW_SQLITE=1 is set, for
environments (like CI smoke tests) that can't run a real Postgres server.
"""

from __future__ import annotations

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    if os.environ.get("SCREENER_ALLOW_SQLITE") == "1":
        return "sqlite:///./webapp/screener.db"
    raise RuntimeError(
        "DATABASE_URL is not set. Point it at a Postgres instance, e.g. "
        "postgresql+psycopg2://screener:screener@localhost:5432/screener "
        "(or set SCREENER_ALLOW_SQLITE=1 for a local sqlite fallback in dev/CI)."
    )


_engine = None
_SessionLocal: sessionmaker | None = None


def get_engine():
    global _engine
    if _engine is None:
        url = _database_url()
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        _engine = create_engine(url, pool_pre_ping=True, connect_args=connect_args)
    return _engine


def get_sessionmaker() -> sessionmaker:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), expire_on_commit=False)
    return _SessionLocal


def new_session() -> Session:
    return get_sessionmaker()()


def init_db() -> None:
    """Create all tables. Used for local/dev bootstrapping; production should use Alembic."""
    Base.metadata.create_all(get_engine())
