# ADR-0004: Journal every external effect and deliver through an outbox

- Status: Accepted
- Date: 2026-09-01

## Context

A process can crash after a remote write succeeds but before local acknowledgement. Blind retries can duplicate tasks, PRs, tests, merges, deployments, or messages. Holding a database transaction across a network call is unsafe.

## Decision

Journal every external effect before attempting it, observe the target afterward, and treat unresolved ambiguity as a legal stop rather than a retry instruction. Create logical notification events atomically with their evidence; every transport retry reuses the original Logical Outbox Identity and retries only delivery transport. The authoritative approval, operation, recovery, and outbox protocol lives in [`../solution-design.md`](../solution-design.md#8-external-effect-transaction).

## Consequences

- Crash recovery converges from durable intent plus target readback.
- Every external adapter must expose a stable target identity and observation strategy.
- Exactly-once physical delivery is not promised; one logical event may have multiple physical transport attempts.
- Logical Outbox Identity is unique, and transport retry never repeats the workflow work that produced the event.
- One-effect-per-advance bounds the blast radius and simplifies recovery.

## Alternatives considered

- Call external systems and then write local state: rejected because the crash window is unrecoverable without a journal.
- Wrap network calls in SQLite transactions: rejected because locks would span unbounded I/O.
