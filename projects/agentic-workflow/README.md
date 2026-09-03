# Agentic Workflow

This project validates isolated product-level suites of real Hermes Agents. It does not build a workflow engine.

Read the canonical vocabulary in [`CONTEXT.md`](CONTEXT.md). The two progressively disclosed setup templates are:

- [`templates/AGENTIC_WORKFLOW_ASSISTANT.md`](templates/AGENTIC_WORKFLOW_ASSISTANT.md) — the global suite maintainer;
- [`templates/PRODUCT_AGENT_SUITE.md`](templates/PRODUCT_AGENT_SUITE.md) — one product's Owner, specialists, Handoff, and capability choices.

## Hermes resource model

A dedicated functional Agent is a Hermes **Profile** and therefore can appear as a Bot. Each Profile has its own identity, model configuration, memory, Sessions, Skills, Cron jobs, and state directory. Two Agent processes never share one Profile.

Each resource has one job:

- `SOUL.md` — who this Agent is and how it makes judgments.
- Profile description — what this Agent is good at; routing metadata for the roster and optional Kanban use.
- Skills — procedures the Agent may load on demand. A Skill is not an Agent or a workflow stage.
- Toolsets — capabilities available to the Agent. Keep them narrow per function.
- `AGENTS.md` — project-local context automatically loaded from the working directory.
- `GOAL.md` — the current Jingtao-owned objective shared by all participating Agents and changed only when Jingtao changes it.
- Canonical Bot Chat — the Product Owner Agent's persistent Owner Session and product decision brain.
- Task Session — an optional ephemeral conversation for one specialist action.
- `HANDOFF.md` — one bounded task instance with Goal, Evidence, Gap, Action, Agent, selected Matt flow and why, acceptance, and safety.
- `RESULT.md` — one Agent's returned outcome and evidence.
- `STATE.md` — a compact supporting projection for recovery and inspection; it does not replace the Owner Session.
- Heartbeat or Loop — a temporary in-Session clock when polling is needed.
- Cron — a temporary durable clock or backstop that starts a fresh isolated Session; it never becomes a fresh Owner.

Profile-private memory is not shared workflow truth. Product decisions remain in the canonical Owner Session; files carry inspectable Goal, Evidence, Handoffs, Results, and a compact State projection. A Profile distribution is unnecessary until an Agent is worth shipping to other machines.

## Product-level topology

`AgenticWorkflow-Assistant` is the global flow maintainer. It creates and maintains isolated Product Agent Suites but does not take over product decisions. Every product starts with one Product Owner Agent and adds a specialist only when an observed Gap requires an independent role.

For the next Quant Research slice:

1. `ProductOwnerAgent-QuantResearch` — owns exactly one canonical persistent Bot Chat/Owner Session, interprets Signals, chooses the next bounded Action, and owns product decisions.
2. `ResearchAgent-QuantResearch` — gathers current project reality and returns concise Evidence for a Handoff. It does not choose the product Goal.

Display names contain exactly two UpperCamelCase segments joined by one hyphen: `<RoleAgent>-<Product>`. Profile IDs may follow Hermes' machine-name constraints, but the display name must preserve this product-visible form. Create Builder, Reviewer, or Operations Agents only after a selected Action demonstrates the independent function. The default Profile remains Jingtao's user-facing Hermes and is not reused concurrently as a specialist.

## Prompt and context stack

Keep each layer small and non-duplicative:

1. Profile `SOUL.md`: stable role and judgment style.
2. Profile memory: private learned facts only.
3. Project `AGENTS.md`: project conventions and resource locations.
4. Skill: reusable method for the selected function.
5. Canonical Owner Session: accumulated product decisions and active context.
6. `GOAL.md`: current project objective and boundaries.
7. `HANDOFF.md`: this invocation's bounded task.
8. Tool results: live Evidence gathered during the run.

## Matt Skill routing

When an observed Gap and proposed Action may benefit from a Matt Skill, use the authoritative [Matt routing table](templates/AGENTIC_WORKFLOW_ASSISTANT.md#matt-routing). Record the selected method and reason in the Handoff.

## Live information flow

The fixed shared directory is:

`/home/jingtao/.hermes/workflows/agentic-workflow`

1. A user message or Bot-to-Bot message delivers a Signal into the product's existing canonical Owner Session.
2. The Owner interprets that Signal against the Goal, Session context, State, and relevant prior Results.
3. The Owner gathers only live Evidence that can change the next decision and identifies the current Gap.
4. The Owner chooses one bounded Action, selects a Matt flow only when its method fits, and writes one complete Handoff.
5. A specialist acts in its own Profile and may use an ephemeral Task Session; it returns a Result to the same Owner Session.
6. The Owner absorbs the Result, records its decision, updates the State projection when useful, and waits for the next Signal.

Heartbeat and Loop can temporarily poll from the Owner Session. A zero-reasoning, script-only Cron job can provide a durable temporary clock and deliver its stdout directly to the canonical Owner Session with `deliver: bot-chat`. An Agent Cron runs in a fresh isolated Session and is not the Owner.

Native Hermes webhooks trigger Agent runs or deliver to configured user-facing platforms; they do not currently target `bot-chat`. A real-time external product event therefore needs a future adapter or relay whose delivery into the canonical Owner Session is verified end to end before that route is recorded as live.

For same-machine named functional Agents, use separate Profiles. Use Bot-to-Bot messaging for named Bots, `delegate_task` only for anonymous short-lived reasoning inside one Agent, A2A only across process, machine, or framework boundaries, and Kanban only when durable multi-day work actually appears.

## Current boundary

- No custom Python runtime, database, state machine, routing engine, or connector framework.
- No tests are written or run during functional validation.
- No automatic merge, deployment, production-signal change, paid/public action, or other high-risk external effect.
- High-risk actions still require Jingtao's explicit approval at the existing tool boundary.

## Current status

The heavy draft was discarded. The first two file-flow runs proved the file shape but reused the default Profile, so they do not yet prove independent dedicated Agent nodes or continuous product ownership. The next slice uses the templates above to create `ProductOwnerAgent-QuantResearch` and `ResearchAgent-QuantResearch` only after Jingtao authorizes live Profile changes.
