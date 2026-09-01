CREATE TABLE compatibility_decisions (
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    active_intent_digest TEXT NOT NULL,
    source_action_envelope_digest TEXT NOT NULL
        REFERENCES action_envelopes(action_envelope_digest),
    verdict TEXT NOT NULL CHECK (verdict IN ('compatible', 'incompatible', 'unknown')),
    decision_json TEXT NOT NULL,
    decision_digest TEXT NOT NULL UNIQUE,
    constitution_revision INTEGER NOT NULL,
    goal_revision INTEGER NOT NULL,
    operating_profile_revision INTEGER NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (project_id, active_intent_digest, source_action_envelope_digest),
    FOREIGN KEY (project_id, constitution_revision)
        REFERENCES constitution_revisions(project_id, revision_number),
    FOREIGN KEY (project_id, goal_revision)
        REFERENCES goal_revisions(project_id, revision_number),
    FOREIGN KEY (project_id, operating_profile_revision)
        REFERENCES operating_profile_revisions(project_id, revision_number)
) STRICT;

CREATE TRIGGER compatibility_decisions_no_update
BEFORE UPDATE ON compatibility_decisions
BEGIN SELECT RAISE(ABORT, 'compatibility decisions are append-only'); END;
CREATE TRIGGER compatibility_decisions_no_delete
BEFORE DELETE ON compatibility_decisions
BEGIN SELECT RAISE(ABORT, 'compatibility decisions are append-only'); END;
