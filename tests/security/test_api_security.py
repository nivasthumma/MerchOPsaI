"""API authentication and rate limiting — closes two threat-model residual risks."""
from __future__ import annotations

from datetime import UTC

import pytest
from fastapi.testclient import TestClient

from app.api import security as sec
from app.api.main import app


@pytest.fixture
def client(db):
    sec.reset_rate_limits()
    with TestClient(app) as c:
        yield c
    sec.reset_rate_limits()
    # The runtime provider override lives in the process. Left set, it would
    # silently change what every later test measures.
    from app.config import set_runtime_llm_provider
    set_runtime_llm_provider(None)


def token(user_id: str) -> dict:
    return {"Authorization": f"Bearer {sec.issue_token(user_id)}"}


# ---------------------------------------------------------------- tokens
def test_signature_verifies():
    t = sec.issue_token("USR_A_OWNER")
    assert sec.verify_token(t) == "USR_A_OWNER"


def test_forged_signature_is_rejected():
    assert sec.verify_token("USR_A_OWNER." + "f" * 64) is None


def test_cannot_swap_the_subject_and_keep_the_signature():
    """The signature covers the user id, so lifting it onto another identity fails."""
    good = sec.issue_token("USR_A_OWNER")
    sig = good.split(".", 1)[1]
    assert sec.verify_token(f"USR_B_OWNER.{sig}") is None


def test_malformed_tokens_are_rejected():
    for bad in ("", "nodot", ".", "USR_A_OWNER.", ".sig", "Bearer x"):
        assert sec.verify_token(bad) is None


def test_secret_change_invalidates_existing_tokens(monkeypatch):
    t = sec.issue_token("USR_A_OWNER")
    monkeypatch.setenv("API_TOKEN_SECRET", "a-different-secret")
    assert sec.verify_token(t) is None


# ---------------------------------------------------------------- endpoints
def test_request_without_token_is_401(client):
    r = client.get("/tasks/ANY")
    assert r.status_code == 401
    assert r.headers.get("www-authenticate") == "Bearer"


def test_request_with_forged_token_is_401(client):
    r = client.get("/tasks/ANY", headers={"Authorization": "Bearer USR_A_OWNER." + "0" * 64})
    assert r.status_code == 401


def test_valid_token_is_accepted(client, db):
    # An authenticated route: 404 for the unknown task, but only after auth passed.
    assert client.get("/tasks/NOPE", headers=token("USR_A_OWNER")).status_code == 404


def test_token_for_deleted_principal_is_rejected(client, db):
    """A token can outlive its subject; the database is still the authority."""
    t = sec.issue_token("USR_DOES_NOT_EXIST")
    # /health is deliberately public, so this must target an authenticated route.
    r = client.get("/tasks/NOPE", headers={"Authorization": f"Bearer {t}"})
    assert r.status_code == 401


def test_permissions_come_from_the_database_not_the_token(client, db):
    """The token carries only an identity. Elevating requires changing the
    database, not minting a different token."""
    r = client.post("/tasks", json={"request": "Refund the duplicate payment."},
                    headers=token("USR_A_ANALYST"))
    assert r.status_code == 200
    body = r.json()
    assert body["approvals"] == []          # analyst never reaches an approval
    assert body["actions"] == []


def test_cross_merchant_task_is_not_visible(client, db):
    created = client.post("/tasks", json={"request": "Why did revenue drop this week?"},
                          headers=token("USR_A_OWNER"))
    task_id = created.json()["id"]
    assert client.get(f"/tasks/{task_id}", headers=token("USR_A_OWNER")).status_code == 200
    # Existence must not leak across merchants: 404, not 403.
    assert client.get(f"/tasks/{task_id}", headers=token("USR_B_OWNER")).status_code == 404


# ---------------------------------------------------------------- rate limiting
def test_action_class_is_rate_limited(client, db):
    hdr = token("USR_A_OWNER")
    codes = [client.post("/tasks/NOPE/approve", headers=hdr).status_code
             for _ in range(sec.LIMITS["action"].requests + 2)]
    assert 429 in codes
    assert codes.index(429) == sec.LIMITS["action"].requests


def test_rate_limit_response_tells_the_caller_when_to_retry(client, db):
    hdr = token("USR_A_OWNER")
    for _ in range(sec.LIMITS["action"].requests + 1):
        r = client.post("/tasks/NOPE/approve", headers=hdr)
    assert r.status_code == 429
    assert "Retry-After" in r.headers
    assert r.json()["detail"]["error"] == "rate_limit_exceeded"


