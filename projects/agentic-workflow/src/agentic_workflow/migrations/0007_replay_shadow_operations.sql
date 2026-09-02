DROP TRIGGER IF EXISTS operation_records_no_update;
DROP TRIGGER IF EXISTS operation_records_no_delete;
DROP TRIGGER IF EXISTS operation_events_no_update;
DROP TRIGGER IF EXISTS operation_events_no_delete;

ALTER TABLE operation_records ADD COLUMN target_identity_json TEXT;
ALTER TABLE operation_records ADD COLUMN target_identity_digest TEXT;
ALTER TABLE operation_records ADD COLUMN expected_target_version TEXT;
ALTER TABLE operation_records ADD COLUMN side_effect_class TEXT;
ALTER TABLE operation_records ADD COLUMN idempotency_identity TEXT;
ALTER TABLE operation_records ADD COLUMN exact_spend_cap_json TEXT;
ALTER TABLE operation_records ADD COLUMN exact_spend_cap_digest TEXT;
ALTER TABLE operation_records ADD COLUMN approval_required INTEGER;
ALTER TABLE operation_records ADD COLUMN approval_expires_at TEXT;
ALTER TABLE operation_records ADD COLUMN mode TEXT;
ALTER TABLE operation_records ADD COLUMN legacy_operation_json TEXT;
ALTER TABLE operation_records ADD COLUMN legacy_operation_digest TEXT;

CREATE UNIQUE INDEX operation_records_idempotency_identity_uq
ON operation_records(idempotency_identity) WHERE idempotency_identity IS NOT NULL;
CREATE INDEX operation_records_target_version_idx
ON operation_records(target_identity_digest, expected_target_version);

CREATE TABLE operation_events_v7 (
    operation_id TEXT NOT NULL REFERENCES operation_records(operation_id),
    event_number INTEGER NOT NULL CHECK (event_number > 0),
    event_type TEXT NOT NULL CHECK (
        event_type IN (
            'RESERVED', 'CONCLUDED', 'AWAITING_APPROVAL', 'PREPARED',
            'ATTEMPT_INTENT', 'ATTEMPT_RETURNED', 'READBACK_RECORDED',
            'APPLIED', 'NOT_APPLIED', 'AMBIGUOUS'
        )
    ),
    payload_json TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    constitution_revision INTEGER NOT NULL,
    goal_revision INTEGER NOT NULL,
    operating_profile_revision INTEGER NOT NULL,
    active_intent_digest TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (operation_id, event_number),
    UNIQUE (operation_id, event_type)
) STRICT;

INSERT INTO operation_events_v7 SELECT * FROM operation_events;
DROP TABLE operation_events;
ALTER TABLE operation_events_v7 RENAME TO operation_events;

CREATE TABLE operation_attempts (
    attempt_id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL UNIQUE REFERENCES operation_records(operation_id),
    attempt_json TEXT NOT NULL,
    attempt_digest TEXT NOT NULL UNIQUE,
    attempted_at TEXT NOT NULL
) STRICT;

CREATE TABLE operation_evidence (
    evidence_id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL REFERENCES operation_records(operation_id),
    evidence_number INTEGER NOT NULL CHECK (evidence_number > 0),
    evidence_kind TEXT NOT NULL CHECK (
        evidence_kind IN ('ATTEMPT_RESULT', 'READBACK', 'OUTCOME')
    ),
    evidence_json TEXT NOT NULL,
    evidence_digest TEXT NOT NULL UNIQUE,
    recorded_at TEXT NOT NULL,
    UNIQUE (operation_id, evidence_number),
    UNIQUE (operation_id, evidence_kind)
) STRICT;

CREATE TABLE operation_approvals (
    approval_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    operation_id TEXT NOT NULL UNIQUE REFERENCES operation_records(operation_id),
    decision_event_digest TEXT NOT NULL UNIQUE,
    approval_json TEXT NOT NULL,
    approval_digest TEXT NOT NULL UNIQUE,
    expires_at TEXT NOT NULL,
    approved_at TEXT NOT NULL
) STRICT;

CREATE TABLE operation_approval_consumptions (
    consumption_id TEXT PRIMARY KEY,
    approval_id TEXT NOT NULL UNIQUE REFERENCES operation_approvals(approval_id),
    operation_id TEXT NOT NULL UNIQUE REFERENCES operation_records(operation_id),
    consumption_json TEXT NOT NULL,
    consumption_digest TEXT NOT NULL UNIQUE,
    consumed_at TEXT NOT NULL
) STRICT;

