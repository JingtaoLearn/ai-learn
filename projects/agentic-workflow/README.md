# Agentic Workflow

This project validates a small network of real Hermes Agents. It does not build a workflow engine.

## Hermes resource model

A dedicated functional Agent is a Hermes **Profile** (and therefore can appear as a Bot). Each profile has its own identity, model configuration, memory, sessions, skills, cron jobs, and state directory. Two Agent processes must never share one profile.

Each resource has one job:

- `SOUL.md` — who this Agent is and how it makes judgments.
- Profile description — what this Agent is good at; routing metadata for the roster and optional Kanban use.
- Skills — procedures the Agent may load on demand. A Skill is not an Agent or a workflow stage.
- Toolsets — capabilities available to the Agent. Keep them narrow per function.
- `AGENTS.md` — project-local context automatically loaded from the working directory.
- `GOAL.md` — the current Jingtao-owned objective shared by all participating Agents.
- `HANDOFF.md` — one bounded task instance.
- `RESULT.md` — one Agent's returned outcome and evidence.
- `STATE.md` — the Owner's compact cross-session project state.
- Cron — a durable wake-up for the Owner profile; it is not the decision maker.

Profile-private memory is not shared workflow state. Shared project truth stays in the fixed files. A profile distribution is unnecessary until an Agent is worth shipping to other machines.

## Initial Agent roster

Create only two profiles for the first useful tracer:

1. `awfowner` — reads the Goal and current information, chooses one next action and one functional Agent, reads the result, and updates State.
2. `awfscout` — gathers current project reality from files, GitHub, production evidence, sessions, or the web and returns a concise Result. It does not choose the product Goal.

Create `awfbuilder`, `awfreviewer`, or `awfops` only after a real selected action requires that independent function. Do not prebuild an Agent fleet.

Both initial profiles should be created with `--no-skills`, receive a short explicit description and SOUL, and install only their own small Skill set. The default profile remains Jingtao's user-facing Hermes and is not reused concurrently as a specialist.

## Prompt and context stack

Keep each layer small and non-duplicative:

1. Profile `SOUL.md`: stable role and judgment style.
2. Profile memory: private learned facts only.
3. Project `AGENTS.md`: project conventions and resource locations.
4. Skill: reusable method for the selected function.
5. `GOAL.md`: current project objective and boundaries.
6. `HANDOFF.md`: this invocation's bounded task.
7. Tool results: live evidence gathered during the run.

## Live information flow

The fixed shared directory is:

`/home/jingtao/.hermes/workflows/agentic-workflow`

1. A Cron job owned by `awfowner` starts one fresh Owner session in the shared directory.
2. Project context loads automatically; the Owner reads Goal, State, Inbox, and relevant prior Result files.
3. The Owner queries only live sources that can change the next decision.
4. The Owner writes one Handoff and invokes a different profile, initially `awfscout`.
5. The specialist reads the same Goal plus the Handoff and writes Result.
6. The Owner reads Result, writes Decision, updates State, and stops.
7. The next Owner pulse continues from the shared files.

For same-machine named functional Agents, use separate profiles. Use `delegate_task` only for anonymous, short-lived reasoning inside one Agent; use A2A only across process, machine, or framework boundaries; use Kanban only when durable multi-day work actually appears.

## Current boundary

- No custom Python runtime, database, state machine, routing engine, or connector framework.
- No tests are written or run during functional validation.
- No automatic merge, deployment, production-signal change, paid/public action, or other high-risk external effect.
- High-risk actions still require Jingtao's explicit approval at the existing tool boundary.

## Current status

The heavy draft was discarded. The first two file-flow runs proved the file shape but reused the default profile, so they do not yet prove independent dedicated Agent nodes. The default-profile Owner Cron is paused while `awfowner` and `awfscout` are designed and created.
