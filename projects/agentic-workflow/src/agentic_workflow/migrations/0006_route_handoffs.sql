CREATE TABLE matt_invocations_v6 (
    invocation_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    action_id TEXT NOT NULL REFERENCES actions(action_id),
    action_envelope_id TEXT NOT NULL REFERENCES action_envelopes(action_envelope_id),
    invocation_json TEXT NOT NULL,
    invocation_digest TEXT NOT NULL UNIQUE,
    skill_name TEXT NOT NULL,
    skill_digest TEXT NOT NULL,
    executor_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    constitution_revision INTEGER NOT NULL,
    goal_revision INTEGER NOT NULL,
    operating_profile_revision INTEGER NOT NULL,
    active_intent_digest TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (executor_id, run_id),
    FOREIGN KEY (project_id, constitution_revision)
        REFERENCES constitution_revisions(project_id, revision_number),
    FOREIGN KEY (project_id, goal_revision)
        REFERENCES goal_revisions(project_id, revision_number),
    FOREIGN KEY (project_id, operating_profile_revision)
        REFERENCES operating_profile_revisions(project_id, revision_number)
) STRICT;

INSERT INTO matt_invocations_v6 SELECT * FROM matt_invocations;

CREATE TABLE matt_execution_attempts_v6 (
    attempt_id TEXT PRIMARY KEY,
    invocation_id TEXT NOT NULL UNIQUE REFERENCES matt_invocations_v6(invocation_id),
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    action_envelope_id TEXT NOT NULL REFERENCES action_envelopes(action_envelope_id),
    executor_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    active_intent_digest TEXT NOT NULL,
    attempt_json TEXT NOT NULL,
    attempt_digest TEXT NOT NULL UNIQUE,
    attempted_at TEXT NOT NULL,
    UNIQUE (executor_id, run_id)
) STRICT;

INSERT INTO matt_execution_attempts_v6 SELECT * FROM matt_execution_attempts;

CREATE TABLE matt_execution_observations_v6 (
    observation_id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL UNIQUE REFERENCES matt_execution_attempts_v6(attempt_id),
    observation_json TEXT NOT NULL,
    observation_digest TEXT NOT NULL UNIQUE,
    outcome TEXT NOT NULL CHECK (outcome IN ('RETURNED', 'AMBIGUOUS', 'REJECTED')),
    observed_at TEXT NOT NULL
) STRICT;

INSERT INTO matt_execution_observations_v6 SELECT * FROM matt_execution_observations;

CREATE TABLE matt_executor_attestations_v6 (
    attestation_id TEXT PRIMARY KEY,
    invocation_id TEXT NOT NULL UNIQUE REFERENCES matt_invocations_v6(invocation_id),
    attestation_json TEXT NOT NULL,
    attestation_digest TEXT NOT NULL UNIQUE,
    executor_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    UNIQUE (executor_id, run_id)
) STRICT;

INSERT INTO matt_executor_attestations_v6 SELECT * FROM matt_executor_attestations;

CREATE TABLE matt_receipts_v6 (
    receipt_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    invocation_id TEXT NOT NULL UNIQUE REFERENCES matt_invocations_v6(invocation_id),
    attestation_id TEXT NOT NULL UNIQUE
        REFERENCES matt_executor_attestations_v6(attestation_id),
    action_envelope_id TEXT NOT NULL REFERENCES action_envelopes(action_envelope_id),
    receipt_json TEXT NOT NULL,
    receipt_digest TEXT NOT NULL UNIQUE,
    constitution_revision INTEGER NOT NULL,
    goal_revision INTEGER NOT NULL,
    operating_profile_revision INTEGER NOT NULL,
    active_intent_digest TEXT NOT NULL,
    accepted_at TEXT NOT NULL,
    FOREIGN KEY (project_id, constitution_revision)
        REFERENCES constitution_revisions(project_id, revision_number),
    FOREIGN KEY (project_id, goal_revision)
        REFERENCES goal_revisions(project_id, revision_number),
    FOREIGN KEY (project_id, operating_profile_revision)
        REFERENCES operating_profile_revisions(project_id, revision_number)
) STRICT;

INSERT INTO matt_receipts_v6 SELECT * FROM matt_receipts;

DROP TABLE matt_receipts;
DROP TABLE matt_executor_attestations;
DROP TABLE matt_execution_observations;
DROP TABLE matt_execution_attempts;
DROP TABLE matt_invocations;
ALTER TABLE matt_invocations_v6 RENAME TO matt_invocations;
ALTER TABLE matt_execution_attempts_v6 RENAME TO matt_execution_attempts;
ALTER TABLE matt_execution_observations_v6 RENAME TO matt_execution_observations;
ALTER TABLE matt_executor_attestations_v6 RENAME TO matt_executor_attestations;
ALTER TABLE matt_receipts_v6 RENAME TO matt_receipts;

