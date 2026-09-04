"""Refuse to run while a mutation-test run has a mutant applied to the source.

`scripts/mutation_test.py` rewrites safety controls in place — it is how the
evaluation suite is graded — and reverts each one in a `finally`. A process that
is killed between the write and the revert never runs that `finally`, and what
it leaves on disk is a disabled control that looks exactly like source somebody
wrote.

The harness already recovers: it writes the original into `.mutation-in-progress`
*before* applying the mutation, and the next run restores from it. What was
missing is anything covering the interval in between. In that window the
application starts normally, the test suite passes normally, and the mutant can
be committed, deployed or demonstrated. That is not hypothetical either — this
module exists because a run was interrupted and left cross-merchant isolation
disabled in `app/policy/engine.py`, and nothing anywhere said so.

So: while the marker is on disk, importing the application raises. The one
process that must be exempt is the harness itself, which applies mutants on
purpose; it marks its own subprocesses with MERCHANTOPS_MUTATION_RUN and is let
through. Anything else — `make api`, `make eval`, a bare `pytest`, a deployment
build — stops with the file to restore named in the message.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MARKER = ROOT / ".mutation-in-progress"

#: Set by scripts/mutation_test.py on the subprocesses it spawns. The harness is
#: the only caller for which an applied mutant is the intended state.
HARNESS_ENV = "MERCHANTOPS_MUTATION_RUN"


class MutationInProgress(RuntimeError):
    """A mutant may be applied to the working tree."""


def _held_file(marker: Path) -> str | None:
    """Which file the marker says is mutated, if it says.

    Read defensively. The marker is written by another process and may be
    half-written, empty, or from an older format; none of that should turn a
    clear refusal into a confusing traceback about JSON.
    """
    import json

    try:
        held = json.loads(marker.read_text())
    except (OSError, ValueError):
        return None
    return held.get("relpath") if isinstance(held, dict) else None


def check(*, marker: Path | None = None) -> None:
    """Raise if a mutation run is in progress and we are not it."""
    marker = MARKER if marker is None else marker
    if not marker.exists() or os.environ.get(HARNESS_ENV):
        return

    where = _held_file(marker)
    raise MutationInProgress(
        f"{marker.name} is present: a mutation test was interrupted and the "
        f"working tree may contain a disabled safety control"
        + (f", in {where}" if where else "")
        + ".\n\n"
        "Restore it before doing anything else:\n"
        "    make mutants        # recovers from the marker, then runs\n"
        "or, to recover without running the suite:\n"
        f"    git checkout -- {where or 'app scripts'} && rm {marker.name}\n\n"
        f"Set {HARNESS_ENV}=1 only if you are the mutation harness."
    )
