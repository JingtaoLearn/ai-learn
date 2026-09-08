# Product Portfolio State Contract

Use one per product. `STATE.md` is a compact mutable projection for the persistent Owner, not a task database or substitute decision brain.

## Concepts

- **Portfolio** — all durable outcome Workstreams contributing to the Goal.
- **Workstream** — one outcome lane that survives multiple Actions.
- **Action** — one bounded result assigned to one Role and task Session.
- **Ready set** — dependency-complete, safe candidate Actions.
- **Execution slot** — an available task Session plus a non-conflicting workspace/seam and eligible host.
- **Legal wait** — no positive-value legal Action remains, an external fact is genuinely unavailable, a Jingtao-owned decision is required, or the Goal is complete.

## Reconciliation

On each Signal, Result, Review, Decision, or recovery Pulse:

1. read Goal, effective Principles, current evidence, and live system state;
2. update each Workstream's outcome, evidence, Gap, state, health, Action, and wake;
3. explicitly identify missing user-visible effects;
4. build the Ready set and select the largest safe bounded Action set;
5. prefer decisive uncertainty reduction and user-visible validated effect over architecture completeness;
6. admit safe independent work across Role task Sessions;
7. validate Results from owning artifacts and systems;
8. replan from evidence rather than preserving backlog order;
9. record a Portfolio wait only after every non-Done lane lacks a positive-value legal Action.

Do not make fairness counters, fixed Issue order, or infrastructure activity more important than Goal progress. Add scheduling mechanics only after a real starvation or recovery problem is observed.

## States

- `ACTIVE` — a bounded Action is executing or under Review.
- `READY` — a safe next Action exists.
- `WAITING` — a real external observation or scheduled event is pending.
- `BLOCKED` — a dependency or authority prevents progress.
- `PARKED` — deliberately lower value than available alternatives; reconsider trigger required.
- `DONE` — Workstream outcome and acceptance are complete.

Health is separate: `ON_TRACK`, `AT_RISK`, `OFF_TRACK`, or `UNKNOWN`.

## Compact `STATE.md`

```markdown
# <Product> State

Verified at: <timestamp>
Fact sources: <owning handles>

## Product posture
- Current Goal outcome: ...
- Most important unmet user-visible effect: ...
- Portfolio health: ...

## Active Action set
- <Workstream / Action / Role / task Session / workspace / wake>

## Workstreams
| ID | Outcome | State / health | Evidence | Gap | Active or next Action | Wake |
|---|---|---|---|---|---|---|
| `WS-...` | ... | ... | ... | ... | ... | ... |

## Cross-Workstream constraints
- <shared seams, dependencies, host limits, Hard Boundaries>

## User-owned decisions
- <only substantive product/risk/authority choices>

## Recent validated effects
- <compact result handles>
```

## Authority split

- Goal owns destination.
- Default/Product/Role Principles own choice constraints.
- State owns current projection.
- Tracker owns product outcome acceptance and integration history.
- Kanban owns formal task lifecycle.
- Run artifacts own frozen Handoff/Result/Review evidence.
- The persistent Owner Session owns living product judgment.

Keep State concise. Do not paste full task logs, long Results, operating manuals, or historical incident recipes into it.