CREATE TRIGGER matt_invocations_no_update BEFORE UPDATE ON matt_invocations
BEGIN SELECT RAISE(ABORT, 'Matt Invocations are append-only'); END;
CREATE TRIGGER matt_invocations_no_delete BEFORE DELETE ON matt_invocations
BEGIN SELECT RAISE(ABORT, 'Matt Invocations are append-only'); END;
CREATE TRIGGER matt_execution_attempts_no_update BEFORE UPDATE ON matt_execution_attempts
BEGIN SELECT RAISE(ABORT, 'Matt execution attempts are append-only'); END;
CREATE TRIGGER matt_execution_attempts_no_delete BEFORE DELETE ON matt_execution_attempts
BEGIN SELECT RAISE(ABORT, 'Matt execution attempts are append-only'); END;
CREATE TRIGGER matt_execution_observations_no_update BEFORE UPDATE ON matt_execution_observations
BEGIN SELECT RAISE(ABORT, 'Matt execution observations are append-only'); END;
CREATE TRIGGER matt_execution_observations_no_delete BEFORE DELETE ON matt_execution_observations
BEGIN SELECT RAISE(ABORT, 'Matt execution observations are append-only'); END;
CREATE TRIGGER matt_executor_attestations_no_update BEFORE UPDATE ON matt_executor_attestations
BEGIN SELECT RAISE(ABORT, 'Matt executor attestations are append-only'); END;
CREATE TRIGGER matt_executor_attestations_no_delete BEFORE DELETE ON matt_executor_attestations
BEGIN SELECT RAISE(ABORT, 'Matt executor attestations are append-only'); END;
CREATE TRIGGER matt_receipts_no_update BEFORE UPDATE ON matt_receipts
BEGIN SELECT RAISE(ABORT, 'Matt Receipts are append-only'); END;
CREATE TRIGGER matt_receipts_no_delete BEFORE DELETE ON matt_receipts
BEGIN SELECT RAISE(ABORT, 'Matt Receipts are append-only'); END;

CREATE TABLE capability_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    adapter_id TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    snapshot_digest TEXT NOT NULL UNIQUE,
    accepted_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
) STRICT;

CREATE TABLE capability_matrices (
    matrix_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    matrix_version INTEGER NOT NULL CHECK (matrix_version > 0),
    matrix_json TEXT NOT NULL,
    matrix_digest TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    UNIQUE (project_id, matrix_version)
) STRICT;

CREATE TABLE capability_matrix_candidates (
    matrix_id TEXT NOT NULL REFERENCES capability_matrices(matrix_id),
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    candidate_number INTEGER NOT NULL CHECK (candidate_number > 0),
    candidate_digest TEXT NOT NULL,
    snapshot_digest TEXT NOT NULL REFERENCES capability_snapshots(snapshot_digest),
    candidate_json TEXT NOT NULL,
    PRIMARY KEY (matrix_id, candidate_number),
    UNIQUE (matrix_id, candidate_digest)
) STRICT;

CREATE TABLE route_plans (
    plan_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    action_id TEXT NOT NULL UNIQUE REFERENCES actions(action_id),
    matrix_id TEXT NOT NULL REFERENCES capability_matrices(matrix_id),
    matrix_digest TEXT NOT NULL REFERENCES capability_matrices(matrix_digest),
    plan_json TEXT NOT NULL,
    plan_digest TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
) STRICT;

CREATE TABLE route_envelopes (
    route_envelope_digest TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    action_id TEXT NOT NULL UNIQUE REFERENCES actions(action_id),
    action_envelope_id TEXT NOT NULL UNIQUE REFERENCES action_envelopes(action_envelope_id),
    plan_id TEXT NOT NULL REFERENCES route_plans(plan_id),
    plan_digest TEXT NOT NULL REFERENCES route_plans(plan_digest),
    matrix_id TEXT NOT NULL REFERENCES capability_matrices(matrix_id),
    matrix_digest TEXT NOT NULL REFERENCES capability_matrices(matrix_digest),
    route_envelope_json TEXT NOT NULL
) STRICT;

CREATE TABLE attempts (
    attempt_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    action_id TEXT NOT NULL REFERENCES actions(action_id),
    attempt_number INTEGER NOT NULL CHECK (attempt_number > 0),
    parent_handoff_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (action_id, attempt_number)
) STRICT;

