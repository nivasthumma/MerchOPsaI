"""Creating a database, in one place.

`scripts/run_e2e.sh` carried this as an inline heredoc and
`scripts/run_scenarios.py` needed the same thing. Two copies of "create the
database if it is not there" is two places to get the admin-connection URL
wrong, and the failure when it is wrong points at psycopg2 rather than at the
missing step.
"""
from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import create_engine, text


def sibling_url(url: str, name: str) -> str:
    """The same server and credentials, a different database."""
    return urlunsplit(urlsplit(url)._replace(path=f"/{name}"))


def database_name(url: str) -> str:
    return urlsplit(url).path.lstrip("/")


def ensure_database(url: str) -> bool:
    """Create the database in `url` if it is not there. Returns whether it was
    created.

    Connects to `postgres` to do it, because you cannot create a database from
    inside the one you are creating. `seed_data.py` builds the SCHEMA and
    assumes the database already exists, which is a reasonable split and an
    unhelpful error when nobody has run this first.
    """
    target = database_name(url)
    engine = create_engine(sibling_url(url, "postgres"),
                           isolation_level="AUTOCOMMIT", future=True)
    try:
        with engine.connect() as c:
            exists = c.execute(text("SELECT 1 FROM pg_database WHERE datname = :n"),
                               {"n": target}).scalar()
            if exists:
                return False
            # Identifiers cannot be bound. The name comes from our own
            # configuration and never from a request.
            c.execute(text(f'CREATE DATABASE "{target}"'))
            return True
    finally:
        engine.dispose()
