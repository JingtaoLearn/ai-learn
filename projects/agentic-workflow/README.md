# Agentic Workflow

This project validates isolated product-level suites of real Hermes Agents. It does not build a workflow engine.

Read the canonical vocabulary in [`CONTEXT.md`](CONTEXT.md). The first verified product-level tracer is recorded in [`VALIDATION.md`](VALIDATION.md). The three progressively disclosed setup templates are:

- [`templates/AGENTIC_WORKFLOW_ASSISTANT.md`](templates/AGENTIC_WORKFLOW_ASSISTANT.md) — the global suite maintainer;
- [`templates/PRODUCT_AGENT_SUITE.md`](templates/PRODUCT_AGENT_SUITE.md) — one product's Owner, specialists, Handoff, and capability choices.
- [`templates/PORTFOLIO_STATE.md`](templates/PORTFOLIO_STATE.md) — the reusable multi-Workstream state projection.

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
- `session-messenger` — the shared Skill scaffold for exact Session messages, callbacks, Results, Reviews, decisions, and external Signals.
- `HANDOFF.md` — one bounded task instance with Goal, Evidence, Gap, Action, Workstream, Agent, selected Matt flow and why, acceptance, and safety.
- `RESULT.md` — one Agent's returned outcome and evidence.
- `STATE.md` — a compact supporting projection for recovery and inspection; it does not replace the Owner Session.
- Heartbeat or Loop — a temporary in-Session clock when polling is needed.
- Cron — a temporary durable clock or backstop that starts a fresh isolated Session; it never becomes a fresh Owner.

Profile-private memory is not shared workflow truth. Product decisions remain in the canonical Owner Session; files carry inspectable Goal, Evidence, Handoffs, Results, and a compact State projection. A Profile distribution is unnecessary until an Agent is worth shipping to other machines.

## Product-level topology

`AgenticWorkflow-Assistant` is the global flow maintainer. It creates and maintains isolated Product Agent Suites but does not take over product decisions. Every product starts with one Product Owner Agent and adds a specialist only when an observed Gap requires an independent role.

For the next Quant Research slice:

1. `ProductOwnerAgent-QuantResearch` — owns exactly one canonical persistent Bot Chat/Owner Session, reconciles the Portfolio, selects capacity-bounded Action sets, and owns product decisions.
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

The first live product workspace is:

`/home/jingtao/.hermes/workflows/quant-research`

1. A user message, Cron `bot-chat` delivery, webhook collector, or [`session-messenger`](skills/session-messenger/SKILL.md) envelope delivers a Signal into the product's existing canonical Owner Session.
2. The Owner interprets that Signal against the Goal, Session context, State, and relevant prior Results.
3. The Owner gathers only live Evidence that can change Portfolio decisions and identifies each affected Workstream Gap.
4. The Owner reconciles every non-Done Workstream, builds a Ready set, and chooses a capacity-bounded Action set. Formal work with acceptance, artifacts, blocking, retry, or review becomes a Kanban task; a short consultation may use native `message_agent`.
5. The selected Kanban worker acts directly in its own Profile and does not spawn another Agent for the same Handoff. Formal cards retain at least two transient attempts unless one-attempt fail-closed behavior is justified.
6. Review uses a product-matched Reviewer. `REVISE` preserves the original Result, adds a separately hashed correction, and requires re-review.
7. A verified native subscription or one idempotent `session-messenger` terminal envelope wakes the exact Owner Session. The Owner absorbs verified evidence, records its decision, and updates State when useful.
8. The Owner immediately re-enters `Goal + Principles -> Portfolio Evidence -> Workstream Gaps -> Action set`, fills newly available independent slots, and lets blocked lanes coexist with progress elsewhere. It records a Portfolio-level wait only when no lane can legally advance.

A CLI Owner's formal Handoff carries its exact callback Profile/Session plus stable correlation and idempotency identities. The terminal worker sends the evidence-bearing envelope after terminal board state; the recovery Pulse only repairs a missed wake.

Heartbeat and Loop can temporarily poll from the Owner Session. A zero-reasoning, script-only Cron job can provide a durable recovery clock and deliver its stdout directly to the canonical Owner Session with `deliver: bot-chat`. Pulses recover stalled loops; they do not choose product Actions. An Agent Cron runs in a fresh isolated Session and is not the Owner.

Scheduled Signals prefer Cron `deliver=bot-chat:<owner-profile>`. Immediate GitHub/CI adapters, production monitors, and data checks may reuse the same `agent-message/v1` envelope with a source label and no callback Session. Transport selection does not fork the business message contract.

For same-machine named functional Agents, use separate Profiles. Use Bot-to-Bot messaging for short live canonical Bot consultation, `delegate_task` only for anonymous short-lived reasoning inside one Agent, A2A only across process, machine, or framework boundaries, and Kanban whenever formal work needs acceptance, artifacts, blocking, retry, review, crash recovery, or auditability.

## Current boundary

- No workflow engine, message broker, database, state machine, outbox, retry loop, dead-letter queue, or general connector framework. One Skill plus one standard-library script dispatches an addressed message to one exact Session.
- The scaffold intentionally ships without a test suite. Real Agent-to-Owner-to-Agent callback and non-Session Signal traces are the functional evidence.
- No automatic merge, deployment, production-signal change, paid/public action, or other high-risk external effect.
- High-risk actions still require Jingtao's explicit approval at the existing tool boundary.

## Current status

The heavy draft was discarded. The current [`session-messenger`](skills/session-messenger/SKILL.md) scaffold proves exact addressed Research→Owner delivery, Owner→Research callback, Research acknowledgment to Owner, and a non-Session Signal entering that same persistent Owner Session. Native `message_agent` is retained for short canonical Bot Chat exchanges, Kanban for formal work, and Cron `bot-chat` for scheduled Signals. See [`VALIDATION.md`](VALIDATION.md).

This remains functional validation. The next product action is the separately governed QuantResearch evidence Spike selected by the Owner; it is not part of the Agentic Workflow tracer.