CREATE TABLE runs (
    run_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    attempt_id TEXT NOT NULL UNIQUE REFERENCES attempts(attempt_id),
    executor_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (executor_id, run_id)
) STRICT;

CREATE TABLE handoffs (
    handoff_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    action_id TEXT NOT NULL REFERENCES actions(action_id),
    action_envelope_id TEXT NOT NULL REFERENCES action_envelopes(action_envelope_id),
    attempt_id TEXT NOT NULL UNIQUE REFERENCES attempts(attempt_id),
    run_id TEXT NOT NULL UNIQUE REFERENCES runs(run_id),
    parent_handoff_id TEXT REFERENCES handoffs(handoff_id),
    delivery_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    package_json TEXT NOT NULL,
    handoff_package_digest TEXT NOT NULL UNIQUE,
    constitution_revision INTEGER NOT NULL,
    goal_revision INTEGER NOT NULL,
    operating_profile_revision INTEGER NOT NULL,
    active_intent_digest TEXT NOT NULL,
    offered_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    UNIQUE (project_id, delivery_id),
    UNIQUE (project_id, idempotency_key),
    FOREIGN KEY (project_id, constitution_revision)
        REFERENCES constitution_revisions(project_id, revision_number),
    FOREIGN KEY (project_id, goal_revision)
        REFERENCES goal_revisions(project_id, revision_number),
    FOREIGN KEY (project_id, operating_profile_revision)
        REFERENCES operating_profile_revisions(project_id, revision_number)
) STRICT;

CREATE TABLE handoff_events (
    handoff_id TEXT NOT NULL REFERENCES handoffs(handoff_id),
    event_number INTEGER NOT NULL CHECK (event_number > 0),
    event_type TEXT NOT NULL CHECK (
        event_type IN (
            'OFFERED', 'ACCEPTED', 'RUNNING', 'RESULT_RECORDED', 'VERIFIED',
            'REJECTED', 'BLOCKED_EXTERNAL', 'SUPERSEDED', 'FAILED', 'AMBIGUOUS',
            'EXPIRED', 'RETRY_REQUESTED'
        )
    ),
    event_json TEXT NOT NULL,
    event_digest TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (handoff_id, event_number),
    UNIQUE (handoff_id, event_type)
) STRICT;

CREATE TABLE handoff_retry_commands (
    command_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    handoff_id TEXT NOT NULL UNIQUE REFERENCES handoffs(handoff_id),
    retry_handoff_id TEXT NOT NULL UNIQUE REFERENCES handoffs(handoff_id),
    command_json TEXT NOT NULL,
    command_digest TEXT NOT NULL UNIQUE,
    expected_attempt_number INTEGER NOT NULL CHECK (expected_attempt_number > 0),
    max_attempts INTEGER NOT NULL CHECK (max_attempts > 0),
    recorded_at TEXT NOT NULL
) STRICT;

CREATE TABLE route_executor_attestations (
    attestation_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    handoff_id TEXT NOT NULL UNIQUE REFERENCES handoffs(handoff_id),
    attempt_id TEXT NOT NULL UNIQUE REFERENCES attempts(attempt_id),
    run_id TEXT NOT NULL UNIQUE REFERENCES runs(run_id),
    adapter_id TEXT NOT NULL,
    attestation_json TEXT NOT NULL,
    attestation_digest TEXT NOT NULL UNIQUE,
    recorded_at TEXT NOT NULL
) STRICT;

CREATE TABLE route_receipts (
    receipt_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    handoff_id TEXT NOT NULL UNIQUE REFERENCES handoffs(handoff_id),
    attempt_id TEXT NOT NULL UNIQUE REFERENCES attempts(attempt_id),
    run_id TEXT NOT NULL UNIQUE REFERENCES runs(run_id),
    attestation_id TEXT NOT NULL UNIQUE REFERENCES route_executor_attestations(attestation_id),
    action_envelope_id TEXT NOT NULL REFERENCES action_envelopes(action_envelope_id),
    receipt_json TEXT NOT NULL,
    receipt_digest TEXT NOT NULL UNIQUE,
    constitution_revision INTEGER NOT NULL,
    goal_revision INTEGER NOT NULL,
    operating_profile_revision INTEGER NOT NULL,
    active_intent_digest TEXT NOT NULL,
    accepted_at TEXT NOT NULL,
    FOREIGN KEY (project_id, constitution_revision)
        REFERENCES constitution_revisions(project_id, revision_number),
    FOREIGN KEY (project_id, goal_revision)
        REFERENCES goal_revisions(project_id, revision_number),
    FOREIGN KEY (project_id, operating_profile_revision)
        REFERENCES operating_profile_revisions(project_id, revision_number)
) STRICT;

