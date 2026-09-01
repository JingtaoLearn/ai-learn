# Agentic Workflow V1 Solution Design

- Status: Accepted for proof Spikes
- Date: 2026-09-01
- Product intent: [`product-intent.md`](product-intent.md)
- Domain glossary: [`../CONTEXT.md`](../CONTEXT.md)
- Decisions: [`adr/`](adr/)

## 1. Selected shape

Build one deep Python `WorkflowKernel` backed by one SQLite control ledger. Keep GitHub, Hermes Kanban, Copilot, Feng, and delivery systems authoritative for their native facts and reach them through one outbound effect seam.

```text
CLI / Cron / Webhook adapters
              ↓
WorkflowKernel: record | advance | view
       ↓                     ↓
SQLite control ledger   ExternalEffects
                         GitHub / Kanban
                         Copilot / Feng / Delivery
```

V1 is a modular monolith, not a distributed workflow platform. It runs as one process, allows one active writer Pulse per project, and attempts at most one external effect per `advance`.

## 2. Public interface

```python
class WorkflowKernel:
    def record(self, event: RecordEvent) -> RecordReceipt: ...
    def advance(self, project_id: ProjectId) -> AdvanceResult: ...
    def view(self, project_id: ProjectId) -> ProjectView: ...
```

### `record`

Accepts only the discriminated union `SignalEvent | UserDecision | ApprovalDecision`.

It validates schema, provenance, event-class authentication, replay identity, nonces, and one-shot approval scope. An identical replay returns the original receipt. Reusing an identity with different content returns `IDENTITY_CONFLICT`. A Signal Event cannot activate intent or approve an effect.

Canonical decision contracts:

```text
SignalEvent:
  project_id, source, source_event_id, payload_digest,
  observed_at, provenance, trust_class

UserDecision:
  project_id, source, source_event_id, authenticated_actor,
  scope, verbatim_text, nonce, replay_identity, provenance,
  decision_kind, complete_revision_payload

ApprovalDecision:
  project_id, source, source_event_id, authenticated_actor,
  scope, verbatim_text, nonce, replay_identity, provenance,
  action_envelope_digest, operation_digest, target_identity,
  target_sha_or_version, side_effect_class, spend_cap,
  expires_at, one_shot_identity
```

The bootstrap form `UserDecision(decision_kind='BOOTSTRAP_PROJECT')` atomically creates the Workflow Project, initial Constitution Revision, Goal Revision, Operating Profile Revision, and Active Intent. It is valid only when the Workflow Project does not exist. An identical replay returns the original receipt; a partial bootstrap or conflicting reuse fails closed.

### `advance`

Runs one bounded Pulse and progresses at most one lifecycle boundary. Cognitive planning, implementation, testing, and review are separate Actions with separate immutable envelopes and receipts.

1. Recover one unresolved Operation or Outbox attempt before planning new work.
2. Read current Active Intent, relevant Signal Events, and the current Portfolio projection.
3. If no Action is active, choose one Gap and applicable Matt method, select an eligible route, then freeze the Action Envelope and Handoff Package before dispatch.
4. If that Action needs external Matt or Worker execution, prepare and attempt one Operation, then return `PendingExternal`.
5. When a result Signal Event arrives, validate its Matt/Route/Test/Review receipt against the already-frozen envelope and record the classified result.
6. A later Pulse uses that result as evidence to authorize a distinct downstream Action and envelope.
7. If an effect requires user approval, first freeze the envelope and exact Operation as `AWAITING_APPROVAL` without attempting it. `record(ApprovalDecision)` may then bind to those digests. A later Pulse atomically validates and consumes the approval before changing the Operation to `PREPARED` and attempting it.

No receipt is validated before the envelope and route it attests to exist. No downstream phase inherits authority from an upstream receipt. The result is progress, `PendingExternal`, or a Legal Stop. The caller never chooses the Matt Skill, venue, model, retry, or transition.

### `view`

Returns only:

```text
current_goal
single_daily_brief
pending_decisions
```

It is read-only and performs no repair or external I/O.

## 3. Internal module design

### `WorkflowKernel`

Owns reconciliation, policy, lifecycle transitions, fencing, legal stops, recovery, and projections.

### `IntentAuthority`

Private implementation module. Validates complete immutable revisions, calculates Active Intent digests, activates revisions atomically, and decides deterministic compatibility.

### `ReceiptGate`

Private implementation module. Validates Matt, Route, Feng, Review, Approval, and Operation receipts against the same Intent Binding and Action Envelope.

### `ControlStore`

Private SQLite implementation. Owns migrations, transactions, leases, append-only records, and projections. It is not hidden behind a generic repository port; tests use real temporary SQLite databases.

### `ExternalEffects`

One genuine outbound seam:

```python
class ExternalEffects(Protocol):
    def attest(self, request: RouteRequest) -> RouteAttestation: ...
    def attempt(self, operation: FrozenOperation) -> AttemptOutcome: ...
    def observe(self, probe: EffectProbe) -> EffectObservation: ...
```

