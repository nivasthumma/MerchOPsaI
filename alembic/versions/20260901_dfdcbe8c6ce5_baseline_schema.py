"""Baseline — the schema as it stood when migrations were introduced.

Revision ID: dfdcbe8c6ce5
Revises:
Created: 2026-09-01

Generated from `app/models.py`, so it reproduces exactly what
`Base.metadata.create_all()` was producing up to this point. It adds nothing and
changes nothing; its whole job is to give every database that already exists a
revision to be at.

**An existing database must be stamped, never upgraded.** Its tables are already
there, so running this migration against it fails on the first CREATE TABLE:

    alembic stamp dfdcbe8c6ce5     # an existing database: record where it is
    alembic upgrade head           # then apply everything after this point

`scripts/migrate.py` does that detection for you, and is what the Makefile and
the deployment guide call.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = 'dfdcbe8c6ce5'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ### auto generated from app/models.py; reviewed, not adjusted ###
    op.create_table('audit_logs',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('task_id', sa.String(length=64), nullable=True),
    sa.Column('incident_id', sa.String(length=64), nullable=True),
    sa.Column('merchant_id', sa.String(length=64), nullable=True),
    sa.Column('user_id', sa.String(length=64), nullable=True),
    sa.Column('event_type', sa.String(length=64), nullable=False),
    sa.Column('correlation_id', sa.String(length=64), nullable=True),
    sa.Column('payload', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_audit_logs_correlation_id'), 'audit_logs', ['correlation_id'], unique=False)
    op.create_index(op.f('ix_audit_logs_created_at'), 'audit_logs', ['created_at'], unique=False)
    op.create_index(op.f('ix_audit_logs_event_type'), 'audit_logs', ['event_type'], unique=False)
    op.create_index(op.f('ix_audit_logs_incident_id'), 'audit_logs', ['incident_id'], unique=False)
    op.create_index(op.f('ix_audit_logs_merchant_id'), 'audit_logs', ['merchant_id'], unique=False)
    op.create_index(op.f('ix_audit_logs_task_id'), 'audit_logs', ['task_id'], unique=False)
    op.create_table('evaluation_results',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('run_id', sa.String(length=64), nullable=False),
    sa.Column('scenario_id', sa.String(length=64), nullable=False),
    sa.Column('task_id', sa.String(length=64), nullable=True),
    sa.Column('passed', sa.Boolean(), nullable=False),
    sa.Column('checks', sa.JSON(), nullable=False),
    sa.Column('metrics', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_evaluation_results_run_id'), 'evaluation_results', ['run_id'], unique=False)
    op.create_index(op.f('ix_evaluation_results_scenario_id'), 'evaluation_results', ['scenario_id'], unique=False)
    op.create_table('tenants',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('webhook_events',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('event_id', sa.String(length=128), nullable=False),
    sa.Column('provider', sa.String(length=32), nullable=False),
    sa.Column('event_type', sa.String(length=64), nullable=False),
    sa.Column('schema_version', sa.String(length=16), nullable=False),
    sa.Column('tenant_id', sa.String(length=64), nullable=True),
    sa.Column('merchant_id', sa.String(length=64), nullable=True),
    sa.Column('entity_id', sa.String(length=64), nullable=True),
    sa.Column('status', sa.Enum('RECEIVED', 'PROCESSED', 'IGNORED', 'DUPLICATE', 'INVALID', name='webhookstatus', native_enum=False), nullable=False),
    sa.Column('signature_valid', sa.Boolean(), nullable=False),
    sa.Column('payload', sa.JSON(), nullable=False),
    sa.Column('payload_hash', sa.String(length=64), nullable=False),
    sa.Column('correlation_id', sa.String(length=64), nullable=False),
    sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('received_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('processed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('processing_note', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('event_id')
    )
    op.create_index('ix_webhook_entity_type', 'webhook_events', ['entity_id', 'event_type'], unique=False)
    op.create_index(op.f('ix_webhook_events_correlation_id'), 'webhook_events', ['correlation_id'], unique=False)
    op.create_index(op.f('ix_webhook_events_entity_id'), 'webhook_events', ['entity_id'], unique=False)
    op.create_index(op.f('ix_webhook_events_event_type'), 'webhook_events', ['event_type'], unique=False)
    op.create_index(op.f('ix_webhook_events_merchant_id'), 'webhook_events', ['merchant_id'], unique=False)
    op.create_index(op.f('ix_webhook_events_provider'), 'webhook_events', ['provider'], unique=False)
    op.create_index(op.f('ix_webhook_events_received_at'), 'webhook_events', ['received_at'], unique=False)
    op.create_index(op.f('ix_webhook_events_status'), 'webhook_events', ['status'], unique=False)
    op.create_index(op.f('ix_webhook_events_tenant_id'), 'webhook_events', ['tenant_id'], unique=False)
    op.create_table('merchants',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('tenant_id', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('currency', sa.String(length=8), nullable=False),
    sa.Column('policy_config', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_merchants_tenant_id'), 'merchants', ['tenant_id'], unique=False)
    op.create_table('customers',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('merchant_id', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('email', sa.String(length=200), nullable=False),
    sa.Column('segment', sa.String(length=64), nullable=False),
    sa.Column('contact_opted_out', sa.Boolean(), nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['merchant_id'], ['merchants.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_customers_merchant_id'), 'customers', ['merchant_id'], unique=False)
    op.create_table('incidents',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('merchant_id', sa.String(length=64), nullable=False),
    sa.Column('incident_type', sa.Enum('PAYMENT_DEGRADATION', 'DUPLICATE_PAYMENT', 'RECONCILIATION_MISMATCH', 'PROVIDER_FAILURE_BURST', name='incidenttype', native_enum=False), nullable=False),
    sa.Column('severity', sa.Enum('LOW', 'MEDIUM', 'HIGH', 'CRITICAL', name='incidentseverity', native_enum=False), nullable=False),
    sa.Column('status', sa.Enum('DETECTED', 'TRIAGED', 'INVESTIGATING', 'ROOT_CAUSE_IDENTIFIED', 'RECOVERY_PLANNED', 'POLICY_EVALUATING', 'APPROVAL_REQUIRED', 'EXECUTING', 'VERIFYING', 'RESOLVED', 'FAILED', 'UNKNOWN', 'ESCALATED', 'CANCELLED', 'CLOSED', name='incidentstatus', native_enum=False), nullable=False),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('summary', sa.Text(), nullable=False),
    sa.Column('detection_key', sa.String(length=200), nullable=False),
    sa.Column('revenue_at_risk_minor', sa.Integer(), nullable=False),
    sa.Column('signals', sa.JSON(), nullable=False),
    sa.Column('detection_rule', sa.String(length=64), nullable=False),
    sa.Column('detection_version', sa.String(length=32), nullable=False),
    sa.Column('correlation_id', sa.String(length=64), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('detected_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['merchant_id'], ['merchants.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('detection_key')
    )
    op.create_index(op.f('ix_incidents_correlation_id'), 'incidents', ['correlation_id'], unique=False)
    op.create_index(op.f('ix_incidents_merchant_id'), 'incidents', ['merchant_id'], unique=False)
    op.create_index('ix_incidents_merchant_status', 'incidents', ['merchant_id', 'status'], unique=False)
    op.create_index(op.f('ix_incidents_status'), 'incidents', ['status'], unique=False)
    op.create_table('notifications',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('merchant_id', sa.String(length=64), nullable=False),
    sa.Column('customer_id', sa.String(length=64), nullable=False),
    sa.Column('channel', sa.String(length=16), nullable=False),
    sa.Column('template', sa.String(length=64), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['merchant_id'], ['merchants.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_notifications_customer_id'), 'notifications', ['customer_id'], unique=False)
    op.create_index(op.f('ix_notifications_merchant_id'), 'notifications', ['merchant_id'], unique=False)
    op.create_table('payment_links',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('merchant_id', sa.String(length=64), nullable=False),
    sa.Column('customer_id', sa.String(length=64), nullable=False),
    sa.Column('source_payment_id', sa.String(length=64), nullable=True),
    sa.Column('amount_minor', sa.Integer(), nullable=False),
    sa.Column('currency', sa.String(length=8), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('short_url', sa.String(length=200), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['merchant_id'], ['merchants.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_payment_links_customer_id'), 'payment_links', ['customer_id'], unique=False)
    op.create_index(op.f('ix_payment_links_merchant_id'), 'payment_links', ['merchant_id'], unique=False)
    op.create_index(op.f('ix_payment_links_source_payment_id'), 'payment_links', ['source_payment_id'], unique=False)
    op.create_table('products',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('merchant_id', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('category', sa.String(length=64), nullable=False),
    sa.Column('price_minor', sa.Integer(), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['merchant_id'], ['merchants.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_products_merchant_id'), 'products', ['merchant_id'], unique=False)
    op.create_table('users',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('tenant_id', sa.String(length=64), nullable=False),
    sa.Column('merchant_id', sa.String(length=64), nullable=False),
    sa.Column('email', sa.String(length=200), nullable=False),
    sa.Column('role', sa.String(length=64), nullable=False),
    sa.Column('permissions', sa.JSON(), nullable=False),
    sa.ForeignKeyConstraint(['merchant_id'], ['merchants.id'], ),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_users_merchant_id'), 'users', ['merchant_id'], unique=False)
    op.create_index(op.f('ix_users_tenant_id'), 'users', ['tenant_id'], unique=False)
    op.create_table('agent_tasks',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('merchant_id', sa.String(length=64), nullable=False),
    sa.Column('user_id', sa.String(length=64), nullable=False),
    sa.Column('request', sa.Text(), nullable=False),
    sa.Column('intent', sa.String(length=64), nullable=True),
    sa.Column('status', sa.Enum('PENDING', 'RUNNING', 'AWAITING_APPROVAL', 'COMPLETED', 'DENIED', 'REJECTED', 'FAILED', 'ABORTED_BUDGET', name='taskstatus', native_enum=False), nullable=False),
    sa.Column('final_answer', sa.Text(), nullable=True),
    sa.Column('findings', sa.JSON(), nullable=False),
    sa.Column('recommendation', sa.JSON(), nullable=True),
    sa.Column('failure_code', sa.String(length=64), nullable=True),
    sa.Column('agent_version', sa.String(length=64), nullable=False),
    sa.Column('model_provider', sa.String(length=32), nullable=True),
    sa.Column('model_version', sa.String(length=64), nullable=False),
    sa.Column('prompt_version', sa.String(length=64), nullable=False),
    sa.Column('tool_registry_version', sa.String(length=64), nullable=True),
    sa.Column('policy_version', sa.String(length=32), nullable=True),
    sa.Column('workflow_version', sa.String(length=32), nullable=True),
    sa.Column('scenario_id', sa.String(length=64), nullable=True),
    sa.Column('incident_id', sa.String(length=64), nullable=True),
    sa.Column('is_replay', sa.Boolean(), nullable=False),
    sa.Column('replayed_from', sa.String(length=64), nullable=True),
    sa.Column('agent_confidence', sa.Float(), nullable=True),
    sa.Column('model_requires_human', sa.Boolean(), nullable=False),
    sa.Column('tool_call_count', sa.Integer(), nullable=False),
    sa.Column('llm_turn_count', sa.Integer(), nullable=False),
    sa.Column('duration_ms', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['incident_id'], ['incidents.id'], ),
    sa.ForeignKeyConstraint(['merchant_id'], ['merchants.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_agent_tasks_incident_id'), 'agent_tasks', ['incident_id'], unique=False)
    op.create_index(op.f('ix_agent_tasks_merchant_id'), 'agent_tasks', ['merchant_id'], unique=False)
    op.create_index(op.f('ix_agent_tasks_scenario_id'), 'agent_tasks', ['scenario_id'], unique=False)
    op.create_index(op.f('ix_agent_tasks_user_id'), 'agent_tasks', ['user_id'], unique=False)
    op.create_table('incident_evidence',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('incident_id', sa.String(length=64), nullable=False),
    sa.Column('key', sa.String(length=128), nullable=False),
    sa.Column('value', sa.JSON(), nullable=False),
    sa.Column('source', sa.String(length=64), nullable=False),
    sa.Column('untrusted', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['incident_id'], ['incidents.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_incident_evidence_incident_id'), 'incident_evidence', ['incident_id'], unique=False)
    op.create_table('orders',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('merchant_id', sa.String(length=64), nullable=False),
    sa.Column('customer_id', sa.String(length=64), nullable=False),
    sa.Column('product_id', sa.String(length=64), nullable=False),
    sa.Column('amount_minor', sa.Integer(), nullable=False),
    sa.Column('currency', sa.String(length=8), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], ),
    sa.ForeignKeyConstraint(['merchant_id'], ['merchants.id'], ),
    sa.ForeignKeyConstraint(['product_id'], ['products.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_orders_created_at'), 'orders', ['created_at'], unique=False)
    op.create_index(op.f('ix_orders_customer_id'), 'orders', ['customer_id'], unique=False)
    op.create_index(op.f('ix_orders_merchant_id'), 'orders', ['merchant_id'], unique=False)
    op.create_table('recovery_plans',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('incident_id', sa.String(length=64), nullable=False),
    sa.Column('merchant_id', sa.String(length=64), nullable=False),
    sa.Column('status', sa.Enum('DRAFT', 'ACTIVE', 'STOPPED', 'ESCALATED', 'COMPLETED', 'EXPIRED', name='planstatus', native_enum=False), nullable=False),
    sa.Column('intervention', sa.Enum('RETRY', 'PAYMENT_LINK', 'CUSTOMER_NOTIFICATION', 'SUBSCRIPTION_RETRY', 'REFUND', 'HUMAN_ESCALATION', 'NO_ACTION', name='intervention', native_enum=False), nullable=False),
    sa.Column('plan_key', sa.String(length=200), nullable=False),
    sa.Column('revenue_at_risk_minor', sa.Integer(), nullable=False),
    sa.Column('eligible_recovery_minor', sa.Integer(), nullable=False),
    sa.Column('expected_recovery_minor', sa.Integer(), nullable=False),
    sa.Column('expected_recovery_basis', sa.Text(), nullable=False),
    sa.Column('max_recovery_minor', sa.Integer(), nullable=False),
    sa.Column('max_actions', sa.Integer(), nullable=False),
    sa.Column('max_attempts_per_customer', sa.Integer(), nullable=False),
    sa.Column('max_duration_seconds', sa.Integer(), nullable=False),
    sa.Column('stop_rule', sa.String(length=64), nullable=True),
    sa.Column('stop_reason', sa.Text(), nullable=True),
    sa.Column('planner_version', sa.String(length=32), nullable=False),
    sa.Column('correlation_id', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['incident_id'], ['incidents.id'], ),
    sa.ForeignKeyConstraint(['merchant_id'], ['merchants.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('plan_key')
    )
    op.create_index(op.f('ix_recovery_plans_correlation_id'), 'recovery_plans', ['correlation_id'], unique=False)
    op.create_index(op.f('ix_recovery_plans_incident_id'), 'recovery_plans', ['incident_id'], unique=False)
    op.create_index(op.f('ix_recovery_plans_merchant_id'), 'recovery_plans', ['merchant_id'], unique=False)
    op.create_index(op.f('ix_recovery_plans_status'), 'recovery_plans', ['status'], unique=False)
    op.create_table('agent_actions',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('task_id', sa.String(length=64), nullable=False),
    sa.Column('merchant_id', sa.String(length=64), nullable=False),
    sa.Column('action_type', sa.String(length=32), nullable=False),
    sa.Column('target_payment_id', sa.String(length=64), nullable=False),
    sa.Column('external_payment_id', sa.String(length=64), nullable=True),
    sa.Column('amount_minor', sa.Integer(), nullable=False),
    sa.Column('idempotency_key', sa.String(length=128), nullable=False),
    sa.Column('status', sa.Enum('PENDING', 'SUBMITTED', 'CONFIRMED', 'FAILED', 'UNKNOWN', name='actionstatus', native_enum=False), nullable=False),
    sa.Column('external_reference', sa.String(length=64), nullable=True),
    sa.Column('verification_state', sa.Enum('SUCCESS', 'FAILED', 'PARTIAL', 'UNKNOWN', name='verificationstate', native_enum=False), nullable=True),
    sa.Column('verification_detail', sa.JSON(), nullable=True),
    sa.Column('verify_attempts', sa.Integer(), nullable=False),
    sa.Column('provider_latency_ms', sa.Float(), nullable=True),
    sa.Column('verification_latency_ms', sa.Float(), nullable=True),
    sa.Column('approval_id', sa.String(length=64), nullable=True),
    sa.Column('recovery_candidate_id', sa.String(length=64), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['merchant_id'], ['merchants.id'], ),
    sa.ForeignKeyConstraint(['task_id'], ['agent_tasks.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('idempotency_key')
    )
    op.create_index(op.f('ix_agent_actions_merchant_id'), 'agent_actions', ['merchant_id'], unique=False)
    op.create_index(op.f('ix_agent_actions_recovery_candidate_id'), 'agent_actions', ['recovery_candidate_id'], unique=False)
    op.create_index(op.f('ix_agent_actions_task_id'), 'agent_actions', ['task_id'], unique=False)
    op.create_table('agent_messages',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('task_id', sa.String(length=64), nullable=False),
    sa.Column('seq', sa.Integer(), nullable=False),
    sa.Column('turn', sa.Integer(), nullable=False),
    sa.Column('role', sa.String(length=16), nullable=False),
    sa.Column('content', sa.JSON(), nullable=False),
    sa.Column('contains_untrusted', sa.Boolean(), nullable=False),
    sa.Column('char_count', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['task_id'], ['agent_tasks.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('task_id', 'seq', name='uq_message_task_seq')
    )
    op.create_index(op.f('ix_agent_messages_task_id'), 'agent_messages', ['task_id'], unique=False)
    op.create_table('approvals',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('task_id', sa.String(length=64), nullable=False),
    sa.Column('merchant_id', sa.String(length=64), nullable=False),
    sa.Column('action_type', sa.String(length=32), nullable=False),
    sa.Column('action_payload', sa.JSON(), nullable=False),
    sa.Column('evidence', sa.JSON(), nullable=False),
    sa.Column('risk_level', sa.String(length=16), nullable=False),
    sa.Column('decision', sa.String(length=32), nullable=False),
    sa.Column('decided_by', sa.String(length=64), nullable=True),
    sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('required_signatures', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['merchant_id'], ['merchants.id'], ),
    sa.ForeignKeyConstraint(['task_id'], ['agent_tasks.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_approvals_merchant_id'), 'approvals', ['merchant_id'], unique=False)
    op.create_index(op.f('ix_approvals_task_id'), 'approvals', ['task_id'], unique=False)
    op.create_table('payments',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('merchant_id', sa.String(length=64), nullable=False),
    sa.Column('order_id', sa.String(length=64), nullable=False),
    sa.Column('customer_id', sa.String(length=64), nullable=False),
    sa.Column('amount_minor', sa.Integer(), nullable=False),
    sa.Column('currency', sa.String(length=8), nullable=False),
    sa.Column('method', sa.String(length=32), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('error_reason', sa.String(length=200), nullable=True),
    sa.Column('amount_refunded_minor', sa.Integer(), nullable=False),
    sa.Column('refund_status', sa.String(length=32), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('external_provider', sa.String(length=32), nullable=True),
    sa.Column('external_payment_id', sa.String(length=64), nullable=True),
    sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], ),
    sa.ForeignKeyConstraint(['merchant_id'], ['merchants.id'], ),
    sa.ForeignKeyConstraint(['order_id'], ['orders.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_payments_created_at'), 'payments', ['created_at'], unique=False)
    op.create_index(op.f('ix_payments_customer_id'), 'payments', ['customer_id'], unique=False)
    op.create_index(op.f('ix_payments_external_payment_id'), 'payments', ['external_payment_id'], unique=False)
    op.create_index('ix_payments_merchant_created', 'payments', ['merchant_id', 'created_at'], unique=False)
    op.create_index(op.f('ix_payments_merchant_id'), 'payments', ['merchant_id'], unique=False)
    op.create_index(op.f('ix_payments_method'), 'payments', ['method'], unique=False)
    op.create_index(op.f('ix_payments_order_id'), 'payments', ['order_id'], unique=False)
    op.create_index(op.f('ix_payments_status'), 'payments', ['status'], unique=False)
    op.create_table('tool_calls',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('task_id', sa.String(length=64), nullable=False),
    sa.Column('seq', sa.Integer(), nullable=False),
    sa.Column('tool_name', sa.String(length=64), nullable=False),
    sa.Column('input', sa.JSON(), nullable=False),
    sa.Column('output', sa.JSON(), nullable=True),
    sa.Column('success', sa.Boolean(), nullable=False),
    sa.Column('error_code', sa.String(length=64), nullable=True),
    sa.Column('risk_level', sa.String(length=16), nullable=True),
    sa.Column('policy_decision', sa.String(length=32), nullable=True),
    sa.Column('duration_ms', sa.Integer(), nullable=False),
    sa.Column('injected_fault', sa.String(length=64), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['task_id'], ['agent_tasks.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('task_id', 'seq', name='uq_toolcall_task_seq')
    )
    op.create_index(op.f('ix_tool_calls_task_id'), 'tool_calls', ['task_id'], unique=False)
    op.create_table('approval_signatures',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('approval_id', sa.String(length=64), nullable=False),
    sa.Column('user_id', sa.String(length=64), nullable=False),
    sa.Column('decision', sa.String(length=16), nullable=False),
    sa.Column('signed_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['approval_id'], ['approvals.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('approval_id', 'user_id', name='uq_signature_approval_user')
    )
    op.create_index(op.f('ix_approval_signatures_approval_id'), 'approval_signatures', ['approval_id'], unique=False)
    op.create_table('recovery_candidates',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('plan_id', sa.String(length=64), nullable=False),
    sa.Column('incident_id', sa.String(length=64), nullable=False),
    sa.Column('merchant_id', sa.String(length=64), nullable=False),
    sa.Column('payment_id', sa.String(length=64), nullable=False),
    sa.Column('customer_id', sa.String(length=64), nullable=False),
    sa.Column('amount_minor', sa.Integer(), nullable=False),
    sa.Column('intervention', sa.Enum('RETRY', 'PAYMENT_LINK', 'CUSTOMER_NOTIFICATION', 'SUBSCRIPTION_RETRY', 'REFUND', 'HUMAN_ESCALATION', 'NO_ACTION', name='intervention', native_enum=False), nullable=False),
    sa.Column('status', sa.Enum('ELIGIBLE', 'INELIGIBLE', 'ATTEMPTED', 'RECOVERED', 'FAILED', 'UNKNOWN', 'SKIPPED', name='candidatestatus', native_enum=False), nullable=False),
    sa.Column('ineligible_reason', sa.String(length=200), nullable=True),
    sa.Column('attributed_amount_minor', sa.Integer(), nullable=False),
    sa.Column('expected_recovery_minor', sa.Integer(), nullable=False),
    sa.Column('actual_recovery_minor', sa.Integer(), nullable=False),
    sa.Column('executable', sa.Boolean(), nullable=False),
    sa.Column('rank', sa.Integer(), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('task_id', sa.String(length=64), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], ),
    sa.ForeignKeyConstraint(['incident_id'], ['incidents.id'], ),
    sa.ForeignKeyConstraint(['merchant_id'], ['merchants.id'], ),
    sa.ForeignKeyConstraint(['payment_id'], ['payments.id'], ),
    sa.ForeignKeyConstraint(['plan_id'], ['recovery_plans.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('plan_id', 'payment_id', name='uq_candidate_plan_payment')
    )
    op.create_index('ix_candidate_plan_status', 'recovery_candidates', ['plan_id', 'status'], unique=False)
    op.create_index(op.f('ix_recovery_candidates_customer_id'), 'recovery_candidates', ['customer_id'], unique=False)
    op.create_index(op.f('ix_recovery_candidates_incident_id'), 'recovery_candidates', ['incident_id'], unique=False)
    op.create_index(op.f('ix_recovery_candidates_merchant_id'), 'recovery_candidates', ['merchant_id'], unique=False)
    op.create_index(op.f('ix_recovery_candidates_payment_id'), 'recovery_candidates', ['payment_id'], unique=False)
    op.create_index(op.f('ix_recovery_candidates_plan_id'), 'recovery_candidates', ['plan_id'], unique=False)
    op.create_index(op.f('ix_recovery_candidates_status'), 'recovery_candidates', ['status'], unique=False)
    op.create_index(op.f('ix_recovery_candidates_task_id'), 'recovery_candidates', ['task_id'], unique=False)
    op.create_table('refunds',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('merchant_id', sa.String(length=64), nullable=False),
    sa.Column('payment_id', sa.String(length=64), nullable=False),
    sa.Column('amount_minor', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('external_reference', sa.String(length=64), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['merchant_id'], ['merchants.id'], ),
    sa.ForeignKeyConstraint(['payment_id'], ['payments.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_refunds_merchant_id'), 'refunds', ['merchant_id'], unique=False)
    op.create_index(op.f('ix_refunds_payment_id'), 'refunds', ['payment_id'], unique=False)
    # ### end Alembic commands ###


def downgrade() -> None:
    """Refused.

    Alembic generated a downgrade that drops all 23 tables, `audit_logs`
    included. That table is append-only by database trigger precisely because
    the history in it is evidence; a command that erases the lot is not a
    migration, and having it one keystroke behind `alembic downgrade base` is a
    worse risk than not being able to unwind a baseline — which nobody needs to
    do anyway, since going back from the baseline means going back to having no
    schema.

    To discard the schema deliberately, say so deliberately:
    `scripts/seed_data.py` with SEED_FORCE=1, or DROP DATABASE.
    """
    raise NotImplementedError(
        "Refusing to drop every table, including the append-only audit log. "
        "See the docstring above.")


def _generated_downgrade_kept_for_reference() -> None:
    # ### what alembic autogenerated; not wired up ###
    op.drop_index(op.f('ix_refunds_payment_id'), table_name='refunds')
    op.drop_index(op.f('ix_refunds_merchant_id'), table_name='refunds')
    op.drop_table('refunds')
    op.drop_index(op.f('ix_recovery_candidates_task_id'), table_name='recovery_candidates')
    op.drop_index(op.f('ix_recovery_candidates_status'), table_name='recovery_candidates')
    op.drop_index(op.f('ix_recovery_candidates_plan_id'), table_name='recovery_candidates')
    op.drop_index(op.f('ix_recovery_candidates_payment_id'), table_name='recovery_candidates')
    op.drop_index(op.f('ix_recovery_candidates_merchant_id'), table_name='recovery_candidates')
    op.drop_index(op.f('ix_recovery_candidates_incident_id'), table_name='recovery_candidates')
    op.drop_index(op.f('ix_recovery_candidates_customer_id'), table_name='recovery_candidates')
    op.drop_index('ix_candidate_plan_status', table_name='recovery_candidates')
    op.drop_table('recovery_candidates')
    op.drop_index(op.f('ix_approval_signatures_approval_id'), table_name='approval_signatures')
    op.drop_table('approval_signatures')
    op.drop_index(op.f('ix_tool_calls_task_id'), table_name='tool_calls')
    op.drop_table('tool_calls')
    op.drop_index(op.f('ix_payments_status'), table_name='payments')
    op.drop_index(op.f('ix_payments_order_id'), table_name='payments')
    op.drop_index(op.f('ix_payments_method'), table_name='payments')
    op.drop_index(op.f('ix_payments_merchant_id'), table_name='payments')
    op.drop_index('ix_payments_merchant_created', table_name='payments')
    op.drop_index(op.f('ix_payments_external_payment_id'), table_name='payments')
    op.drop_index(op.f('ix_payments_customer_id'), table_name='payments')
    op.drop_index(op.f('ix_payments_created_at'), table_name='payments')
    op.drop_table('payments')
    op.drop_index(op.f('ix_approvals_task_id'), table_name='approvals')
    op.drop_index(op.f('ix_approvals_merchant_id'), table_name='approvals')
    op.drop_table('approvals')
    op.drop_index(op.f('ix_agent_messages_task_id'), table_name='agent_messages')
    op.drop_table('agent_messages')
    op.drop_index(op.f('ix_agent_actions_task_id'), table_name='agent_actions')
    op.drop_index(op.f('ix_agent_actions_recovery_candidate_id'), table_name='agent_actions')
    op.drop_index(op.f('ix_agent_actions_merchant_id'), table_name='agent_actions')
    op.drop_table('agent_actions')
    op.drop_index(op.f('ix_recovery_plans_status'), table_name='recovery_plans')
    op.drop_index(op.f('ix_recovery_plans_merchant_id'), table_name='recovery_plans')
    op.drop_index(op.f('ix_recovery_plans_incident_id'), table_name='recovery_plans')
    op.drop_index(op.f('ix_recovery_plans_correlation_id'), table_name='recovery_plans')
    op.drop_table('recovery_plans')
    op.drop_index(op.f('ix_orders_merchant_id'), table_name='orders')
    op.drop_index(op.f('ix_orders_customer_id'), table_name='orders')
    op.drop_index(op.f('ix_orders_created_at'), table_name='orders')
    op.drop_table('orders')
    op.drop_index(op.f('ix_incident_evidence_incident_id'), table_name='incident_evidence')
    op.drop_table('incident_evidence')
    op.drop_index(op.f('ix_agent_tasks_user_id'), table_name='agent_tasks')
    op.drop_index(op.f('ix_agent_tasks_scenario_id'), table_name='agent_tasks')
    op.drop_index(op.f('ix_agent_tasks_merchant_id'), table_name='agent_tasks')
    op.drop_index(op.f('ix_agent_tasks_incident_id'), table_name='agent_tasks')
    op.drop_table('agent_tasks')
    op.drop_index(op.f('ix_users_tenant_id'), table_name='users')
    op.drop_index(op.f('ix_users_merchant_id'), table_name='users')
    op.drop_table('users')
    op.drop_index(op.f('ix_products_merchant_id'), table_name='products')
    op.drop_table('products')
    op.drop_index(op.f('ix_payment_links_source_payment_id'), table_name='payment_links')
    op.drop_index(op.f('ix_payment_links_merchant_id'), table_name='payment_links')
    op.drop_index(op.f('ix_payment_links_customer_id'), table_name='payment_links')
    op.drop_table('payment_links')
    op.drop_index(op.f('ix_notifications_merchant_id'), table_name='notifications')
    op.drop_index(op.f('ix_notifications_customer_id'), table_name='notifications')
    op.drop_table('notifications')
    op.drop_index(op.f('ix_incidents_status'), table_name='incidents')
    op.drop_index('ix_incidents_merchant_status', table_name='incidents')
    op.drop_index(op.f('ix_incidents_merchant_id'), table_name='incidents')
    op.drop_index(op.f('ix_incidents_correlation_id'), table_name='incidents')
    op.drop_table('incidents')
    op.drop_index(op.f('ix_customers_merchant_id'), table_name='customers')
    op.drop_table('customers')
    op.drop_index(op.f('ix_merchants_tenant_id'), table_name='merchants')
    op.drop_table('merchants')
    op.drop_index(op.f('ix_webhook_events_tenant_id'), table_name='webhook_events')
    op.drop_index(op.f('ix_webhook_events_status'), table_name='webhook_events')
    op.drop_index(op.f('ix_webhook_events_received_at'), table_name='webhook_events')
    op.drop_index(op.f('ix_webhook_events_provider'), table_name='webhook_events')
    op.drop_index(op.f('ix_webhook_events_merchant_id'), table_name='webhook_events')
    op.drop_index(op.f('ix_webhook_events_event_type'), table_name='webhook_events')
    op.drop_index(op.f('ix_webhook_events_entity_id'), table_name='webhook_events')
    op.drop_index(op.f('ix_webhook_events_correlation_id'), table_name='webhook_events')
    op.drop_index('ix_webhook_entity_type', table_name='webhook_events')
    op.drop_table('webhook_events')
    op.drop_table('tenants')
    op.drop_index(op.f('ix_evaluation_results_scenario_id'), table_name='evaluation_results')
    op.drop_index(op.f('ix_evaluation_results_run_id'), table_name='evaluation_results')
    op.drop_table('evaluation_results')
    op.drop_index(op.f('ix_audit_logs_task_id'), table_name='audit_logs')
    op.drop_index(op.f('ix_audit_logs_merchant_id'), table_name='audit_logs')
    op.drop_index(op.f('ix_audit_logs_incident_id'), table_name='audit_logs')
    op.drop_index(op.f('ix_audit_logs_event_type'), table_name='audit_logs')
    op.drop_index(op.f('ix_audit_logs_created_at'), table_name='audit_logs')
    op.drop_index(op.f('ix_audit_logs_correlation_id'), table_name='audit_logs')
    op.drop_table('audit_logs')
    # ### end Alembic commands ###
