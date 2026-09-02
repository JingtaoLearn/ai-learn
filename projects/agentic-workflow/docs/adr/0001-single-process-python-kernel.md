# ADR-0001: Use a single-process Python kernel with a SQLite control ledger

- Status: Accepted
- Date: 2026-09-01

## Context

The workflow needs atomic goal revision activation, fencing, one-shot approval consumption, operation recovery, budget reservation, and an outbox. GitHub, Hermes Kanban, and Cron remain authoritative for their own facts but cannot provide one transaction across this protocol. A general workflow engine would add infrastructure before the first proven vertical slice.

## Decision

Build `projects/agentic-workflow` as a single-process Python package with one authoritative local control ledger. Do not modify Hermes core or introduce a general workflow engine. The concrete SQLite and transaction protocol is owned by [`../solution-design.md`](../solution-design.md#4-sqlite-control-ledger).

## Consequences

- Restart safety and protocol invariants live in one local transaction domain.
- V1 is single-host and single-writer per project, not highly available.
- Required runtime dependencies can remain in the Python standard library.
- Supervision, backup, integrity checks, and schema migration become operational requirements.
- Remote I/O never occurs while a SQLite transaction is open.

## Alternatives considered

- GitHub/Kanban/Cron as the only ledger: useful for Replay and Shadow, but insufficient for one-shot approvals, fencing, ambiguous external effects, aggregate budgets, and enforcing a unique Logical Outbox Identity.
- Temporal or another workflow engine: powerful, but premature for the bounded first implementation.
- TypeScript modular monolith: no existing project or admitted runtime justified the extra toolchain; Python and pytest are already proven on Feng.
