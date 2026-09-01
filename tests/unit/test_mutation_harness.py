"""The mutation harness restores what a killed run left behind — ADR-0035.

`scripts/mutation_test.py` reverts each mutation in a `finally`, which only runs
if the process reaches it. SIGKILL does not, and a killed run then leaves a
rewritten safety control on disk that reads exactly like source somebody wrote.

That has happened twice — once leaving every HIGH-risk action auto-approving in
a working tree — and both times it was found by grepping for `# MUTANT`, which
is not a control. These tests cover the recovery that replaced the grep.
"""
from __future__ import annotations

import json

import pytest

from scripts.mutation_test import _hold, _recover


@pytest.fixture
def lock(tmp_path):
    return tmp_path / ".mutation-in-progress"


def test_no_lock_means_nothing_to_recover(lock):
    assert _recover(lock) is True


def test_a_killed_run_is_restored_from_the_lock(lock, tmp_path, monkeypatch):
    """The case that has cost real time twice."""
    import scripts.mutation_test as mt

    monkeypatch.setattr(mt, "ROOT", tmp_path)
    source = tmp_path / "engine.py"
    original = "if owner != caller:\n    deny()\n"
    source.write_text(original)

    # A run records the original, applies the mutant, and is killed.
    _hold(lock, "engine.py", original)
    source.write_text("if False:  # MUTANT\n    deny()\n")

    assert _recover(lock) is True
    assert source.read_text() == original
    assert not lock.exists()


def test_recovery_is_exact_rather_than_a_best_effort_repair(lock, tmp_path, monkeypatch):
    """Restoring means byte-for-byte, including anything the mutant deleted."""
    import scripts.mutation_test as mt

    monkeypatch.setattr(mt, "ROOT", tmp_path)
    source = tmp_path / "rules.py"
    original = "AND signature_valid = true\n# a trailing comment the mutant removes\n"
    source.write_text(original)

    _hold(lock, "rules.py", original)
    source.write_text("AND true  -- MUTANT\n")

    _recover(lock)
    assert source.read_text() == original


def test_a_lock_recorded_before_the_write_restores_to_a_no_op(lock, tmp_path, monkeypatch):
    """Killed between recording and mutating: the file is already correct.

    `_hold` runs before `write_text` precisely so this window restores to what
    is already there rather than to something stale.
    """
    import scripts.mutation_test as mt

    monkeypatch.setattr(mt, "ROOT", tmp_path)
    source = tmp_path / "engine.py"
    original = "unchanged\n"
    source.write_text(original)

    _hold(lock, "engine.py", original)      # killed here, before the mutation

    assert _recover(lock) is True
    assert source.read_text() == original


def test_a_lock_with_no_payload_still_refuses(lock):
    """Either a run is happening now, or it predates recovery.

    Neither can be restored from, so the honest answer is to stop and say so
    rather than to assume the tree is clean.
    """
    _hold(lock, None, None)
    assert _recover(lock) is False
    assert lock.exists()                     # not cleared: nothing was resolved


def test_an_unreadable_lock_refuses_rather_than_guessing(lock):
    lock.write_text("Mutation test in progress.\n")   # the pre-ADR-0035 format
    assert _recover(lock) is False


def test_the_lock_holds_enough_to_restore_from(lock):
    _hold(lock, "app/policy/engine.py", "the original text")
    held = json.loads(lock.read_text())
    assert held["file"] == "app/policy/engine.py"
    assert held["original"] == "the original text"


# ------------------------------------------------ the leftover-artifact check
def test_the_artifact_check_ignores_work_this_run_never_touched(
        tmp_path, monkeypatch, capsys):
    """The remedy it prints is `git checkout --`, so what it names matters.

    It used to call every file dirty under app/ a mutation artifact. On a clean
    tree that is right; on the ordinary tree of someone mid-change — the only
    tree anyone runs this from — it named their uncommitted work and told them
    to discard it.
    """
    import scripts.mutation_test as mt

    monkeypatch.setattr(mt, "ROOT", tmp_path)
    monkeypatch.setattr(mt, "_ORIGINALS", {})

    # Uncommitted work in a file no mutant targets.
    (tmp_path / "unrelated.py").write_text("work in progress\n")

    mt._verify_tree_restored()
    assert "MUTATION ARTIFACTS" not in capsys.readouterr().out


def test_the_artifact_check_still_reports_a_file_left_mutated(
        tmp_path, monkeypatch, capsys):
    import scripts.mutation_test as mt

    monkeypatch.setattr(mt, "ROOT", tmp_path)
    source = tmp_path / "engine.py"
    source.write_text("if owner != caller:\n")
    monkeypatch.setattr(mt, "_ORIGINALS", {"engine.py": "if owner != caller:\n"})

    source.write_text("if False:  # MUTANT\n")     # the revert never ran
    mt._verify_tree_restored()

    out = capsys.readouterr().out
    assert "MUTATION ARTIFACTS" in out
    assert "engine.py" in out