def test_limit_classes_have_independent_budgets(client, db):
    hdr = token("USR_A_OWNER")
    for _ in range(sec.LIMITS["action"].requests + 1):
        client.post("/tasks/NOPE/approve", headers=hdr)
    # Reads must still work after the action budget is spent.
    assert client.get("/tasks/NOPE", headers=hdr).status_code == 404


def test_limits_are_per_principal(client, db):
    a = token("USR_A_OWNER")
    for _ in range(sec.LIMITS["action"].requests + 1):
        client.post("/tasks/NOPE/approve", headers=a)
    assert client.post("/tasks/NOPE/approve", headers=a).status_code == 429
    # One principal exhausting its budget must not deny another.
    assert client.post("/tasks/NOPE/approve",
                       headers=token("USR_B_OWNER")).status_code != 429


def test_unauthenticated_requests_cannot_consume_a_principal_budget(client, db):
    """Rate limiting runs AFTER authentication, so an anonymous flood cannot
    exhaust a real user's allowance."""
    for _ in range(sec.LIMITS["action"].requests + 5):
        client.post("/tasks/NOPE/approve")
    assert client.post("/tasks/NOPE/approve",
                       headers=token("USR_A_OWNER")).status_code != 429


# ------------------------------------------------------------- evidence route
def _halted_task(client) -> str:
    """A task stopped at the approval gate — the state §21 describes."""
    r = client.post("/tasks", json={"request": "Find the duplicate payment and refund it"},
                    headers=token("USR_A_OWNER"))
    assert r.status_code == 200
    assert r.json()["status"] == "AWAITING_APPROVAL"
    return r.json()["id"]


def test_evidence_is_reachable_for_the_task_owner(client, db):
    """CONTRACT §21 lists evidence among what the human reviews before
    approving. Streamlit reads tool_calls from the database directly; an HTTP
    client needs a route to the same facts or it can only show four of five."""
    tid = _halted_task(client)
    body = client.get(f"/tasks/{tid}/evidence", headers=token("USR_A_OWNER")).json()
    tools = [c["tool"] for c in body["tool_calls"]]
    assert "find_duplicate_payments" in tools
    assert any(c["evidence"] for c in body["tool_calls"])


def test_evidence_preserves_the_untrusted_tag(client, db):
    """CONTRACT §36. Merchant free text is an injection surface, and the client
    must be told which values to quarantine. Stripping the flag at the API would
    push that judgement onto every consumer."""
    tid = _halted_task(client)
    body = client.get(f"/tasks/{tid}/evidence", headers=token("USR_A_OWNER")).json()
    items = [e for c in body["tool_calls"] for e in c["evidence"]]
    untrusted = [e for e in items if e["untrusted"]]
    assert untrusted, "the seeded order carries injected notes; the tag must survive"
    assert any("SYSTEM OVERRIDE" in str(e["value"]) for e in untrusted)


def test_evidence_does_not_leak_across_merchants(client, db):
    tid = _halted_task(client)
    # 404 rather than 403: existence is not leaked.
    assert client.get(f"/tasks/{tid}/evidence",
                      headers=token("USR_B_OWNER")).status_code == 404
    assert client.get(f"/tasks/{tid}/evidence").status_code == 401


# --------------------------------------------------------- provider selection
def test_identity_comes_from_the_server(client, db):
    body = client.get("/me", headers=token("USR_A_ANALYST")).json()
    assert body["user_id"] == "USR_A_ANALYST"
    assert body["role"] == "analyst"
    # Permissions are the database's answer, not the token's claim.
    assert "action:refund" not in body["permissions"]


def test_provider_switch_requires_the_owner_role(client, db):
    r = client.post("/config/llm-provider", json={"provider": "deterministic"},
                    headers=token("USR_A_ANALYST"))
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "role_required"


def test_provider_switch_refuses_a_provider_that_is_not_configured(client, db):
    """The control selects among providers the process can already reach. It
    cannot conjure one, and it never accepts a credential (CONTRACT §37)."""
    r = client.post("/config/llm-provider", json={"provider": "anthropic"},
                    headers=token("USR_A_OWNER"))
    # No credential in the test environment, so this must be refused rather
    # than leaving the agent pointed at a provider it cannot reach.
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "no_credential"
    assert client.get("/health").json()["llm_provider"] == "deterministic"


