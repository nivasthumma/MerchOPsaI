"""Sharding the mutation corpus, and the merge's refusal to overstate.

The complete run is 3h40m against a 360-minute hosted ceiling. Splitting it is
straightforward; the part worth testing is the merge, because everything
downstream trusts the `complete` flag it writes — the README publishes the score
and `check_counts` gates the published number against it.

The failure this guards is the quiet one: shards finish, one is cancelled, and a
naive merge reports a perfect score over the mutants that happened to run.
"""
from __future__ import annotations

import json

import pytest

from scripts.merge_mutation_reports import merge
from scripts.mutation_test import MUTATIONS, shard_of


# --------------------------------------------------------------------------
# The split itself
# --------------------------------------------------------------------------
def _shard(index: int, total: int) -> list[str]:
    """The harness's own selection. Recomputing the formula here would make
    every test below pass regardless of what the harness actually does."""
    return [m[0] for m in shard_of(MUTATIONS, index, total)]


@pytest.mark.parametrize("total", [2, 4, 8])
def test_the_shards_cover_every_mutant_exactly_once(total):
    """No gaps and no overlaps. A gap is a control nobody tested reported as a
    pass; an overlap inflates the denominator."""
    seen = [label for i in range(1, total + 1) for label in _shard(i, total)]
    assert sorted(seen) == sorted(m[0] for m in MUTATIONS)
    assert len(seen) == len(set(seen))


def test_the_shards_are_within_one_of_each_other():
    """Round-robin rather than contiguous blocks. Mutants for one file sit
    together and cost about the same, so contiguous shards hand one job every
    slow mutant in a file."""
    sizes = [len(_shard(i, 4)) for i in range(1, 5)]
    assert max(sizes) - min(sizes) <= 1


# --------------------------------------------------------------------------
# The merge earns `complete` or refuses
# --------------------------------------------------------------------------
def _report(index: int, total: int, *, labels: list[str], tree: str = "abc123",
            clean: bool = True, defined: int | None = None) -> dict:
    return {
        "generated_at": f"2026-09-09T0{index}:00:00+00:00",
        "tree": tree, "tree_clean": clean, "complete": False,
        "defined": defined if defined is not None else len(MUTATIONS),
        "run": len(labels), "caught": len(labels), "survived": [],
        "shard": {"index": index, "total": total},
        "labels": labels,
        "mutants": [{"label": lab, "status": "CAUGHT", "caught_by": "", "scenarios": []}
                    for lab in labels],
    }


def _complete_set(total: int = 4) -> list[dict]:
    return [_report(i, total, labels=_shard(i, total)) for i in range(1, total + 1)]


def test_a_full_set_of_shards_merges_into_a_complete_run(tmp_path):
    merged = merge([_write(tmp_path, r) for r in _complete_set()])
    assert merged["complete"] is True
    assert merged["run"] == len(MUTATIONS)
    assert merged["caught"] == len(MUTATIONS)
    assert merged["merged_from"] == 4


def _write(tmp_path, report: dict):
    p = tmp_path / f"shard-{report['shard']['index']}.json"
    p.write_text(json.dumps(report))
    return p


def test_a_missing_shard_is_refused_rather_than_averaged(tmp_path):
    """The one that matters. Three of four jobs succeeding is not a complete
    run, and a merge that published it would report a perfect score over the
    mutants that happened to finish."""
    reports = _complete_set()[:-1]
    with pytest.raises(SystemExit) as e:
        merge([_write(tmp_path, r) for r in reports])
    assert "missing" in str(e.value)
    assert "[4]" in str(e.value)


def test_shards_from_different_commits_are_refused(tmp_path):
    """A score merged across trees describes no tree that exists."""
    reports = _complete_set()
    reports[2]["tree"] = "deadbee"
    with pytest.raises(SystemExit) as e:
        merge([_write(tmp_path, r) for r in reports])
    assert "different trees" in str(e.value)


def test_an_overlap_is_refused(tmp_path):
    """Two shards grading the same mutant inflates the denominator, which makes
    the score look better by making the corpus look bigger."""
    reports = _complete_set()
    reports[1]["labels"] = reports[0]["labels"]
    with pytest.raises(SystemExit) as e:
        merge([_write(tmp_path, r) for r in reports])
    assert "more than one shard" in str(e.value)


def test_a_gap_is_refused_even_with_every_shard_present(tmp_path):
    """All four jobs ran and one graded fewer than it should have. The count is
    checked against the corpus, not against the number of shards."""
    reports = _complete_set()
    reports[0]["labels"] = reports[0]["labels"][:-1]
    with pytest.raises(SystemExit) as e:
        merge([_write(tmp_path, r) for r in reports])
    assert "the corpus is" in str(e.value)


def test_a_dirty_shard_makes_the_whole_measurement_dirty(tmp_path):
    """One shard grading uncommitted edits means the merged score describes a
    tree nobody else has."""
    reports = _complete_set()
    reports[1]["tree_clean"] = False
    merged = merge([_write(tmp_path, r) for r in reports])
    assert merged["tree_clean"] is False


def test_a_survivor_in_any_shard_survives_the_merge(tmp_path):
    """The merge must not lose the finding that matters most."""
    reports = _complete_set()
    reports[2]["mutants"][0]["status"] = "SURVIVED"
    reports[2]["caught"] -= 1
    merged = merge([_write(tmp_path, r) for r in reports])
    assert len(merged["survived"]) == 1
    assert merged["caught"] == len(MUTATIONS) - 1


def test_the_timestamp_is_the_slowest_shard_not_the_first(tmp_path):
    """"When was this measured" is when the last one finished."""
    merged = merge([_write(tmp_path, r) for r in _complete_set()])
    assert merged["generated_at"].startswith("2026-09-09T04")


def test_an_unsharded_report_is_refused(tmp_path):
    """A whole-corpus run does not need merging, and merging one would stamp it
    with a shard count it never had."""
    r = _report(1, 1, labels=[m[0] for m in MUTATIONS])
    r["shard"] = None
    with pytest.raises(SystemExit) as e:
        merge([_write_plain(tmp_path, r)])
    assert "not a sharded run" in str(e.value)


def _write_plain(tmp_path, report: dict):
    p = tmp_path / "plain.json"
    p.write_text(json.dumps(report))
    return p
