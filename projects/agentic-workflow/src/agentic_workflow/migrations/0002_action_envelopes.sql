CREATE TABLE actions (
    action_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    action_kind TEXT NOT NULL,
    action_json TEXT NOT NULL,
    action_digest TEXT NOT NULL UNIQUE,
    constitution_revision INTEGER NOT NULL,
    goal_revision INTEGER NOT NULL,
    operating_profile_revision INTEGER NOT NULL,
    active_intent_digest TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (project_id, constitution_revision)
        REFERENCES constitution_revisions(project_id, revision_number),
    FOREIGN KEY (project_id, goal_revision)
        REFERENCES goal_revisions(project_id, revision_number),
    FOREIGN KEY (project_id, operating_profile_revision)
        REFERENCES operating_profile_revisions(project_id, revision_number)
) STRICT;

CREATE TABLE action_envelopes (
    action_envelope_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    action_id TEXT NOT NULL UNIQUE REFERENCES actions(action_id),
    predecessor_action_envelope_id TEXT REFERENCES action_envelopes(action_envelope_id),
    envelope_json TEXT NOT NULL,
    action_envelope_digest TEXT NOT NULL UNIQUE,
    constitution_revision INTEGER NOT NULL,
    goal_revision INTEGER NOT NULL,
    operating_profile_revision INTEGER NOT NULL,
    active_intent_digest TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (project_id, constitution_revision)
        REFERENCES constitution_revisions(project_id, revision_number),
    FOREIGN KEY (project_id, goal_revision)
        REFERENCES goal_revisions(project_id, revision_number),
    FOREIGN KEY (project_id, operating_profile_revision)
        REFERENCES operating_profile_revisions(project_id, revision_number)
) STRICT;

CREATE TRIGGER actions_no_update
BEFORE UPDATE ON actions
BEGIN SELECT RAISE(ABORT, 'actions are append-only'); END;
CREATE TRIGGER actions_no_delete
BEFORE DELETE ON actions
BEGIN SELECT RAISE(ABORT, 'actions are append-only'); END;
CREATE TRIGGER action_envelopes_no_update
BEFORE UPDATE ON action_envelopes
BEGIN SELECT RAISE(ABORT, 'action envelopes are append-only'); END;
CREATE TRIGGER action_envelopes_no_delete
BEFORE DELETE ON action_envelopes
BEGIN SELECT RAISE(ABORT, 'action envelopes are append-only'); END;