A private connector registry dispatches typed operation variants to GitHub, Kanban, Copilot, Feng, or delivery connectors. Provider-specific fields do not leak into the public kernel interface.

### Other justified seams

- `Clock`: system and deterministic adapters.
- `DecisionAuthenticator`: production identity verification and a test adapter.
- `ExternalEffects`: live and scripted adapters.

Do not create public goal, signal, router, journal, approval, or outbox modules.

## 4. SQLite control ledger

Use Python `sqlite3`, WAL, foreign keys, explicit numbered migrations, busy timeout, `synchronous=FULL`, and `BEGIN IMMEDIATE` for writer transactions.

Core identity/status fields are normal columns. Immutable structured payloads use canonical JSON plus SHA-256. Foreign keys, lifecycle state, and identities never live only inside JSON.

### Core tables

```text
projects
constitution_revisions
goal_revisions
operating_profile_revisions
active_intents
inbox_events
decision_nonces
approvals
evidence
portfolio_tasks
actions
action_envelopes
matt_invocations
matt_receipts
capability_snapshots
route_receipts
handoffs
attempts
test_evidence
review_receipts
operation_journal
operation_attempts
outbox_events
outbox_attempts
project_pulses
daily_briefs
```

### Required uniqueness

```text
UNIQUE(project_id, source, source_event_id)
UNIQUE(project_id, actor_id, nonce)
UNIQUE(project_id, one_shot_identity)
UNIQUE(project_id, goal_revision_number)
UNIQUE(action_envelope_digest)
UNIQUE(handoff_package_digest)
UNIQUE(operation_idempotency_key)
UNIQUE(outbox_event_id)
```

## 5. Active Intent and supersession

Every Action Envelope, Handoff Package, Matt Receipt, Route Receipt, Test Evidence, Review Receipt, Approval Decision, and Operation Record embeds:

```text
constitution_revision
goal_revision
operating_profile_revision
active_intent_digest
```

Changing any revision atomically creates a new Active Intent and invalidates old envelopes and transitive dependents by default. A compatibility rule may authorize a new envelope under the new binding; it never mutates the old envelope. Unknown impact fails closed.

Worker cancellation is best effort. Integration and operation fencing are authoritative.

## 6. Matt assurance

A deterministic classifier marks an Action as mechanical or cognitive. Unknown means cognitive.

Every cognitive Action requires a Matt Invocation containing Skill name and digest, trusted executor/run, input evidence digest, Skill gates, completion criterion, and expected artifact. The executor records actual Skill-load proof.

The resulting Matt Receipt binds the actual Skill digest, gate outcomes, artifact digest, Intent Binding, Action Envelope, route, completion classification, and allowed next methods. `ReceiptGate` validates it independently. Worker prose is never a receipt.

There is no global Matt state machine. Each Skill owns its steps and completion conditions; evidence selects the next applicable Skill.

## 7. Model and venue routing

A Capability Snapshot separates three claims for one observed venue candidate:

- control: requested provider/model/reasoning/context/tools can be set;
- attestation: actual values and parent/subagent identities can be proven;
- budget enforcement: hard, soft, external watchdog, or none.

The kernel validates immutable Capability Snapshots and projects accepted, unexpired candidates into the versioned Capability Matrix. The router selects one candidate from that Matrix and produces a non-authoritative Route Plan bound to the Matrix digest. Policy validates the plan against the Action, budget, ownership, and intent, then freezes its canonical content as the Route Envelope inside the Action Envelope. Later changes require a new Capability Snapshot, Matrix, Route Plan, and Action Envelope; none mutate the frozen route.

An exact route requires every dimension to be controllable and attestable, and its budget must be enforced by a hard cap or an explicitly approved, digest-bound external watchdog. `soft` and `none` budget enforcement are ineligible. Exact routes forbid fallback. A capability-class route selects only from a digest-pinned allowlist; every candidate has its own hard limits or explicitly approved watchdog and records the actual route.

The bootstrap Operating Profile payload is declared in [`../config/operating-profile.v1.json`](../config/operating-profile.v1.json). The file is an immutable bootstrap input, not active runtime policy and not a mutable cache. It becomes authoritative only when an authenticated bootstrap User Decision records its full payload as an Operating Profile Revision in the ledger. File presence or later edits never change Active Intent.

## 8. External effect transaction

Never hold SQLite locks across network or subprocess I/O. Receipts produced by an Action cannot be prerequisites for freezing that same Action.

### Freeze transaction

1. Acquire the project Pulse lease and fencing epoch.
2. Validate current intent, ownership, capability eligibility, budget availability, and receipts from completed prerequisite Actions.
3. Select the Matt method and eligible route.
4. Freeze the Action Envelope and Handoff Package.
5. Insert one Operation Record as `PREPARED`, or `AWAITING_APPROVAL` when the frozen operation requires user authorization.
6. Reserve the declared budget.

### Approval transaction