def test_provider_switch_is_audited(client, db):
    from sqlalchemy import text
    r = client.post("/config/llm-provider", json={"provider": "deterministic"},
                    headers=token("USR_A_OWNER"))
    assert r.status_code == 200
    assert r.json()["llm_provider_source"] == "runtime"

    row = db.execute(text("""
        SELECT user_id, payload FROM audit_logs
        WHERE event_type = 'llm_provider_changed' ORDER BY id DESC LIMIT 1
    """)).mappings().first()
    assert row is not None, "a privileged change must leave a record"
    assert row["user_id"] == "USR_A_OWNER"
    assert row["payload"]["to"] == "deterministic"


def test_unknown_provider_is_rejected(client, db):
    r = client.post("/config/llm-provider", json={"provider": "gpt"},
                    headers=token("USR_A_OWNER"))
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "unknown_provider"


def test_the_tenant_on_the_principal_is_the_users_own(client):
    """Found by a surviving mutant: hardcoding a tenant in principal resolution
    changed nothing any test could see, because every API test authenticated as
    a user of the same tenant.

    The merchant check would still have refused the cross-merchant read, so this
    was defence in depth being quietly disabled rather than a live hole — which
    is exactly the kind of thing a suite stops noticing.
    """
    a = client.get("/me", headers=token("USR_A_OWNER")).json()
    b = client.get("/me", headers=token("USR_B_OWNER")).json()
    assert a["tenant_id"] == "TEN_KETTLE"
    assert b["tenant_id"] == "TEN_NORTHWIND"
    assert a["tenant_id"] != b["tenant_id"]


# ------------------------------------------------------ §65 resource routes
def test_the_approval_queue_is_a_resource_and_is_merchant_scoped(client, db, owner):
    """Reachable before only through the task that owned it, so "what is
    waiting on me" had to be assembled client-side."""
    from app.agent.runtime import AgentRuntime

    out = AgentRuntime(db, owner).run("Find the duplicate payment and refund it.")
    assert out.approval is not None
    db.commit()

    a = client.get("/approvals", headers=token("USR_A_OWNER")).json()["approvals"]
    assert [x["id"] for x in a] == [out.approval.id]
    assert a[0]["required_signatures"] >= 1
    assert a[0]["expired"] is False

    b = client.get("/approvals", headers=token("USR_B_OWNER")).json()["approvals"]
    assert b == []


def test_an_expired_approval_is_not_shown_as_actionable(client, db, owner):
    """It stays PENDING in the database until someone tries to use it. The
    queue must not present it as work an operator can still do."""
    from datetime import datetime, timedelta

    from app.agent.runtime import AgentRuntime

    out = AgentRuntime(db, owner).run("Find the duplicate payment and refund it.")
    out.approval.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db.commit()

    a = client.get("/approvals", headers=token("USR_A_OWNER")).json()["approvals"]
    assert a[0]["expired"] is True


def test_an_action_is_addressable_and_merchant_scoped(client, db, owner):
    from app.agent.approval import approve_and_execute
    from app.agent.runtime import AgentRuntime

    out = AgentRuntime(db, owner).run("Find the duplicate payment and refund it.")
    r = approve_and_execute(db, out.task.id, owner)
    action_id = r["action"].id
    db.commit()

    body = client.get(f"/actions/{action_id}", headers=token("USR_A_OWNER")).json()
    assert body["verification_state"] == "SUCCESS"
    assert body["approval_id"]
    assert body["provider_latency_ms"] is not None
    # The key proves nothing useful in full and should not land in a screenshot.
    assert body["idempotency_key_prefix"].endswith("...")
    assert len(body["idempotency_key_prefix"]) < 25

    assert client.get(f"/actions/{action_id}",
                      headers=token("USR_B_OWNER")).status_code == 404


def test_temperature_is_never_sent_to_the_model():
    """MerchantOps §16 asks for temperature 0. Sampling parameters were removed
    on this model family — sending temperature to claude-opus-5 returns a 400 —
    so the instruction is not implementable and `effort` is what replaced it.
    Asserted rather than left as a comment somebody might undo."""
    import inspect

    from app.llm import anthropic_provider

    src = inspect.getsource(anthropic_provider.AnthropicProvider.turn)
    assert "temperature" not in src.split('"""')[0] or "temperature=" not in src
    assert "temperature=" not in src
    assert '"effort"' in src