CREATE TRIGGER capability_snapshots_no_update BEFORE UPDATE ON capability_snapshots
BEGIN SELECT RAISE(ABORT, 'Capability Snapshots are append-only'); END;
CREATE TRIGGER capability_snapshots_no_delete BEFORE DELETE ON capability_snapshots
BEGIN SELECT RAISE(ABORT, 'Capability Snapshots are append-only'); END;
CREATE TRIGGER capability_matrices_no_update BEFORE UPDATE ON capability_matrices
BEGIN SELECT RAISE(ABORT, 'Capability Matrices are append-only'); END;
CREATE TRIGGER capability_matrices_no_delete BEFORE DELETE ON capability_matrices
BEGIN SELECT RAISE(ABORT, 'Capability Matrices are append-only'); END;
CREATE TRIGGER capability_matrix_candidates_no_update BEFORE UPDATE ON capability_matrix_candidates
BEGIN SELECT RAISE(ABORT, 'Capability Matrix candidates are append-only'); END;
CREATE TRIGGER capability_matrix_candidates_no_delete BEFORE DELETE ON capability_matrix_candidates
BEGIN SELECT RAISE(ABORT, 'Capability Matrix candidates are append-only'); END;
CREATE TRIGGER route_plans_no_update BEFORE UPDATE ON route_plans
BEGIN SELECT RAISE(ABORT, 'Route Plans are append-only'); END;
CREATE TRIGGER route_plans_no_delete BEFORE DELETE ON route_plans
BEGIN SELECT RAISE(ABORT, 'Route Plans are append-only'); END;
CREATE TRIGGER route_envelopes_no_update BEFORE UPDATE ON route_envelopes
BEGIN SELECT RAISE(ABORT, 'Route Envelopes are append-only'); END;
CREATE TRIGGER route_envelopes_no_delete BEFORE DELETE ON route_envelopes
BEGIN SELECT RAISE(ABORT, 'Route Envelopes are append-only'); END;
CREATE TRIGGER attempts_no_update BEFORE UPDATE ON attempts
BEGIN SELECT RAISE(ABORT, 'Attempts are append-only'); END;
CREATE TRIGGER attempts_no_delete BEFORE DELETE ON attempts
BEGIN SELECT RAISE(ABORT, 'Attempts are append-only'); END;
CREATE TRIGGER runs_no_update BEFORE UPDATE ON runs
BEGIN SELECT RAISE(ABORT, 'Runs are append-only'); END;
CREATE TRIGGER runs_no_delete BEFORE DELETE ON runs
BEGIN SELECT RAISE(ABORT, 'Runs are append-only'); END;
CREATE TRIGGER handoffs_no_update BEFORE UPDATE ON handoffs
BEGIN SELECT RAISE(ABORT, 'Handoffs are append-only'); END;
CREATE TRIGGER handoffs_no_delete BEFORE DELETE ON handoffs
BEGIN SELECT RAISE(ABORT, 'Handoffs are append-only'); END;
CREATE TRIGGER handoff_events_no_update BEFORE UPDATE ON handoff_events
BEGIN SELECT RAISE(ABORT, 'Handoff events are append-only'); END;
CREATE TRIGGER handoff_events_no_delete BEFORE DELETE ON handoff_events
BEGIN SELECT RAISE(ABORT, 'Handoff events are append-only'); END;
CREATE TRIGGER handoff_retry_commands_no_update BEFORE UPDATE ON handoff_retry_commands
BEGIN SELECT RAISE(ABORT, 'Handoff retry commands are append-only'); END;
CREATE TRIGGER handoff_retry_commands_no_delete BEFORE DELETE ON handoff_retry_commands
BEGIN SELECT RAISE(ABORT, 'Handoff retry commands are append-only'); END;
CREATE TRIGGER route_executor_attestations_no_update BEFORE UPDATE ON route_executor_attestations
BEGIN SELECT RAISE(ABORT, 'Route executor attestations are append-only'); END;
CREATE TRIGGER route_executor_attestations_no_delete BEFORE DELETE ON route_executor_attestations
BEGIN SELECT RAISE(ABORT, 'Route executor attestations are append-only'); END;
CREATE TRIGGER route_receipts_no_update BEFORE UPDATE ON route_receipts
BEGIN SELECT RAISE(ABORT, 'Route Receipts are append-only'); END;
CREATE TRIGGER route_receipts_no_delete BEFORE DELETE ON route_receipts
BEGIN SELECT RAISE(ABORT, 'Route Receipts are append-only'); END;
