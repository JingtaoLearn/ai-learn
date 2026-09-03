# Product Agent Suite Template

Copy this document for one product only. Replace every `<Product>` with one UpperCamelCase product segment such as `QuantResearch`. Defining a suite does not authorize creating live Profiles or changing permissions, schedules, production, merge, deployment, paid, or public state.

## Product contract

- Product: `<Product>`
- Jingtao-owned Goal: `<link to the canonical Goal>`
- Principles: `<unchanged owner-approved Principles>`
- Safety boundary: `<allowed effects and existing approval gates>`
- Canonical Product Owner display name: `ProductOwnerAgent-<Product>`
- Canonical Owner Session: `<persistent Bot Chat identifier or title>`
- Real-time Signal routes: `<user, Agent, webhook, or product-event routes into that Session>`
- Temporary clock/backstop: `<none, Heartbeat, Loop, or Cron with removal condition>`

The Owner Session is the product decision brain. `GOAL.md`, `STATE.md`, `INBOX.md`, Handoffs, Results, and Decisions are inspectable supporting artifacts. They do not replace the Session.

## Display-name rule

Every product Agent display name has exactly two UpperCamelCase segments joined by one hyphen:

`<RoleAgent>-<Product>`

Valid examples:

- `ProductOwnerAgent-QuantResearch`
- `ResearchAgent-QuantResearch`

A role or product name that needs another hyphen must be recast as one UpperCamelCase segment. Record the separate Hermes Profile ID beside the display name when the native machine identifier differs.

## Model and reasoning policy

Every Agent in the suite uses `gpt-5.6-sol`. Claude models are not available and must not be selected as defaults, task overrides, fallbacks, or review models.

- `ProductOwnerAgent-<Product>` uses `max` reasoning.
- Research and other bounded execution specialists use at least `high` reasoning.
- Agents making architecture, workflow-maintenance, or independent-review judgments use `xhigh` unless they are the Product Owner, which remains `max`.

Record both model and reasoning effort in the live Profile configuration and in any Kanban or Cron override that can replace Profile defaults.

## Smallest suite

| Display name | Responsibility | Interface | Initial Toolsets | Disclosed Skills | Creation evidence |
|---|---|---|---|---|---|
| `ProductOwnerAgent-<Product>` | Own the Goal-aware next decision in one canonical Owner Session | Signal and Evidence in; Handoff or Owner Decision out | `file`, `web`, `session_search`, `skills` | Only methods selected for observed Gaps | Required for every product |
| `<SpecialistRoleAgent>-<Product>` | Close one recurring class of Gap without taking product ownership | Complete Handoff in; sourced Result out | `<narrow role capabilities>` | `<methods needed by this role>` | `<observed Gap requiring an independent role>` |

Delete the specialist row until its creation evidence exists. Add further rows one at a time; a hypothetical role is not a suite requirement.

Profiles isolate Hermes state, not operating-system filesystem access. Set an explicit workspace and keep Toolsets narrow when filesystem or external-system access matters.

## Signal contract

A Signal is new information, not merely elapsed time.

1. Deliver the Signal into the existing canonical Owner Session.
2. Interpret it against the Goal and accumulated product decisions.
3. Gather only Evidence that can change the decision.
4. Identify the current Gap and choose one bounded Action.
5. Return every Specialist Result to this same Owner Session.

Prefer native event delivery from users, Bots, gateways, or webhooks. Heartbeat and Loop may poll inside the Owner Session. Cron may be a durable temporary backstop, but its fresh isolated run only observes information and delivers a Signal to the Owner Session. Record the condition that removes every timer.

## Handoff template

### Goal

`<The unchanged owner-owned objective relevant to this Action>`

### Evidence

- `<Fact — source>`

### Gap

`<One missing knowledge, judgment, or result preventing progress>`

### Action

`<One bounded outcome, not a list of stages>`

### Agent

`<One display name from this Product Agent Suite>`

### Selected Matt flow

`<domain-modeling | codebase-design | writing-for-agents | research | to-spec | none>`

### Why this flow

`<How this professional method fits the observed Gap and Action, or why direct action is sufficient>`

### Acceptance

- `<Checkable condition>`
- `<Every required output and Evidence item accounted for>`

### Safety

- Allowed: `<effects this Action may perform>`
- Approval required: `<team, permission, production, merge, deployment, paid, public, or other gated effects>`
- Preserve: `<Goal, Principles, data, and existing controls that remain unchanged>`

## Owner completion check

Before sending the Handoff, verify that all nine headings are present and specific and that the selected Agent still resolves to `gpt-5.6-sol` with the role's required reasoning effort. Before accepting the Result, verify every Acceptance item from Evidence rather than treating completion prose as proof. Then record `continue`, `wait`, `stop`, or `ask Jingtao` in the Owner Session and update supporting State only when the compact projection changed.
