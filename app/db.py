"""Database session management."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from app.config import get_settings

_engine = None
_SessionLocal = None


def get_engine():
    """The engine, built once per process.

    On a long-lived server that is exactly right: one pool, reused. On Vercel
    every instance is its own process, so a pool per instance multiplies into
    the database's connection limit — a cold-start spike can exhaust it while
    each individual instance looks healthy. There, hold no connections between
    invocations and let the platform's pooler do the pooling.
    """
    global _engine
    if _engine is None:
        serverless = bool(os.getenv("VERCEL"))
        _engine = create_engine(
            get_settings().database_url,
            future=True,
            pool_pre_ping=True,
            **({"poolclass": NullPool} if serverless else {}),
        )
    return _engine


def get_session_factory():
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)
    return _SessionLocal


@contextmanager
def session_scope() -> Iterator[Session]:
    s = get_session_factory()()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()
