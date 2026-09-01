CREATE TABLE projects (
    project_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    project_json TEXT NOT NULL,
    project_digest TEXT NOT NULL,
    created_at TEXT NOT NULL
) STRICT;

CREATE TRIGGER projects_no_update
BEFORE UPDATE ON projects
BEGIN SELECT RAISE(ABORT, 'projects are append-only'); END;
CREATE TRIGGER projects_no_delete
BEFORE DELETE ON projects
BEGIN SELECT RAISE(ABORT, 'projects are append-only'); END;

CREATE TABLE constitution_revisions (
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    revision_number INTEGER NOT NULL CHECK (revision_number > 0),
    payload_json TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    PRIMARY KEY (project_id, revision_number)
) STRICT;

CREATE TABLE goal_revisions (
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    revision_number INTEGER NOT NULL CHECK (revision_number > 0),
    payload_json TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    PRIMARY KEY (project_id, revision_number)
) STRICT;

CREATE TABLE operating_profile_revisions (
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    revision_number INTEGER NOT NULL CHECK (revision_number > 0),
    payload_json TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    PRIMARY KEY (project_id, revision_number)
) STRICT;

CREATE TRIGGER constitution_revisions_no_update
BEFORE UPDATE ON constitution_revisions
BEGIN SELECT RAISE(ABORT, 'constitution revisions are append-only'); END;
CREATE TRIGGER constitution_revisions_no_delete
BEFORE DELETE ON constitution_revisions
BEGIN SELECT RAISE(ABORT, 'constitution revisions are append-only'); END;
CREATE TRIGGER goal_revisions_no_update
BEFORE UPDATE ON goal_revisions
BEGIN SELECT RAISE(ABORT, 'goal revisions are append-only'); END;
CREATE TRIGGER goal_revisions_no_delete
BEFORE DELETE ON goal_revisions
BEGIN SELECT RAISE(ABORT, 'goal revisions are append-only'); END;
CREATE TRIGGER operating_profile_revisions_no_update
BEFORE UPDATE ON operating_profile_revisions
BEGIN SELECT RAISE(ABORT, 'operating profile revisions are append-only'); END;
CREATE TRIGGER operating_profile_revisions_no_delete
BEFORE DELETE ON operating_profile_revisions
BEGIN SELECT RAISE(ABORT, 'operating profile revisions are append-only'); END;

CREATE TABLE active_intents (
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    intent_number INTEGER NOT NULL CHECK (intent_number > 0),
    constitution_revision INTEGER NOT NULL,
    goal_revision INTEGER NOT NULL,
    operating_profile_revision INTEGER NOT NULL,
    active_intent_digest TEXT NOT NULL,
    activated_at TEXT NOT NULL,
    PRIMARY KEY (project_id, intent_number),
    FOREIGN KEY (project_id, constitution_revision)
        REFERENCES constitution_revisions(project_id, revision_number),
    FOREIGN KEY (project_id, goal_revision)
        REFERENCES goal_revisions(project_id, revision_number),
    FOREIGN KEY (project_id, operating_profile_revision)
        REFERENCES operating_profile_revisions(project_id, revision_number)
) STRICT;

CREATE TRIGGER active_intents_no_update
BEFORE UPDATE ON active_intents
BEGIN SELECT RAISE(ABORT, 'active intents are append-only'); END;
CREATE TRIGGER active_intents_no_delete
BEFORE DELETE ON active_intents
BEGIN SELECT RAISE(ABORT, 'active intents are append-only'); END;

CREATE TABLE active_intent_current (
    project_id TEXT PRIMARY KEY REFERENCES projects(project_id),
    intent_number INTEGER NOT NULL CHECK (intent_number > 0),
    FOREIGN KEY (project_id, intent_number)
        REFERENCES active_intents(project_id, intent_number)
) STRICT;

CREATE TABLE inbox_events (
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    source TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    event_digest TEXT NOT NULL,
    event_json TEXT NOT NULL,
    receipt_json TEXT NOT NULL,
    receipt_digest TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (project_id, source, source_event_id)
) STRICT;

CREATE TRIGGER inbox_events_no_update
BEFORE UPDATE ON inbox_events
BEGIN SELECT RAISE(ABORT, 'inbox events are append-only'); END;
CREATE TRIGGER inbox_events_no_delete
BEFORE DELETE ON inbox_events
BEGIN SELECT RAISE(ABORT, 'inbox events are append-only'); END;

CREATE TABLE decision_nonces (
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    actor_id TEXT NOT NULL,
    nonce TEXT NOT NULL,
    replay_identity TEXT NOT NULL,
    source TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    PRIMARY KEY (project_id, actor_id, nonce),
    UNIQUE (project_id, actor_id, replay_identity),
    FOREIGN KEY (project_id, source, source_event_id)
        REFERENCES inbox_events(project_id, source, source_event_id)
) STRICT;

CREATE TRIGGER decision_nonces_no_update
BEFORE UPDATE ON decision_nonces
BEGIN SELECT RAISE(ABORT, 'decision nonces are append-only'); END;
CREATE TRIGGER decision_nonces_no_delete
BEFORE DELETE ON decision_nonces
BEGIN SELECT RAISE(ABORT, 'decision nonces are append-only'); END;

CREATE TABLE daily_briefs (
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    brief_number INTEGER NOT NULL CHECK (brief_number > 0),
    projection_json TEXT NOT NULL,
    projection_digest TEXT NOT NULL,
    projected_at TEXT NOT NULL,
    PRIMARY KEY (project_id, brief_number)
) STRICT;

CREATE TRIGGER daily_briefs_no_update
BEFORE UPDATE ON daily_briefs
BEGIN SELECT RAISE(ABORT, 'daily briefs are append-only'); END;
CREATE TRIGGER daily_briefs_no_delete
BEFORE DELETE ON daily_briefs
BEGIN SELECT RAISE(ABORT, 'daily briefs are append-only'); END;
