# Spike 001: Active Intent Fencing

- Status: `VALIDATED`
- Type: Logic prototype
- Prototype branch: `prototype/agentic-intent-fencing`
- Prototype commit: `66b84cefdbc98a8eb4a9b1e3760642b162cbf250`

## Question

Can one real SQLite control ledger atomically activate a new Goal Revision and prevent an Action bound to the old Active Intent from reserving or concluding an external effect, while allowing explicitly compatible work to continue only through a new envelope?

## Cheapest valid prototype

Use Python `sqlite3`, a temporary database, WAL, foreign keys, and `BEGIN IMMEDIATE`. Model one project, two Goal Revisions, two Active Intents, one old Action Envelope, and an operation reservation/conclusion path. Inject concurrent/stale attempts around activation.

## Predeclared verdicts

- `VALIDATED`: every stale reservation and conclusion is rejected; unknown compatibility fails closed; deterministic compatibility can create a new envelope under the new binding; concurrent writers preserve one active pointer and append-only history.
- `PARTIAL`: serial fencing works but a documented concurrency or conclusion window remains.
- `INVALIDATED`: stale authority can reserve or conclude an effect, or compatibility reuses/mutates the old envelope.
- `INCONCLUSIVE`: the prototype cannot exercise the real SQLite transaction behavior.

## Evidence required

- schema and transaction trace;
- deterministic concurrent test;
- stale reservation and stale conclusion outcomes;
- compatibility re-envelope outcome;
- Feng execution receipt with exact source hash and exit code.

## Verdict evidence

The prototype uses real Python `sqlite3`, WAL, foreign keys, `BEGIN IMMEDIATE`, process-level concurrency, append-only revisions/events, and crash injection. An independent fresh Hermes review first found that append-only triggers lacked a sensitive direct test. The test was added, a mutation probe proved trigger removal is detected, and final independent review returned `PASS`.

A Git bundle containing exact commit `66b84cefdbc98a8eb4a9b1e3760642b162cbf250` was cloned into a new Feng `/tmp` workspace and executed with system Python. All `10` unittest cases passed, including direct update/delete rejection on every authoritative append-only table, deterministic concurrent revision writers, stale reservation, stale conclusion, unknown/incompatible compatibility rejection, compatible re-enveloping, append-only operation events, and two process-crash rollback windows. The temporary Feng workspace and bundle were removed after execution.

The observed behavior meets every `VALIDATED` criterion. The production implementation must re-derive the minimal transaction behavior through the public kernel seam rather than copy the prototype wholesale.

## Boundary

This validates the transaction model only. It does not authorize external writes, merge, deployment, or reuse of prototype code as production implementation.