CREATE TRIGGER operation_attempts_no_update BEFORE UPDATE ON operation_attempts
BEGIN SELECT RAISE(ABORT, 'operation attempts are append-only'); END;
CREATE TRIGGER operation_attempts_no_delete BEFORE DELETE ON operation_attempts
BEGIN SELECT RAISE(ABORT, 'operation attempts are append-only'); END;
CREATE TRIGGER operation_evidence_no_update BEFORE UPDATE ON operation_evidence
BEGIN SELECT RAISE(ABORT, 'operation evidence is append-only'); END;
CREATE TRIGGER operation_evidence_no_delete BEFORE DELETE ON operation_evidence
BEGIN SELECT RAISE(ABORT, 'operation evidence is append-only'); END;
CREATE TRIGGER operation_approvals_no_update BEFORE UPDATE ON operation_approvals
BEGIN SELECT RAISE(ABORT, 'operation approvals are append-only'); END;
CREATE TRIGGER operation_approvals_no_delete BEFORE DELETE ON operation_approvals
BEGIN SELECT RAISE(ABORT, 'operation approvals are append-only'); END;
CREATE TRIGGER operation_approval_consumptions_no_update
BEFORE UPDATE ON operation_approval_consumptions
BEGIN SELECT RAISE(ABORT, 'operation approval consumptions are append-only'); END;
CREATE TRIGGER operation_approval_consumptions_no_delete
BEFORE DELETE ON operation_approval_consumptions
BEGIN SELECT RAISE(ABORT, 'operation approval consumptions are append-only'); END;

DROP TRIGGER daily_briefs_no_update;
DROP TRIGGER daily_briefs_no_delete;
ALTER TABLE daily_briefs ADD COLUMN brief_id TEXT;
ALTER TABLE daily_briefs ADD COLUMN local_day TEXT;
ALTER TABLE daily_briefs ADD COLUMN timezone TEXT;
ALTER TABLE daily_briefs ADD COLUMN source_evidence_digest TEXT;
CREATE UNIQUE INDEX daily_briefs_id_uq ON daily_briefs(brief_id) WHERE brief_id IS NOT NULL;
CREATE UNIQUE INDEX daily_briefs_closed_day_uq
ON daily_briefs(project_id, local_day) WHERE local_day IS NOT NULL;
CREATE TRIGGER daily_briefs_no_update BEFORE UPDATE ON daily_briefs
BEGIN SELECT RAISE(ABORT, 'daily briefs are append-only'); END;
CREATE TRIGGER daily_briefs_no_delete BEFORE DELETE ON daily_briefs
BEGIN SELECT RAISE(ABORT, 'daily briefs are append-only'); END;

CREATE TABLE outbox_events (
    outbox_event_id TEXT PRIMARY KEY,
    logical_outbox_identity TEXT NOT NULL UNIQUE,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    brief_id TEXT NOT NULL UNIQUE,
    local_day TEXT NOT NULL,
    event_json TEXT NOT NULL,
    event_digest TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    UNIQUE (project_id, local_day)
) STRICT;

CREATE TABLE outbox_delivery_attempts (
    attempt_id TEXT PRIMARY KEY,
    outbox_event_id TEXT NOT NULL REFERENCES outbox_events(outbox_event_id),
    attempt_number INTEGER NOT NULL CHECK (attempt_number > 0),
    outcome TEXT NOT NULL CHECK (outcome IN ('DELIVERY_FAILED', 'ACKNOWLEDGED')),
    result_json TEXT NOT NULL,
    result_digest TEXT NOT NULL UNIQUE,
    recorded_at TEXT NOT NULL,
    UNIQUE (outbox_event_id, attempt_number)
) STRICT;

CREATE TABLE outbox_acknowledgements (
    acknowledgement_id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL UNIQUE REFERENCES outbox_delivery_attempts(attempt_id),
    outbox_event_id TEXT NOT NULL UNIQUE REFERENCES outbox_events(outbox_event_id),
    acknowledgement_json TEXT NOT NULL,
    acknowledgement_digest TEXT NOT NULL UNIQUE,
    transport_receipt_id TEXT NOT NULL UNIQUE,
    acknowledged_at TEXT NOT NULL
) STRICT;

CREATE TRIGGER outbox_events_no_update BEFORE UPDATE ON outbox_events
BEGIN SELECT RAISE(ABORT, 'outbox events are append-only'); END;
CREATE TRIGGER outbox_events_no_delete BEFORE DELETE ON outbox_events
BEGIN SELECT RAISE(ABORT, 'outbox events are append-only'); END;
CREATE TRIGGER outbox_delivery_attempts_no_update BEFORE UPDATE ON outbox_delivery_attempts
BEGIN SELECT RAISE(ABORT, 'outbox delivery attempts are append-only'); END;
CREATE TRIGGER outbox_delivery_attempts_no_delete BEFORE DELETE ON outbox_delivery_attempts
BEGIN SELECT RAISE(ABORT, 'outbox delivery attempts are append-only'); END;
CREATE TRIGGER outbox_acknowledgements_no_update BEFORE UPDATE ON outbox_acknowledgements
BEGIN SELECT RAISE(ABORT, 'outbox acknowledgements are append-only'); END;
CREATE TRIGGER outbox_acknowledgements_no_delete BEFORE DELETE ON outbox_acknowledgements
BEGIN SELECT RAISE(ABORT, 'outbox acknowledgements are append-only'); END;
