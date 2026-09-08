"""Creating a database, and refusing to destroy one, in one place.

`scripts/run_e2e.sh` carried the create as an inline heredoc and
`scripts/run_scenarios.py` needed the same thing. Two copies of "create the
database if it is not there" is two places to get the admin-connection URL
wrong, and the failure when it is wrong points at psycopg2 rather than at the
missing step.

The refusal is here rather than in `app/eval/runner.py`, where it was written,
so that the next command which drops a schema can reach it instead of growing
a second copy of the suffix list.

It is deliberately NOT applied to `scripts/seed_data.py`. Seeding the
development database is what that command is for -- the demo instructions say
to run it -- so a name check there refuses its primary use. What guards the
seeder is the count of agent tasks it would destroy, which is the right
question for a command whose target is meant to be the database you look at.
"""
from __future__ import annotations

import os
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import create_engine, text

# A database any of these may drop and rebuild. The suffixes are the ones
# `scripts/run_scenarios.py`, `tests/conftest.py`, `scripts/run_e2e.sh` and the
# `ci` target create for themselves; a name without one is somebody's working
# database until proven otherwise.
DISPOSABLE_SUFFIXES = ("_eval", "_test", "_e2e", "_scratch", "_ci")

# One override for every caller. Named for what it permits rather than for the
# suite that first needed it, because the next destructive path would otherwise
# invent a second variable meaning the same thing.
OVERRIDE_ENV = "MERCHANTOPS_ALLOW_DESTRUCTIVE_EVAL"


def looks_disposable(url: str) -> bool:
    """Whether this database's name says it is somebody's scratch copy."""
    return database_name(url).endswith(DISPOSABLE_SUFFIXES)


def refuse_to_destroy_a_working_database(url: str, *, what: str, instead: str) -> None:
    """Raise unless `url` names a database somebody is willing to lose.

    `what` is the destructive thing about to happen and `instead` is the
    command that does it safely -- both in the message, because a refusal that
    does not say what to run instead is a refusal somebody works around with
    the override.
    """
    if os.environ.get(OVERRIDE_ENV):
        return
    if looks_disposable(url):
        return
    name = database_name(url)
    raise RuntimeError(
        f"Refusing to run against {name!r}: {what}, and that name does not end "
        f"in any of {', '.join(DISPOSABLE_SUFFIXES)}, so it looks like a "
        f"working database rather than a scratch one.\n\n"
        f"{instead}\n\n"
        f"Set {OVERRIDE_ENV}=1 if this really is disposable."
    )


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