For `AWAITING_APPROVAL`, `record(ApprovalDecision)` authenticates the actor and binds the already-existing Action Envelope and Operation digests. A later `advance` transaction rechecks current intent and fencing, consumes the one-shot approval, and changes the Operation to `PREPARED`. No effect occurs before this transition.

### Attempt and observe

Call one prepared connector outside the transaction with a bounded deadline. Read back the target using its stable identity and expected version.

### Conclude transaction

Recheck fencing and Active Intent, then persist the actual Route Receipt and raw result evidence. Classify the effect as:

- `CONFIRMED_APPLIED`;
- `CONFIRMED_NOT_APPLIED`;
- `AMBIGUOUS`.

A result Signal Event may then carry the executor-produced Matt, Test, or Review Receipt. A later Pulse validates those receipts against the pre-existing envelope and operation before marking the Action complete or authorizing a downstream Action.

`AMBIGUOUS` is a legal stop. It is never blindly retried.

## 9. Handoff protocol

A Handoff Package is schema-versioned, content-addressed, attempt-bound, and includes:

- Intent Binding and Action Envelope digest;
- attempt, run, delivery, idempotency, fencing, executor, and expiry identities;
- exact base SHA, expected merge base, expected remote version, owned paths, and non-goals;
- Matt Invocation digest and Route Envelope;
- tool/side-effect policy, budget, deadline, test profile, and acceptance;
- stop and supersession conditions.

Lifecycle:

```text
OFFERED → ACCEPTED → RUNNING → RESULT_RECORDED → VERIFIED
```

Exceptional states are `REJECTED`, `BLOCKED_EXTERNAL`, `SUPERSEDED`, `FAILED`, and `AMBIGUOUS`. Duplicate delivery returns the prior receipt; a deliberate retry creates a new Attempt/Run.

## 10. Outbox and daily brief

Evidence and its logical Outbox Event are created in one transaction. Transport claims and delivers the event outside the transaction, then records the attempt and acknowledgement. Delivery may be at least once; the logical event identity is unique.

All internal Pulses for one day project into one Daily Brief. Delivery failure retries the same brief and never reruns workflow work.

## 11. Package layout

```text
projects/agentic-workflow/
├── CONTEXT.md
├── README.md
├── pyproject.toml
├── src/agentic_workflow/
│   ├── __init__.py
│   ├── kernel.py
│   ├── model.py
│   ├── policy.py
│   ├── receipts.py
│   ├── store.py
│   ├── effects.py
│   ├── cli.py
│   └── migrations/
├── tests/
├── scripts/
│   └── feng-test-runner.sh
└── docs/
    ├── product-intent.md
    ├── solution-design.md
    ├── spikes/
    └── adr/
```

The runtime package uses only the standard library in V1. Development uses pinned pytest and Ruff.

## 12. Proof Spikes before formal implementation

### Active-intent fencing

Prove with real SQLite transactions that activating a new Goal Revision prevents a stale Action from reserving or concluding an effect, while deterministic compatibility can create a new envelope.

### Requested/actual route receipt

Prove that an exact route rejects mismatched actual model/reasoning/subagent telemetry and that a capability-class route accepts only a pinned candidate.

### Feng exact-SHA receipt

Prove that Feng can execute a frozen test profile against one exact commit/tree and return environment identity, command, exit code, and artifact hashes; wrong-SHA evidence cannot satisfy the gate.

Each Spike has a predeclared `VALIDATED`, `PARTIAL`, `INVALIDATED`, or `INCONCLUSIVE` verdict and produces evidence, not production code.

## 13. V1 tracer slice

```text
Authenticated Goal Revision
→ GitHub issue Signal Event
→ Matt to-spec Invocation + validated Receipt
→ Agent-ready Portfolio task
→ Kanban Handoff
→ Matt implement/tdd Invocation
→ Copilot Cloud assignment + actual Route Receipt
→ validated implement/tdd Matt Receipt
→ GitHub PR exact SHA Signal Event
→ Feng exact-SHA test
→ Matt code-review Invocation
→ independent Spec + Standards Review Receipts
→ validated code-review Matt Receipt
→ Outcome Evidence
→ one Daily Brief Outbox Event
```

V1 does not merge or deploy automatically. It permits polling/manual callbacks before Webhook scaling. Every gate may return a legal stop.

## 14. Test seams

### Local lightweight tests

- Public `record`, `advance`, and `view` behavior.
- Pure canonicalization, compatibility, classification, and receipt policies.
- Real temporary SQLite databases with crash injection.
- Scripted `Clock`, `DecisionAuthenticator`, and `ExternalEffects` adapters.

### Feng authoritative tests

- Migration and concurrency tests.
- Crash-after-prepare and crash-after-effect recovery.
- Wrong-SHA, route drift, expired approval, forged Matt Receipt, duplicate delivery, and stale-intent scenarios.
- Connector contract and end-to-end tracer tests.
- Resource, container, browser, or integration-heavy suites.

A test for an old intent, SHA, route, or artifact may remain evidence but cannot satisfy the current gate.
