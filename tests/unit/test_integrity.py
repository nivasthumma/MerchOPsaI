"""The guard that refuses to run while a mutant may be applied.

`app/integrity.py` exists because a killed mutation run left cross-merchant
isolation disabled in `app/policy/engine.py` and nothing said so: the API
started, the suite passed, and the only thing that would have caught it was
somebody reading the diff. These tests are what stop that guard from being
quietly removed or from firing on the one process that must be exempt.
"""
from __future__ import annotations

import pytest

from app.integrity import HARNESS_ENV, MutationInProgress, check


def test_a_clean_tree_passes(tmp_path):
    check(marker=tmp_path / "absent")


def test_a_present_marker_stops_the_process(tmp_path):
    marker = tmp_path / ".mutation-in-progress"
    marker.write_text("{}")
    with pytest.raises(MutationInProgress):
        check(marker=marker)


def test_the_refusal_names_the_file_to_restore(tmp_path):
    """The message has to be actionable at 2am. A guard that says "something is
    wrong" and not "restore app/policy/engine.py" is a guard people work around."""
    marker = tmp_path / ".mutation-in-progress"
    marker.write_text('{"relpath": "app/policy/engine.py"}')
    with pytest.raises(MutationInProgress) as exc:
        check(marker=marker)
    assert "app/policy/engine.py" in str(exc.value)
    assert "git checkout --" in str(exc.value)


def test_a_marker_that_says_nothing_useful_still_stops_the_process(tmp_path):
    """Half-written, empty, or an older format. The marker is produced by
    another process that may have died mid-write, so it cannot be trusted to
    parse -- but an unparseable marker is still a marker, and the answer is
    still no."""
    for content in ("", "not json", "[]", '{"relpath": null}'):
        marker = tmp_path / ".mutation-in-progress"
        marker.write_text(content)
        with pytest.raises(MutationInProgress):
            check(marker=marker)


def test_the_harness_itself_is_let_through(tmp_path, monkeypatch):
    """The mutation harness applies mutants on purpose and runs the suite
    against them. If this exemption breaks, mutation testing stops working
    entirely -- which is why it is asserted rather than assumed."""
    marker = tmp_path / ".mutation-in-progress"
    marker.write_text('{"relpath": "app/policy/engine.py"}')
    monkeypatch.setenv(HARNESS_ENV, "1")
    check(marker=marker)


def test_the_harness_marks_the_subprocesses_it_spawns():
    """The exemption above is only safe because the harness sets the flag on the
    processes that run the suite. Read out of the source rather than executed:
    running it means a fifty-minute mutation run."""
    import inspect

    import scripts.mutation_test as harness

    for fn in (harness.run_suite, harness.run_tests):
        src = inspect.getsource(fn)
        assert "HARNESS_ENV" in src, (
            f"{fn.__name__} spawns the suite without marking itself as the "
            f"harness; app.integrity will refuse to import inside it.")


def test_the_harness_and_the_guard_name_the_same_file():
    """Two constants for one path is how a guard ends up watching a file nobody
    writes."""
    import scripts.mutation_test as harness
    from app.integrity import MARKER

    assert harness.LOCK == MARKER
