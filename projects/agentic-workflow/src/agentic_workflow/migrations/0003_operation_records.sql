CREATE TABLE operation_records (
    operation_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    action_envelope_id TEXT NOT NULL UNIQUE REFERENCES action_envelopes(action_envelope_id),
    operation_json TEXT NOT NULL,
    operation_digest TEXT NOT NULL UNIQUE,
    constitution_revision INTEGER NOT NULL,
    goal_revision INTEGER NOT NULL,
    operating_profile_revision INTEGER NOT NULL,
    active_intent_digest TEXT NOT NULL,
    reserved_at TEXT NOT NULL,
    FOREIGN KEY (project_id, constitution_revision)
        REFERENCES constitution_revisions(project_id, revision_number),
    FOREIGN KEY (project_id, goal_revision)
        REFERENCES goal_revisions(project_id, revision_number),
    FOREIGN KEY (project_id, operating_profile_revision)
        REFERENCES operating_profile_revisions(project_id, revision_number)
) STRICT;

CREATE TABLE operation_events (
    operation_id TEXT NOT NULL REFERENCES operation_records(operation_id),
    event_number INTEGER NOT NULL CHECK (event_number > 0),
    event_type TEXT NOT NULL CHECK (event_type IN ('RESERVED', 'CONCLUDED')),
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

CREATE TRIGGER operation_records_no_update
BEFORE UPDATE ON operation_records
BEGIN SELECT RAISE(ABORT, 'operation records are append-only'); END;
CREATE TRIGGER operation_records_no_delete
BEFORE DELETE ON operation_records
BEGIN SELECT RAISE(ABORT, 'operation records are append-only'); END;
CREATE TRIGGER operation_events_no_update
BEFORE UPDATE ON operation_events
BEGIN SELECT RAISE(ABORT, 'operation events are append-only'); END;
CREATE TRIGGER operation_events_no_delete
BEFORE DELETE ON operation_events
BEGIN SELECT RAISE(ABORT, 'operation events are append-only'); END;