# --------------------------------------------------------------------------
# The development signing secret must not reach a deployment
# --------------------------------------------------------------------------
class TestDevelopmentSecretIsRefusedOnDeployments:
    """`require_configured_secret` — the control the docstring already claimed.

    Until this existed, `app/api/security.py` described an API that "refuses to
    start in strict mode without a real one (see require_configured_secret)"
    and no such function was defined anywhere in the repository. `/health`
    reported the fallback and nothing consulted the report before serving.

    The fallback matters because the value is a literal in this repository:
    anyone who can read it can mint a token for any user, and permissions are
    then read from the database exactly as for a real one. The checks behind the
    token hold; the identity in front of them does not.
    """

    def _reload(self, monkeypatch, **env):
        """Re-read the module under a given environment.

        DEV_SECRET_IN_USE is computed at import, so the environment has to be in
        place before the module is read rather than after.
        """
        import importlib

        import app.api.security as sec
        for key in ("API_TOKEN_SECRET", "MERCHANTOPS_ALLOW_DEV_SECRET",
                    "MERCHANTOPS_ENV", "VERCEL", "AWS_EXECUTION_ENV",
                    "KUBERNETES_SERVICE_HOST", "DYNO", "RENDER",
                    "FLY_APP_NAME", "WEBSITE_INSTANCE_ID"):
            monkeypatch.delenv(key, raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        return importlib.reload(sec)

    def test_a_laptop_with_no_secret_still_runs(self, monkeypatch):
        """The fallback exists so a fresh clone works. That must keep working."""
        sec = self._reload(monkeypatch)
        sec.require_configured_secret()          # must not raise

    def test_a_deployment_with_no_secret_refuses_to_start(self, monkeypatch):
        sec = self._reload(monkeypatch, VERCEL="1")
        with pytest.raises(sec.InsecureConfiguration) as exc:
            sec.require_configured_secret()
        assert "API_TOKEN_SECRET" in str(exc.value)
        assert "VERCEL" in str(exc.value), "the message must name what gave it away"

    @pytest.mark.parametrize("marker", [
        "VERCEL", "AWS_EXECUTION_ENV", "KUBERNETES_SERVICE_HOST",
        "DYNO", "RENDER", "FLY_APP_NAME", "WEBSITE_INSTANCE_ID",
    ])
    def test_every_platform_marker_is_honoured(self, monkeypatch, marker):
        """One platform's variable being handled is not a control."""
        sec = self._reload(monkeypatch, **{marker: "1"})
        with pytest.raises(sec.InsecureConfiguration):
            sec.require_configured_secret()

    @pytest.mark.parametrize("value", ["production", "staging", "prod", "PRODUCTION"])
    def test_an_explicit_environment_is_enough(self, monkeypatch, value):
        sec = self._reload(monkeypatch, MERCHANTOPS_ENV=value)
        with pytest.raises(sec.InsecureConfiguration):
            sec.require_configured_secret()

    def test_a_real_secret_satisfies_it_everywhere(self, monkeypatch):
        sec = self._reload(monkeypatch, VERCEL="1", API_TOKEN_SECRET="a-real-secret")
        sec.require_configured_secret()          # must not raise

    def test_the_override_is_available_and_explicit(self, monkeypatch):
        """A deliberate exception stays possible; it just has to be deliberate."""
        sec = self._reload(monkeypatch, VERCEL="1", MERCHANTOPS_ALLOW_DEV_SECRET="1")
        sec.require_configured_secret()          # must not raise

    def test_tokens_signed_with_the_default_are_forgeable_from_this_repository(
            self, monkeypatch):
        """Why the control exists, stated as a test rather than a comment.

        The default is a literal in the source. Anyone holding it can produce a
        token the server accepts for any user id.
        """
        self._reload(monkeypatch)

        # Minted with the published default, in the CURRENT format (ADR-0049).
        # Forging the retired format would demonstrate nothing about the one in
        # use -- and the retired one is refused outright now, which is not the
        # same thing as the default being safe.
        import importlib

        import app.auth as auth_module
        auth = importlib.reload(auth_module)

        forged = auth.mint("USR_A_OWNER")
        assert auth.parse(forged).sub == "USR_A_OWNER", (
            "the development default is a literal in this repository; anyone "
            "holding it can mint a token the server accepts for any user")
