"""Streamlit UI — CONTRACT §40. The trace is the primary demo surface.

Deliberately plain. The reviewer should spend the demo watching the agent loop
and the policy gate, not navigating a dashboard.

Note on the Streamlit execution model: the agent runs synchronously inside a
single rerun and everything it produces is read back from PostgreSQL. Nothing
is held in session state except ids, so a rerun never loses or duplicates work.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import streamlit as st
from sqlalchemy import text

from app.agent.approval import ApprovalError, approve_and_execute, reject, reverify
from app.agent.replay import playback, re_reason
from app.agent.runtime import AgentRuntime
from app.audit.trace import trace_for
from app.config import get_settings
from app.db import session_scope
from app.eval.runner import PRINCIPALS, load_scenarios
from app.models import AgentAction, AgentTask, Approval

st.set_page_config(page_title="MerchantOps Agent", layout="wide")
S = get_settings()

STATE_COLOUR = {"SUCCESS": "🟢", "FAILED": "🔴", "PARTIAL": "🟠", "UNKNOWN": "⚪"}

# ---------------------------------------------------------------- sidebar
with st.sidebar:
    st.title("MerchantOps Agent")
    st.caption("Independent developer project. Not affiliated with, sponsored "
               "by, or endorsed by Razorpay.")
    st.divider()

    who = st.selectbox("Acting as", list(PRINCIPALS),
                       format_func=lambda k: f"{k} ({', '.join(PRINCIPALS[k].permissions)})")
    principal = PRINCIPALS[who]

    st.divider()
    st.subheader("Run configuration")
    real = S.resolved_razorpay_mode == "live_test_mode"
    st.write(f"**LLM provider:** `{S.resolved_llm_provider}`")
    st.write(f"**Payment adapter:** `{S.resolved_razorpay_mode}`")
    if real:
        st.success("Executing against real Razorpay Test Mode.")
    else:
        st.warning("Refunds execute against a **mock adapter**, not Razorpay. "
                   "Policy, approval and verification are unchanged.")
    if S.resolved_llm_provider == "deterministic":
        st.info("Reasoning uses the deterministic planner, not a language model. "
                "Results measure the control plane, not model intelligence.")

st.header("Investigate")

col_a, col_b = st.columns([3, 1])
with col_a:
    request = st.text_input("Ask about revenue, payments, orders or duplicates",
                            value="Why did revenue drop this week?")
with col_b:
    st.write("")
    st.write("")
    go = st.button("Run", type="primary", use_container_width=True)

EXAMPLES = [
    "Why did revenue drop this week?",
    "Which payment method is failing most?",
    "Are there any duplicate payments on this account?",
    "Find the duplicate payment and refund it.",
]
cols = st.columns(len(EXAMPLES))
for c, ex in zip(cols, EXAMPLES):
    if c.button(ex, use_container_width=True):
        request = ex
        go = True

if go and request.strip():
    with st.spinner("Agent running..."):
        with session_scope() as s:
            out = AgentRuntime(s, principal).run(request)
            st.session_state["task_id"] = out.task.id
            st.session_state.pop("replay", None)

task_id = st.session_state.get("task_id")

if task_id:
    with session_scope() as s:
        task = s.get(AgentTask, task_id)
        approvals = s.query(Approval).filter(Approval.task_id == task_id).all()
        actions = s.query(AgentAction).filter(AgentAction.task_id == task_id).all()
        calls = s.execute(text("""
            SELECT seq, tool_name, input, success, risk_level, policy_decision,
                   duration_ms, error_code, output
            FROM tool_calls WHERE task_id = :t ORDER BY seq
        """), {"t": task_id}).mappings().all()
        events = trace_for(s, task_id)

    st.divider()
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Status", task.status.value)
    m2.metric("Tool calls", task.tool_call_count)
    m3.metric("Duration", f"{task.duration_ms} ms")
    verif = actions[-1].verification_state.value if actions and actions[-1].verification_state else "—"
    m4.metric("Verification", f"{STATE_COLOUR.get(verif, '')} {verif}")

    if task.final_answer:
        st.subheader("Result")
        st.write(task.final_answer)
    if task.failure_code:
        st.error(f"Failure code: `{task.failure_code}`")

    tabs = st.tabs(["Agent activity", "Evidence", "Approval", "Audit trace",
                    "Replay", "Scenarios"])

    # ---------------------------------------------------------- activity
    with tabs[0]:
        for c in calls:
            icon = "✅" if c["success"] else ("⛔" if c["policy_decision"] == "DENY" else "⏸️")
            head = (f"{icon}  {c['seq']}. `{c['tool_name']}`  ·  risk {c['risk_level']}  ·  "
                    f"policy {c['policy_decision'] or '—'}  ·  {c['duration_ms']} ms")
            with st.expander(head, expanded=not c["success"]):
                st.caption("Arguments")
                st.json(c["input"])
                if c["error_code"]:
                    st.error(f"`{c['error_code']}`")
                out = c["output"] or {}
                if out.get("policy"):
                    st.caption("Policy decision")
                    st.json(out["policy"])
                elif out.get("data"):
                    st.caption("Result")
                    st.json(out["data"])

    # ---------------------------------------------------------- evidence
    with tabs[1]:
        st.caption("Typed findings. OBSERVED claims cite the tool call that produced "
                   "them, which is what makes grounding measurable (CONTRACT §14).")
        observed = [f for f in (task.findings or []) if f["kind"] == "OBSERVED"]
        inferred = [f for f in (task.findings or []) if f["kind"] != "OBSERVED"]
        st.write(f"**{len(observed)} observed · {len(inferred)} inferred**")
        for f in observed:
            st.write(f"- `{f['metric']}` = **{f['value']}**  ·  cites `{','.join(f['evidence_refs'])}`")
        for f in inferred:
            st.info(f"**{f['kind']}** — {f['claim']}")

        untrusted = []
        for c in calls:
            for ev in (c["output"] or {}).get("evidence", []):
                if ev.get("untrusted"):
                    untrusted.append(ev)
        if untrusted:
            st.divider()
            st.subheader("Untrusted merchant data encountered")
            st.caption("Delivered to the model inside delimiters and never treated "
                       "as instructions (CONTRACT §36).")
            for ev in untrusted:
                st.code(f"[{ev['source']}] {ev['value']}", language=None)

    # ---------------------------------------------------------- approval
    with tabs[2]:
        if not approvals:
            st.info("This task required no high-risk action.")
        for ap in approvals:
            st.write(f"**{ap.id}** · `{ap.action_type}` · risk **{ap.risk_level}** · "
                     f"decision **{ap.decision}**")
            st.json(ap.action_payload)
            st.caption(f"Expires {ap.expires_at.isoformat()} "
                       f"(approvals are re-checked server-side at execution time)")
            if ap.decision == "PENDING":
                c1, c2 = st.columns(2)
                if c1.button("Approve and execute", type="primary", key=f"ap{ap.id}"):
                    with session_scope() as s:
                        try:
                            approve_and_execute(s, task_id, principal)
                        except ApprovalError as e:
                            st.error(f"{e} (`{e.code}`)")
                    st.rerun()
                if c2.button("Reject", key=f"rj{ap.id}"):
                    with session_scope() as s:
                        reject(s, task_id, principal)
                    st.rerun()

        for a in actions:
            st.divider()
            st.write(f"**Action {a.id}** · {a.status.value}")
            st.write(f"- target `{a.target_payment_id}` → external `{a.external_payment_id}`")
            st.write(f"- external reference: `{a.external_reference}`")
            st.write(f"- idempotency key: `{a.idempotency_key[:24]}…` (derived server-side)")
            vs = a.verification_state.value if a.verification_state else "—"
            st.write(f"- verification: {STATE_COLOUR.get(vs, '')} **{vs}** "
                     f"(attempt {a.verify_attempts})")
            if a.verification_detail:
                st.caption(a.verification_detail.get("reason", ""))
            if vs == "UNKNOWN":
                st.warning("UNKNOWN is a pending safety state, not a verdict. "
                           "Re-verify to reconcile it against the provider.")
                if st.button("Re-verify", key=f"rv{a.id}"):
                    with session_scope() as s:
                        reverify(s, task_id, principal)
                    st.rerun()

    # ---------------------------------------------------------- audit
    with tabs[3]:
        st.caption("Append-only audit trail (CONTRACT §27). Secrets redacted.")
        for e in events:
            st.write(f"`{e['at'][11:19]}`  **{e['event']}**")
            if e["payload"]:
                with st.expander("payload", expanded=False):
                    st.json(e["payload"])

    # ---------------------------------------------------------- replay
    with tabs[4]:
        st.caption("PLAYBACK renders the recorded trace. RE_REASON re-runs the agent "
                   "against frozen tool results. Neither performs a financial action.")
        c1, c2 = st.columns(2)
        if c1.button("Playback", use_container_width=True):
            with session_scope() as s:
                st.session_state["replay"] = playback(s, task_id)
        if c2.button("Re-reason (frozen tools)", use_container_width=True):
            with session_scope() as s:
                st.session_state["replay"] = re_reason(s, task_id, principal)
        rp = st.session_state.get("replay")
        if rp:
            st.success(f"Mode **{rp['mode']}** · external calls made: "
                       f"**{rp['external_calls_made']}**")
            if rp["mode"] == "RE_REASON":
                st.write(f"- reasoning diverged: **{rp['reasoning_diverged']}**")
                st.write(f"- policy diverged: **{rp['policy_diverged']}** "
                         f"({rp['policy_divergence_cause'] or 'n/a'})")
                st.write(f"- original: `{rp['original_tool_sequence']}`")
                st.write(f"- replay:   `{rp['replay_tool_sequence']}`")
                if rp["diff"]:
                    st.json(rp["diff"])
            else:
                st.json(rp["steps"])

    # ---------------------------------------------------------- scenarios
    with tabs[5]:
        st.caption("Run `scripts/run_scenarios.py` for full measured results. "
                   "This tab lists the defined scenarios.")
        for sc in load_scenarios():
            flag = "⭐" if sc.critical else "　"
            st.write(f"{flag} `{sc.id}` · {sc.category} — {sc.description.strip()}")
else:
    st.info("Run an investigation to see the agent loop, policy gate, verification "
            "and audit trace.")
