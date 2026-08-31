"""The agent's structured output — MerchantOps §36, §37, §38.

§37 asks the model to return a typed object and the backend to validate it:

    {intent, findings[], recommendation, confidence, requires_human}

## What the model is allowed to decide with this

Nothing that gates anything.

`confidence` and `requires_human` are model output, and model output travels
through prompts that carry merchant free text. If a low `requires_human` could
relax a control, an injected instruction would only have to sound confident. So
the rule is the same one the risk engine uses (ADR-0019), for the same reason:

    requires_human = policy_requires_human OR model_requires_human

The model may RAISE the bar. It may never lower it. `confidence` is recorded and
displayed and consulted by nothing.

## Grounding

§36: every AI conclusion carries evidence references. A finding that asserts
something about the world -- a root cause, an inference -- must cite at least one
evidence id that actually exists in this task's tool results. One that cites
nothing, or cites `E99` when seven pieces of evidence were gathered, is rejected
as AGENT_GROUNDING_FAILURE (§56) rather than displayed.

Recommendations and uncertainties are exempt: a recommendation follows from
findings rather than from evidence directly, and "I could not establish X" is
precisely a claim with no evidence behind it.

## Why the deterministic findings stay

`AgentRuntime._derive_findings` builds OBSERVED findings from what the tools
actually returned. Those are not replaced by model output and never will be:
they are what makes the grounding rate computable without asking a second model
to judge the first, which is the project's strongest measurement. Model findings
are added alongside as INFERRED and RECOMMENDED, which is what §20's
FACT / INFERENCE / RECOMMENDATION split means when it reaches the database.
"""
from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

# Claim types that assert something about the world and therefore need evidence.
GROUNDED_TYPES = frozenset({"root_cause", "inference", "observation"})

_FENCED = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)


class ModelFinding(BaseModel):
    type: Literal["observation", "root_cause", "inference", "recommendation", "uncertainty"]
    claim: str = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)

    @field_validator("evidence_ids")
    @classmethod
    def _clean(cls, v: list[str]) -> list[str]:
        return [x.strip() for x in v if isinstance(x, str) and x.strip()]


class ModelRecommendation(BaseModel):
    type: str = Field(min_length=1)
    detail: str | None = None


class AgentOutput(BaseModel):
    """§37's object. Extra keys are rejected: a model inventing a field is a
    model whose output we have stopped understanding."""
    model_config = {"extra": "forbid"}

    intent: str = Field(min_length=1)
    findings: list[ModelFinding] = Field(default_factory=list)
    recommendation: ModelRecommendation | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    requires_human: bool = False


class OutputProblem(BaseModel):
    code: str            # MODEL_INVALID_OUTPUT | AGENT_GROUNDING_FAILURE
    detail: str
    offending: list[str] = Field(default_factory=list)


def split_output(text: str) -> tuple[str, str | None]:
    """Separate the prose from the JSON block.

    The prose is what a person reads and what the evaluation suite grades; the
    block is machine output. Returning them joined would put JSON amounts into
    prose assertions -- a scenario checking that an answer does not contain
    "50000" would start failing on a recommendation's own figures.
    """
    if not text:
        return "", None
    m = _FENCED.search(text)
    if m:
        return (text[:m.start()] + text[m.end():]).strip(), m.group(1)
    # Unfenced: take a trailing object if the text ends with one.
    stripped = text.rstrip()
    if stripped.endswith("}"):
        depth = 0
        for i in range(len(stripped) - 1, -1, -1):
            if stripped[i] == "}":
                depth += 1
            elif stripped[i] == "{":
                depth -= 1
                if depth == 0:
                    return stripped[:i].strip(), stripped[i:]
    return text.strip(), None


def parse(text: str) -> tuple[str, AgentOutput | None, OutputProblem | None]:
    """(prose, output, problem). A model that returns no block is not an error
    here -- §37 is a contract the runtime enforces, and the runtime decides what
    a missing block means for the task."""
    prose, block = split_output(text)
    if block is None:
        return prose, None, None
    try:
        raw = json.loads(block)
    except ValueError as exc:
        return prose, None, OutputProblem(
            code="MODEL_INVALID_OUTPUT", detail=f"Output block is not valid JSON: {exc}")
    try:
        return prose, AgentOutput.model_validate(raw), None
    except ValidationError as exc:
        return prose, None, OutputProblem(
            code="MODEL_INVALID_OUTPUT",
            detail="Output did not match the agent output schema.",
            offending=[".".join(str(x) for x in e["loc"]) or "<root>"
                       for e in exc.errors()][:8])


def check_grounding(output: AgentOutput, known_evidence_ids: set[str]) -> OutputProblem | None:
    """§36. A claim about the world must cite evidence that exists."""
    ungrounded: list[str] = []
    for f in output.findings:
        if f.type not in GROUNDED_TYPES:
            continue
        if not any(e in known_evidence_ids for e in f.evidence_ids):
            ungrounded.append(f.claim[:80])
    if ungrounded:
        return OutputProblem(
            code="AGENT_GROUNDING_FAILURE",
            detail=(f"{len(ungrounded)} finding(s) assert something about the world "
                    f"while citing no evidence gathered in this task."),
            offending=ungrounded[:8])
    return None


def to_findings(output: AgentOutput, evidence_index: dict[str, str]) -> list[dict]:
    """Model findings in the stored `Finding` shape.

    `evidence_refs` are resolved to tool_call ids so a stored finding points at
    the same thing an OBSERVED one does, and a reader does not have to know two
    citation schemes.
    """
    kind = {"observation": "OBSERVED", "root_cause": "INFERRED",
            "inference": "INFERRED", "recommendation": "RECOMMENDED",
            "uncertainty": "INFERRED"}
    out = []
    for f in output.findings:
        out.append({
            "claim": f.claim,
            "kind": kind.get(f.type, "INFERRED"),
            "evidence_refs": [evidence_index[e] for e in f.evidence_ids
                              if e in evidence_index],
            "evidence_ids": f.evidence_ids,
            "metric": None, "value": None,
            "source": "model",
            "finding_type": f.type,
        })
    return out
