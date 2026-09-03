# State

## Phase

Hermes Agent resource-model alignment.

## Current reality

- The heavy Agentic Workflow implementation has been removed from the active path.
- Official Hermes documentation confirms that a dedicated functional Agent is a Profile/Bot with its own SOUL, config, memory, sessions, skills, cron, and state.
- Skills are on-demand procedures; Toolsets are capabilities; AGENTS.md is project context; Cron is only a durable trigger.
- Two Agent processes must not share the same profile.
- Runs 001 and 002 proved the Markdown information shape, but both reused the default profile and therefore do not prove independent dedicated Agent nodes.
- The default-profile prototype Owner Cron is paused.

## Current frontier

Replace the same-profile prototype with two minimal `--no-skills` profiles:

1. `awfowner` — Goal-aware Owner and router.
2. `awfscout` — read-oriented project-reality specialist.

Give each one a short SOUL, explicit profile description, narrow Skill set, and narrow Toolsets. Move the Owner Cron into `awfowner`. Then run one real `awfowner → awfscout → Result → awfowner decision` tracer against current quantitative-platform information.

## Deferred

Create Builder, Reviewer, or Operations profiles only when an observed action needs them. Do not add Kanban, A2A, profile distributions, a custom runtime, database, tests, or mandatory validation framework during this phase.
