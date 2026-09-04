# Product Agent Suite Template

Copy this document for one product only. Replace every `<Product>` with one UpperCamelCase product segment such as `QuantResearch`. Defining a suite does not authorize creating live Profiles or changing permissions, schedules, production, merge, deployment, paid, or public state.

## Product contract

- Product: `<Product>`
- Jingtao-owned Goal: `<link to the canonical Goal>`
- Principles: `<unchanged owner-approved Principles>`
- Portfolio state contract: [`PORTFOLIO_STATE.md`](PORTFOLIO_STATE.md)
- Safety boundary: `<allowed effects and existing approval gates>`
- Canonical Product Owner display name: `ProductOwnerAgent-<Product>`
- Canonical Owner Session: `<persistent Bot Chat identifier or title>`
- Exact-Session callback adapter: `session-messenger` installed in every participating Profile
- Verified Signal routes: `<Cron bot-chat, webhook/Kanban, or immediate session-messenger adapter into the exact Owner Session>`
- Temporary clock/backstop: `<none, Heartbeat, Loop, or Cron with removal condition>`

The Owner Session is the product decision brain. `GOAL.md`, `STATE.md`, `INBOX.md`, Handoffs, Results, and Decisions are inspectable supporting artifacts. They do not replace the Session.

The Owner continuously re-enters `Goal + Principles -> Portfolio Evidence -> Workstream Gaps -> Action set` after every Signal, Result, Review, Decision, or recovery Pulse. The Goal supplies direction and the owner-approved Principles constrain every valid move. Finishing one Action only reopens capacity. The Owner fills every safe independent execution slot rather than serializing the whole product behind one global frontier.

Maintain a compact live role registry containing each display name, Profile ID, responsibility, canonical Session when one exists, model/reasoning policy, and readiness evidence. Inspect it together with `hermes profile list` before assigning or requesting a role.

## Display-name rule

Every product Agent display name has exactly two UpperCamelCase segments joined by one hyphen:

`<RoleAgent>-<Product>`

Valid examples:

- `ProductOwnerAgent-QuantResearch`
- `ResearchAgent-QuantResearch`

A role or product name that needs another hyphen must be recast as one UpperCamelCase segment. Record the separate Hermes Profile ID beside the display name when the native machine identifier differs.

## Model and reasoning policy

Every Agent uses `gpt-5.6-sol`. No Claude model is selected anywhere: not as a default, task override, fallback, or review model.

- `ProductOwnerAgent-<Product>` uses `max` reasoning.
- Research, Scout, and other bounded execution specialists use exactly `high` reasoning.
- `AgenticWorkflow-Assistant` and independent Reviewer Agents use exactly `xhigh` reasoning.

Record both model and reasoning effort in the live Profile configuration and in any Kanban or Cron override that can replace Profile defaults.

## Smallest suite

| Display name | Responsibility | Interface | Initial Toolsets | Disclosed Skills | Creation evidence |
|---|---|---|---|---|---|
| `ProductOwnerAgent-<Product>` | Own the Goal-aware next decision in one canonical Owner Session | Signal and Evidence in; Handoff or Owner Decision out | `file`, `web`, `session_search`, `skills` | Only methods selected for observed Gaps | Required for every product |
| `<SpecialistRoleAgent>-<Product>` | Close one recurring class of Gap without taking product ownership | Complete Handoff in; sourced Result out | `<narrow role capabilities>` | `<methods needed by this role>` | `<observed Gap requiring an independent role>` |

Delete the specialist row until its creation evidence exists. Add further rows one at a time; a hypothetical role is not a suite requirement.

Profiles isolate Hermes state, not operating-system filesystem access. Set an explicit workspace and keep Toolsets narrow when filesystem or external-system access matters.

A Kanban worker is already the background Agent for its card. When the selected Matt method says to spawn a background researcher, a Research worker applies the method directly in its current Session instead of spawning the same Handoff again. Nested Agents are reserved for genuinely independent subproblems with separate ownership.

Use the board's normal transient-failure budget for formal work. Set `max_retries` to at least `2` unless a documented deterministic hazard requires one-attempt fail-closed behavior. A reviewer crash must not silently become a PASS or force a cross-product reviewer substitution.

## Signal contract

A Signal is new information, not merely elapsed time.

1. Deliver the Signal into the existing canonical Owner Session.
2. Interpret it against the Goal and accumulated product decisions.
3. Gather only Evidence that can change the decision.
4. Reconcile every non-Done Workstream, then build the Ready set.
5. Select an Action set that fills all safe independent execution slots under Profile, workspace, dependency, idempotency, and resource constraints.
6. Route formal work through Kanban, short live canonical Bot Chat consultation through `message_agent`, and headless exact-Session callbacks through `session-messenger`.
7. Return every Specialist Result to this same Owner Session through the selected native or callback route.
8. Reconcile the whole Portfolio immediately after accepting, rejecting, or invalidating any Result; refill capacity or record lane-local and Portfolio legal waits with exact wake conditions.

If the Owner card was created from a plain CLI Session, do not assume Kanban auto-subscribed that Session to `notify+wake`. On terminal task state, send one idempotent `session-messenger` RESULT/REVIEW envelope to the exact Owner Session unless a verified native subscription already delivered it.

Every formal Handoff for a CLI Owner records the callback Profile, exact Session, stable correlation/idempotency identities, required terminal message type, and artifact/hash evidence. The terminal worker—normally the final Reviewer—sends that envelope after the board reaches terminal state. A recovery Pulse may repair a missed wake, but the next Action remains the Owner's decision.

For a replyable `session-messenger` message, include both exact Session endpoints; the receiver swaps them, preserves the stable `correlation_id`, sets `causation_id` to the inbound `message_id`, and advances the bounded hop count. Scheduled Signals prefer Cron `bot-chat`; immediate source adapters may use the same `agent-message/v1` envelope with a source label and no callback Session. Record the condition that removes every timer.

Review never rewrites immutable specialist evidence. A material finding creates a separately hashed additive correction, returns the card to the original specialist, and requires another independent review before the Owner accepts the package.

## Handoff template

### Goal

`<The unchanged owner-owned objective relevant to this Action>`

### Evidence

- `<Fact — source>`

### Gap

`<One missing knowledge, judgment, or result preventing progress>`

### Action

`<One bounded outcome, not a list of stages>`

### Workstream

`<One stable Portfolio lane ID>`

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

### Owner terminal callback

- Profile: `<exact Product Owner Profile>`
- Session: `<exact canonical Owner Session>`
- Correlation: `<stable Action/run identity>`
- Terminal type mapping: `<PASS/done -> REVIEW; attributed blocked -> BLOCKED; REVISE is non-terminal>`
- Idempotency keys: `<literal stable key for each terminal mapping>`
- Sender: `<terminal worker, normally the final Reviewer>`
- Evidence: `<artifact paths and hashes to include>`

## Owner completion check

Before sending the Handoff, verify that all eleven headings are present and specific and that the selected Agent still resolves to `gpt-5.6-sol` with the role's required reasoning effort. Before accepting the Result, verify every Acceptance item from Evidence rather than treating completion prose as proof. Then update the affected Workstream, reconcile every other non-Done lane, dispatch the selected Action set, and update supporting State only when the compact projection changed.

`continue` makes one lane eligible for another bounded Action. A Portfolio-level `wait` is legal only when every non-Done lane is Active, Waiting, Blocked, Parked with a reconsider trigger, has no positive-value bounded Action, or the Goal is objectively complete. A recovery Pulse only wakes the exact Owner Session to reconcile the Portfolio; the Owner alone selects work.
