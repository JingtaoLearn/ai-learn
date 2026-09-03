# Information Sources

The Owner Agent chooses which sources matter for the current decision. It may read them with existing Hermes tools; no ingestion service is required.

## Durable shared files

- `GOAL.md` — current goal and phase boundary.
- `STATE.md` — compact current reality and next useful question.
- `INBOX.md` — new user or automation signals not yet absorbed.
- `runs/*/RESULT.md` — outputs from prior specialist Agents.
- `runs/*/DECISION.md` — prior Owner decisions.

## Live sources available on demand

- User messages and recent Hermes sessions.
- GitHub issues, pull requests, branches, and current diffs.
- Hermes Kanban tasks when work is deliberately scheduled there.
- Existing Cron jobs and their latest outputs.
- Production health, logs, reports, or files relevant to the current Goal.
- Web or knowledge sources when research is needed.

A busy source is not automatically important. The Owner reads a source only when it can change the next decision.
