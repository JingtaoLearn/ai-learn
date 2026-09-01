CREATE TABLE matt_invocations (
    invocation_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    action_id TEXT NOT NULL UNIQUE REFERENCES actions(action_id),
    action_envelope_id TEXT NOT NULL UNIQUE REFERENCES action_envelopes(action_envelope_id),
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

CREATE TABLE matt_execution_attempts (
    attempt_id TEXT PRIMARY KEY,
    invocation_id TEXT NOT NULL UNIQUE REFERENCES matt_invocations(invocation_id),
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    action_envelope_id TEXT NOT NULL UNIQUE REFERENCES action_envelopes(action_envelope_id),
    executor_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    active_intent_digest TEXT NOT NULL,
    attempt_json TEXT NOT NULL,
    attempt_digest TEXT NOT NULL UNIQUE,
    attempted_at TEXT NOT NULL,
    UNIQUE (executor_id, run_id)
) STRICT;

CREATE TABLE matt_execution_observations (
    observation_id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL UNIQUE REFERENCES matt_execution_attempts(attempt_id),
    observation_json TEXT NOT NULL,
    observation_digest TEXT NOT NULL UNIQUE,
    outcome TEXT NOT NULL CHECK (outcome IN ('RETURNED', 'AMBIGUOUS', 'REJECTED')),
    observed_at TEXT NOT NULL
) STRICT;

CREATE TABLE matt_executor_attestations (
    attestation_id TEXT PRIMARY KEY,
    invocation_id TEXT NOT NULL UNIQUE REFERENCES matt_invocations(invocation_id),
    attestation_json TEXT NOT NULL,
    attestation_digest TEXT NOT NULL UNIQUE,
    executor_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    UNIQUE (executor_id, run_id)
) STRICT;

CREATE TABLE matt_receipts (
    receipt_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    invocation_id TEXT NOT NULL UNIQUE REFERENCES matt_invocations(invocation_id),
    attestation_id TEXT NOT NULL UNIQUE REFERENCES matt_executor_attestations(attestation_id),
    action_envelope_id TEXT NOT NULL UNIQUE REFERENCES action_envelopes(action_envelope_id),
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

CREATE TRIGGER matt_invocations_no_update
BEFORE UPDATE ON matt_invocations
BEGIN SELECT RAISE(ABORT, 'Matt Invocations are append-only'); END;
CREATE TRIGGER matt_invocations_no_delete
BEFORE DELETE ON matt_invocations
BEGIN SELECT RAISE(ABORT, 'Matt Invocations are append-only'); END;
CREATE TRIGGER matt_execution_attempts_no_update
BEFORE UPDATE ON matt_execution_attempts
BEGIN SELECT RAISE(ABORT, 'Matt execution attempts are append-only'); END;
CREATE TRIGGER matt_execution_attempts_no_delete
BEFORE DELETE ON matt_execution_attempts
BEGIN SELECT RAISE(ABORT, 'Matt execution attempts are append-only'); END;
CREATE TRIGGER matt_execution_observations_no_update
BEFORE UPDATE ON matt_execution_observations
BEGIN SELECT RAISE(ABORT, 'Matt execution observations are append-only'); END;
CREATE TRIGGER matt_execution_observations_no_delete
BEFORE DELETE ON matt_execution_observations
BEGIN SELECT RAISE(ABORT, 'Matt execution observations are append-only'); END;
CREATE TRIGGER matt_executor_attestations_no_update
BEFORE UPDATE ON matt_executor_attestations
BEGIN SELECT RAISE(ABORT, 'Matt executor attestations are append-only'); END;
CREATE TRIGGER matt_executor_attestations_no_delete
BEFORE DELETE ON matt_executor_attestations
BEGIN SELECT RAISE(ABORT, 'Matt executor attestations are append-only'); END;
CREATE TRIGGER matt_receipts_no_update
BEFORE UPDATE ON matt_receipts
BEGIN SELECT RAISE(ABORT, 'Matt Receipts are append-only'); END;
CREATE TRIGGER matt_receipts_no_delete
BEFORE DELETE ON matt_receipts
BEGIN SELECT RAISE(ABORT, 'Matt Receipts are append-only'); END;
