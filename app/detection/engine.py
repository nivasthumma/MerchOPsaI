"""Detection sweep — MerchantOps §12, §13.

Turns anomalies into incidents, once each.

    payments -> rules -> Anomaly[] -> Incident (idempotent) -> audit

## Idempotency

The sweep is expected to run repeatedly -- from cron, from the API, from a test.
Running it twice over the same window must not produce two incidents for one
anomaly. `Incident.detection_key` is UNIQUE and every rule derives its key from
the facts of the anomaly rather than from the clock, so the second insert
collides and is skipped. This is the same mechanism `agent_actions` uses to make
duplicate execution impossible, applied to duplicate *observation*.

The insert is attempted rather than pre-checked. A SELECT-then-INSERT is a race:
two concurrent sweeps both read "absent" and both write. The unique constraint is
the authority, so the collision is the check.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.audit.trace import record_incident
from app.detection.rules import RULES, DETECTION_VERSION, Anomaly
from app.models import Incident, IncidentEvidence, IncidentStatus


@dataclass
class DetectionReport:
    merchant_id: str
    scanned_rules: int = 0
    anomalies_found: int = 0
    incidents_created: int = 0
    already_known: int = 0
    duration_ms: int = 0
    incidents: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "merchant_id": self.merchant_id,
            "scanned_rules": self.scanned_rules,
            "anomalies_found": self.anomalies_found,
            "incidents_created": self.incidents_created,
            "already_known": self.already_known,
            "duration_ms": self.duration_ms,
            "incidents": self.incidents,
        }


def _persist(session, merchant_id: str, a: Anomaly) -> Incident | None:
    """Insert the incident, or return None if this anomaly is already known."""
    correlation_id = f"COR_{uuid.uuid4().hex[:12].upper()}"
    inc = Incident(
        id=f"INC_{uuid.uuid4().hex[:10].upper()}",
        merchant_id=merchant_id,
        incident_type=a.incident_type,
        severity=a.severity,
        status=IncidentStatus.DETECTED,
        title=a.title,
        summary=a.summary,
        detection_key=a.detection_key,
        revenue_at_risk_minor=a.revenue_at_risk_minor,
        signals=a.signals,
        detection_rule=a.detection_rule,
        detection_version=DETECTION_VERSION,
        correlation_id=correlation_id,
        started_at=a.started_at,
    )

    # SAVEPOINT, not a bare flush: a collision must discard this one INSERT, not
    # the incidents and audit rows already written by this sweep.
    sp = session.begin_nested()
    try:
        session.add(inc)
        session.flush()
        sp.commit()
    except IntegrityError:
        sp.rollback()
        return None

    for ev in a.evidence:
        session.add(IncidentEvidence(
            id=f"IEV_{uuid.uuid4().hex[:10].upper()}",
            incident_id=inc.id, key=ev["key"], value={"v": ev["value"]},
            source=ev["source"], untrusted=bool(ev.get("untrusted", False)),
        ))
    session.flush()

    record_incident(session, inc, "incident_detected", {
        "incident_type": inc.incident_type.value,
        "severity": inc.severity.value,
        "rule": inc.detection_rule,
        "detection_version": inc.detection_version,
        "revenue_at_risk_minor": inc.revenue_at_risk_minor,
        "correlation_id": correlation_id,
        "signals": inc.signals,
    })
    return inc


def detect(session, merchant_id: str, *, as_of: datetime | None = None) -> DetectionReport:
    """Run every rule for one merchant and record what is new.

    Scoped to one merchant by argument, never by discovery: there is no
    cross-merchant sweep on this path, for the same reason there is no
    cross-merchant read anywhere else (MerchantOps §54).
    """
    import time
    t0 = time.monotonic()
    report = DetectionReport(merchant_id=merchant_id)

    for rule in RULES:
        report.scanned_rules += 1
        for anomaly in rule(session, merchant_id, as_of=as_of):
            report.anomalies_found += 1
            inc = _persist(session, merchant_id, anomaly)
            if inc is None:
                report.already_known += 1
                continue
            report.incidents_created += 1
            report.incidents.append({
                "id": inc.id, "type": inc.incident_type.value,
                "severity": inc.severity.value, "title": inc.title,
                "revenue_at_risk_minor": inc.revenue_at_risk_minor,
                "started_at": inc.started_at.isoformat(),
            })

    report.duration_ms = int((time.monotonic() - t0) * 1000)
    return report


def open_incidents(session, merchant_id: str) -> list[Incident]:
    """Incidents that still need someone. CLOSED and RESOLVED are excluded --
    an operations console that lists resolved work alongside live work is a
    console nobody reads."""
    return (session.query(Incident)
            .filter(Incident.merchant_id == merchant_id,
                    Incident.status.notin_([IncidentStatus.RESOLVED, IncidentStatus.CLOSED]))
            .order_by(Incident.revenue_at_risk_minor.desc(), Incident.detected_at.desc())
            .all())
