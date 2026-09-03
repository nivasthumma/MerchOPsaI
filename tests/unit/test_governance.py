"""Model governance — MerchantOps §42.

The specification is a worked example: v1 scores 23/25, v2 scores 24/25, and v2
must not be promoted because it introduced one unauthorised refund. Every test
here is a way of getting that wrong.
"""
from __future__ import annotations

from app.eval.governance import compare, render


def _r(sid, passed, critical=False):
    return {"scenario_id": sid, "passed": passed, "metrics": {"critical": critical}}


def _report(provider, model, results):
    return {"provider": provider, "model": model, "results": results,
            "passed": sum(1 for r in results if r["passed"]), "total": len(results)}


def test_the_specs_own_worked_example():
    """v1 23/25, v2 24/25, one critical regression. §42 says do not promote."""
    v1 = _report("deterministic", "v1",
                 [_r("SEC-01", True, critical=True)]
                 + [_r(f"S{i}", i >= 2) for i in range(24)])
    v2 = _report("anthropic", "v2",
                 [_r("SEC-01", False, critical=True)]
                 + [_r(f"S{i}", True) for i in range(24)])

    c = compare(v1, v2)
    assert c.baseline_passed == 23
    assert c.candidate_passed == 24        # the candidate scores HIGHER
    assert c.promote is False              # and is refused anyway
    assert [d.scenario_id for d in c.critical_regressions] == ["SEC-01"]
    assert "not better" in c.blockers[0]


def test_a_higher_score_never_buys_a_critical_regression():
    """There is deliberately no threshold. A rule with a tolerance is an
    aggregate wearing a different hat."""
    baseline = _report("a", "1", [_r("SEC-01", True, critical=True)]
                       + [_r(f"S{i}", False) for i in range(50)])
    candidate = _report("b", "2", [_r("SEC-01", False, critical=True)]
                        + [_r(f"S{i}", True) for i in range(50)])
    c = compare(baseline, candidate)
    assert c.candidate_passed - c.baseline_passed == 49
    assert c.promote is False


def test_a_strict_improvement_is_promoted():
    baseline = _report("a", "1", [_r("S1", True, critical=True), _r("S2", False)])
    candidate = _report("b", "2", [_r("S1", True, critical=True), _r("S2", True)])
    c = compare(baseline, candidate)
    assert c.promote is True
    assert c.blockers == []
    assert [d.scenario_id for d in c.improvements] == ["S2"]


def test_a_non_critical_regression_is_weighed_not_ignored():
    """§42 makes critical scenarios absolute. It does not make everything else
    free — a candidate that breaks more than it fixes is not an improvement."""
    baseline = _report("a", "1", [_r("S1", True), _r("S2", True), _r("S3", False)])
    candidate = _report("b", "2", [_r("S1", False), _r("S2", False), _r("S3", True)])
    c = compare(baseline, candidate)
    assert not c.critical_regressions
    assert c.promote is False
    assert "2 scenario(s) regressed against 1 improved" in c.blockers[0]


def test_an_even_trade_of_non_critical_scenarios_is_allowed():
    baseline = _report("a", "1", [_r("S1", True), _r("S2", False)])
    candidate = _report("b", "2", [_r("S1", False), _r("S2", True)])
    c = compare(baseline, candidate)
    assert c.promote is True


def test_a_candidate_cannot_dodge_the_gate_by_downgrading_a_scenario():
    """Criticality is a property of the scenario. Taking either run's word for
    it means a report that quietly demotes a safety scenario still trips the
    gate."""
    baseline = _report("a", "1", [_r("SEC-01", True, critical=True)])
    candidate = _report("b", "2", [_r("SEC-01", False, critical=False)])
    c = compare(baseline, candidate)
    assert c.critical_regressions
    assert c.promote is False


def test_runs_over_different_scenario_sets_are_not_comparable():
    """Quietly comparing the intersection would report a verdict about a suite
    neither model was measured on."""
    baseline = _report("a", "1", [_r("S1", True), _r("S2", True)])
    candidate = _report("b", "2", [_r("S1", True), _r("S3", True)])
    c = compare(baseline, candidate)
    assert c.promote is False
    assert any("different scenarios" in b for b in c.blockers)


def test_an_identical_run_promotes_and_says_nothing_changed():
    rows = [_r("S1", True, critical=True), _r("S2", True)]
    c = compare(_report("a", "1", rows), _report("b", "2", list(rows)))
    assert c.promote is True
    assert "No scenario changed outcome." in render(c)


def test_the_rendering_names_the_critical_regression_loudly():
    v1 = _report("a", "1", [_r("SEC-01", True, critical=True)])
    v2 = _report("b", "2", [_r("SEC-01", False, critical=True)])
    out = render(compare(v1, v2))
    assert "DO NOT PROMOTE" in out
    assert "[CRITICAL]" in out
    assert "SEC-01" in out


def test_the_comparison_refuses_without_two_real_providers():
    """Running one provider twice and calling it a model comparison would be a
    governance decision about a comparison that never happened."""
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    r = subprocess.run([sys.executable, "scripts/compare_models.py"],
                       cwd=root, capture_output=True, text=True,
                       env={**__import__("os").environ, "PYTHONPATH": str(root)})
    assert r.returncode == 2, r.stdout
    assert "Refusing to compare" in r.stdout
