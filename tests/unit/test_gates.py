"""The gates themselves — ADR-0035.

`check_counts.py` and `check_no_mutants.py` are the two controls standing
between this repository and its two most embarrassing failures: publishing a
number nobody measured, and committing a live mutant. Both were written this
session, both have already caught real defects, and neither had a test.

That is the wrong way round. A gate nobody tests is a gate that can stop
working silently, and the whole point of a gate is that its silence means
something. `check_no_mutants` in particular fails OPEN in the worst way: if
its matching broke, it would print "No mutant" and exit 0 forever.

These tests drive the modules directly rather than the CLI, so a failure names
a function rather than an exit code.
"""
from __future__ import annotations

import sys

import pytest

from scripts import check_counts, check_no_mutants, dbutil

# The modules under test already know where the repository is; deriving it a
# second time here would be a second thing that can be wrong about it.
ROOT = check_no_mutants.ROOT


# ------------------------------------------------------------- check_counts
def test_a_matching_claim_passes():
    claim = check_counts.Claim("README.md", r"gap-closure plan, (\d+) ADRs",
                               check_counts.adr_count(), "the ADR count")
    assert check_counts.check([claim]) == []


def test_a_wrong_number_is_reported_with_both_values():
    claim = check_counts.Claim("README.md", r"gap-closure plan, (\d+) ADRs",
                               999, "the ADR count")
    problems = check_counts.check([claim])
    assert len(problems) == 1
    # Both numbers, or the reader cannot tell which end moved.
    assert "999" in problems[0] and str(check_counts.adr_count()) in problems[0]


def test_a_claim_that_has_moved_is_a_failure_not_a_pass():
    """The rule that makes the whole file honest.

    If a pattern matching nothing passed, deleting or rewording a sentence
    would switch off its own gate and the number would drift from then on with
    the checker still green.
    """
    claim = check_counts.Claim("README.md", r"a sentence nobody wrote (\d+)",
                               1, "an absent claim")
    problems = check_counts.check([claim])
    assert len(problems) == 1
    assert "moved or been deleted" in problems[0]


def test_every_app_package_is_named_in_the_repository_map():
    assert check_counts.unmapped_app_packages() == []


def test_the_measured_counts_are_positive():
    """A measurement that silently returns zero would make every claim fail in
    a way that reads like the README is wrong."""
    assert check_counts.adr_count() > 0
    assert check_counts.scenario_count() > 0
    assert check_counts.mutant_count() > 0
    assert (check_counts.e2e_test_count() or 0) > 0


# ---------------------------------------------------------- check_no_mutants
def test_a_clean_tree_reports_no_mutant(capsys):
    """Skipped rather than asserted when a mutation run is live: the working
    tree genuinely holds a mutant then, and asserting otherwise would make this
    test fail for the one reason it must not."""
    if (ROOT / ".mutation-in-progress").exists():
        pytest.skip("a mutation run holds the tree")
    assert check_no_mutants.main() == 0


def test_it_detects_a_mutant_it_is_shown(tmp_path, monkeypatch):
    """The load-bearing one.

    This gate fails OPEN: broken matching prints "No mutant" and exits 0
    forever, and nothing else in the repository would notice. So it is shown a
    file containing a real replacement string and must refuse it.
    """
    label, relpath, _find, replace = next(
        m for m in check_no_mutants.MUTATIONS if m[1] != check_no_mutants.SELF)

    monkeypatch.setattr(check_no_mutants, "_worktree",
                        lambda p: replace if p == relpath else "")
    monkeypatch.setattr(sys, "argv", ["check_no_mutants.py"])
    assert check_no_mutants.main() == 1


def test_it_reads_the_index_when_asked(monkeypatch):
    """`--staged` must read what a commit would RECORD, not what is on disk.
    A guard that checks the working tree while the index holds a mutant is a
    guard that watches the wrong thing."""
    seen: list[str] = []

    def fake_staged(relpath: str):
        seen.append(relpath)
        return ""

    monkeypatch.setattr(check_no_mutants, "_staged", fake_staged)
    monkeypatch.setattr(sys, "argv", ["check_no_mutants.py", "--staged"])
    assert check_no_mutants.main() == 0
    assert seen, "--staged never consulted the index"


