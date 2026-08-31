"""Model governance — MerchantOps §42.

    A model change should trigger evaluation.
    ...
    Overall score alone is insufficient.
    Critical safety scenarios can be release blockers.

§42's worked example is the whole specification: v1 scores 23/25, v2 scores
24/25, and v2 must **not** be promoted because it introduced one unauthorised
refund. A candidate that is better on average and worse on a safety scenario is
not better.

CI already gates a single run on critical scenarios. That answers "is this
build acceptable" and not "is this model an improvement on that one", which is
a comparison and needs two runs. This module is that comparison.

## The rule

    promote  iff  no critical scenario regressed
             and  no more scenarios regressed than improved overall

The first clause is absolute and has no threshold. There is deliberately no
"acceptable number of critical regressions" and no score that buys one: the
point of §42 is that an aggregate cannot outvote a safety failure, and a rule
with a tolerance is an aggregate wearing a different hat.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ScenarioDelta:
    scenario_id: str
    critical: bool
    baseline_passed: bool
    candidate_passed: bool

    @property
    def regressed(self) -> bool:
        return self.baseline_passed and not self.candidate_passed

    @property
    def improved(self) -> bool:
        return not self.baseline_passed and self.candidate_passed

    def as_dict(self) -> dict:
        return {"scenario_id": self.scenario_id, "critical": self.critical,
                "baseline": "pass" if self.baseline_passed else "FAIL",
                "candidate": "pass" if self.candidate_passed else "FAIL",
                "regressed": self.regressed, "improved": self.improved}


@dataclass
class Comparison:
    baseline_model: str
    candidate_model: str
    baseline_passed: int = 0
    candidate_passed: int = 0
    total: int = 0
    deltas: list[ScenarioDelta] = field(default_factory=list)
    only_in_baseline: list[str] = field(default_factory=list)
    only_in_candidate: list[str] = field(default_factory=list)

    @property
    def critical_regressions(self) -> list[ScenarioDelta]:
        return [d for d in self.deltas if d.regressed and d.critical]

    @property
    def regressions(self) -> list[ScenarioDelta]:
        return [d for d in self.deltas if d.regressed]

    @property
    def improvements(self) -> list[ScenarioDelta]:
        return [d for d in self.deltas if d.improved]

    @property
    def promote(self) -> bool:
        return not self.blockers

    @property
    def blockers(self) -> list[str]:
        """Why promotion is refused. Empty means promote."""
        out: list[str] = []
        if self.critical_regressions:
            ids = ", ".join(d.scenario_id for d in self.critical_regressions)
            out.append(
                f"{len(self.critical_regressions)} critical scenario(s) regressed: {ids}. "
                f"A candidate that is better on average and worse on a safety scenario is "
                f"not better.")
        if len(self.regressions) > len(self.improvements):
            out.append(
                f"{len(self.regressions)} scenario(s) regressed against "
                f"{len(self.improvements)} improved.")
        if self.only_in_baseline or self.only_in_candidate:
            # Two runs over different scenario sets are not a comparison. Saying
            # so beats quietly comparing the intersection and reporting a
            # verdict about a suite neither model was measured on.
            out.append(
                f"The runs cover different scenarios "
                f"({len(self.only_in_baseline)} only in baseline, "
                f"{len(self.only_in_candidate)} only in candidate); they are not "
                f"comparable.")
        return out

    def as_dict(self) -> dict:
        return {
            "baseline_model": self.baseline_model,
            "candidate_model": self.candidate_model,
            "baseline_passed": self.baseline_passed,
            "candidate_passed": self.candidate_passed,
            "total": self.total,
            "regressions": [d.as_dict() for d in self.regressions],
            "improvements": [d.as_dict() for d in self.improvements],
            "critical_regressions": [d.as_dict() for d in self.critical_regressions],
            "promote": self.promote,
            "blockers": self.blockers,
        }


def compare(baseline: dict, candidate: dict) -> Comparison:
    """Two evaluation reports in, one promotion decision out."""
    def index(report: dict) -> dict[str, dict]:
        return {r["scenario_id"]: r for r in report.get("results", [])}

    b, c = index(baseline), index(candidate)
    shared = sorted(set(b) & set(c))

    cmp = Comparison(
        baseline_model=f"{baseline.get('provider')}/{baseline.get('model')}",
        candidate_model=f"{candidate.get('provider')}/{candidate.get('model')}",
        baseline_passed=int(baseline.get("passed", 0)),
        candidate_passed=int(candidate.get("passed", 0)),
        total=len(shared),
        only_in_baseline=sorted(set(b) - set(c)),
        only_in_candidate=sorted(set(c) - set(b)),
    )
    for sid in shared:
        cmp.deltas.append(ScenarioDelta(
            scenario_id=sid,
            # Criticality is a property of the scenario, so either run can say
            # it. Taking the OR means a candidate cannot dodge the gate by
            # reporting a safety scenario as ordinary.
            critical=bool(b[sid]["metrics"].get("critical")
                          or c[sid]["metrics"].get("critical")),
            baseline_passed=bool(b[sid]["passed"]),
            candidate_passed=bool(c[sid]["passed"]),
        ))
    return cmp


def render(cmp: Comparison) -> str:
    """The table a human reads before deciding."""
    lines = [
        "=" * 78,
        "Model governance — MerchantOps §42",
        "=" * 78,
        "",
        f"  baseline   {cmp.baseline_model}   {cmp.baseline_passed}/{cmp.total} passed",
        f"  candidate  {cmp.candidate_model}   {cmp.candidate_passed}/{cmp.total} passed",
        "",
    ]
    if cmp.improvements:
        lines.append(f"  improved ({len(cmp.improvements)}):")
        lines += [f"    + {d.scenario_id}{'  [critical]' if d.critical else ''}"
                  for d in cmp.improvements]
        lines.append("")
    if cmp.regressions:
        lines.append(f"  regressed ({len(cmp.regressions)}):")
        lines += [f"    - {d.scenario_id}{'  [CRITICAL]' if d.critical else ''}"
                  for d in cmp.regressions]
        lines.append("")
    if not cmp.improvements and not cmp.regressions:
        lines += ["  No scenario changed outcome.", ""]

    lines.append("PROMOTE" if cmp.promote else "DO NOT PROMOTE")
    for b in cmp.blockers:
        lines.append(f"  - {b}")
    return "\n".join(lines)
