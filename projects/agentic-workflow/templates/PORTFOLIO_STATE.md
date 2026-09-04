# Product Portfolio State Contract

Use one instance per product. This is a compact mutable projection for the persistent Product Owner, not a replacement backlog, task database, or workflow runtime.

## Concepts

- **Portfolio** — all durable Workstreams contributing to the Product Goal.
- **Workstream** — one outcome lane that survives multiple sequential Actions.
- **Action** — one bounded executable outcome, normally represented by one Kanban card and one run directory.
- **Ready set** — Workstreams with a dependency-complete, safe next Action.
- **Role pool** — zero to three isolated Profile instances for one concrete Specialist role; Product Owner is singleton.
- **Execution slot** — an unoccupied role-pool Profile plus a non-overlapping workspace/seam lease and an eligible live execution host. There is no static cross-role global cap.
- **Safety/stage-gate class** — the tuple of authorization boundary, irreversible-risk tier, and next unmet product gate used only to compare fairness among genuinely substitutable ready Actions. Actions in different classes are not peers for oldest-ready selection.

## Reconciliation

On every Signal, Result, Review, Decision, or recovery Pulse:

1. Reconcile every non-Done Workstream from live tracker, Kanban, process, production, and evidence facts.
2. Update lane state, health, Evidence, Gap, current/next Action, dependencies, and wake condition.
3. Build the Ready set and fill every safe independent execution slot.
4. Enforce slots `01..03` per concrete Specialist role, one process per Profile, one writer per physical workspace, one lease holder per mutable semantic seam, one Integrator per target branch, native dependencies, stable idempotency/fencing, and live host capability.
5. Rank ready Actions by irreversible safety/correctness risk, stage-gate leverage, user value, evidence age, and starvation.
6. Let blocked or waiting lanes coexist with progress in unrelated ready lanes.
7. Set `ready_since` only when a lane first becomes READY; preserve it across compatible skips. Increment `skipped_slot_releases` only when a verified compatible slot is released and assigned to a peer Action in the same safety/stage-gate class. Reset both fields when the lane leaves READY, its Action or class materially changes, or it is staffed. Within one class, select the oldest `ready_since` first. After three compatible skips, staff the lane at the next compatible release unless evidence proves it is no longer ready. Reclassification requires that changed evidence and an exact reconsider trigger; parking is never a fairness escape hatch.
8. Record `CAPACITY_SATURATED` only when every prerequisite is verified and the compatible Profile/workspace/host slot is observably occupied, with the exact releasing task/process as the wake condition. A missing, unavailable, or unverified Profile, lease, dependency, budget, or safety fact makes the lane `BLOCKED` with health `UNKNOWN` and its own evidence-acquisition wake condition, not capacity saturation.
9. Enter a Portfolio-level wait only when every non-Done lane is Active, Waiting, Blocked, deliberately Parked with a reconsider trigger, or has no positive-value bounded Action.

## State and health

State:

- `ACTIVE` — an Action is executing or under review.
- `READY` — a safe dependency-complete Action exists but is not staffed.
- `WAITING` — external evidence or a scheduled event is pending.
- `BLOCKED` — a dependency or authorization prevents progress.
- `PARKED` — deliberately unstaffed; a reconsider trigger is required.
- `DONE` — the Workstream outcome and acceptance are complete.

Health is independent: `ON_TRACK`, `AT_RISK`, `OFF_TRACK`, or `UNKNOWN`.

## `STATE.md` shape

```markdown
# <Product> State

Verified at: <timestamp>
Fact sources: <tracker, board, process, production, artifact handles>

## Product posture
- Phase: ...
- Destination: ...
- Portfolio health: ...

## Execution capacity
- Host/token budget: ...
- Single-writer constraints: ...
- Occupied slots: ...
- Available compatible slots: ...

## Workstream portfolio
| ID | Outcome | State / health | Active or next Action | Ready age / skips |
|---|---|---|---|
| `WS-...` | ... | `ACTIVE / ON_TRACK` | task/Agent/workspace or next candidate | `ready_since`; `skipped_slot_releases` |

## Non-Done Workstream detail
### `WS-...`
- Verified: <timestamp and fact-source handles>
- Evidence: ...
- Gap: ...
- Current or next Action: ...
- Task / role pool / Profile slot / workspace / execution host: ...
- Dependencies: ...
- Shared-seam constraints: <interfaces, schemas, files, or leases this lane shares with named Workstreams>
- Wake: ...
- Ready since / skipped compatible slot releases: ...

## Ready-set policy
<Which lanes must be assessed when capacity opens; include the concrete aging rule and never prescribe a fixed issue order.>

## Cross-Workstream constraints
<Shared seams, native dependencies, workspace conflicts, production isolation.>

## User-owned decisions
<Only irreducible product/risk/authorization choices.>

## Recently completed checkpoints
<Compact handles only.>
```

## Authority split

- Goal/Principles own destination and constraints.
- `STATE.md` owns the current Portfolio projection.
- GitHub Issues/PRs own outcome acceptance and integration history.
- Kanban owns executable tasks, dependencies, assignees, attempts, review, and blockers.
- `runs/<id>/` owns immutable Handoff/Result/Correction/Decision evidence.
- The persistent Owner Session owns living product judgment.

Do not copy complete task comments, Results, or Issue specifications into `STATE.md`. Volatile claims carry a verification time and source handle.