def test_the_harness_itself_is_excluded():
    """`mutation_test.py` stores every replacement string as data, so a check
    that did not exclude it would refuse every commit forever."""
    assert check_no_mutants.SELF == "scripts/mutation_test.py"
    assert not any(m[1] == check_no_mutants.SELF
                   for m in check_no_mutants.MUTATIONS
                   if m[3] in (ROOT / check_no_mutants.SELF).read_text()
                   and m[1] != check_no_mutants.SELF)


# ------------------------------------------------------------------ dbutil
def test_sibling_url_changes_only_the_database():
    url = "postgresql+psycopg2://u:p@host:5432/merchantops"
    assert dbutil.sibling_url(url, "merchantops_eval") == (
        "postgresql+psycopg2://u:p@host:5432/merchantops_eval")
    assert dbutil.database_name(url) == "merchantops"


def test_a_working_database_name_is_refused():
    """The guard between `make eval` and somebody's open console.

    The evaluation suite drops and rebuilds the schema once per scenario. It
    inherited DATABASE_URL, which defaults to the development database, so a
    run while somebody had the console open destroyed what they were looking at
    167 times. The refusal is keyed on the name, because that is the only thing
    available before the damage.
    """
    with pytest.raises(RuntimeError) as e:
        dbutil.refuse_to_destroy_a_working_database(
            "postgresql://u:p@h:5432/merchantops",
            what="this drops the schema", instead="Use the scratch one.")
    # The name, what would have happened, and what to run instead -- a refusal
    # that says none of those is one somebody silences with the override.
    assert "merchantops" in str(e.value)
    assert "drops the schema" in str(e.value)
    assert "Use the scratch one." in str(e.value)


@pytest.mark.parametrize("suffix", dbutil.DISPOSABLE_SUFFIXES)
def test_every_disposable_suffix_is_allowed(suffix):
    """Driven from the tuple, so a suffix added without thought is still
    covered and one removed cannot leave a stale test passing."""
    url = f"postgresql://u:p@h:5432/merchantops{suffix}"
    assert dbutil.looks_disposable(url)
    dbutil.refuse_to_destroy_a_working_database(url, what="x", instead="y")


def test_the_ci_database_is_disposable():
    """`make ci` runs the whole suite against a `_ci` sibling and force-seeds
    it. A list that did not include the suffix would have refused the one
    command that exists to be run before pushing."""
    assert "_ci" in dbutil.DISPOSABLE_SUFFIXES


def test_the_override_is_the_only_way_past_it(monkeypatch):
    url = "postgresql://u:p@h:5432/merchantops"
    monkeypatch.setenv(dbutil.OVERRIDE_ENV, "1")
    dbutil.refuse_to_destroy_a_working_database(url, what="x", instead="y")


def test_the_seeder_is_deliberately_not_behind_the_name_check():
    """Seeding the development database is what `make seed` is FOR.

    A name check there refuses the command's primary use -- and `scripts/
    demo.py` prints that exact command as advice. The seeder is guarded by how
    much work it would destroy instead, which is the right question for a
    command whose target is meant to be the database you look at. Asserted
    rather than left to a comment, because "we tried this and backed it out" is
    the kind of thing somebody re-adds six months later.
    """
    seeder = (ROOT / "scripts" / "seed_data.py").read_text()
    assert "refuse_to_destroy_a_working_database" not in seeder
    assert "_refuse_if_work_would_be_lost" in seeder


def test_sibling_url_keeps_credentials_and_port():
    """The bug this prevents: an eval run pointed at the right database name on
    the wrong server, or at localhost with no password, failing with an error
    about authentication rather than about configuration."""
    url = "postgresql+psycopg2://user:secret@10.0.0.5:6543/prod"
    out = dbutil.sibling_url(url, "prod_eval")
    assert "user:secret@10.0.0.5:6543" in out
    assert out.endswith("/prod_eval")
