# Agentic Workflow Product Intent

- Status: Accepted for V1 implementation
- Decision authority: Jingtao
- Product and engineering owner: Hermes
- First consumer: Quant Research Decision System

## Outcome

Build a continuously operating Agentic Workflow that advances a user-owned goal without requiring the user to manage task decomposition, design, implementation, tests, pull requests, deployment, or recovery.

The user provides constraints, rules, the ultimate goal, and explicit revisions. Hermes preserves that authority, continuously ingests signals, replans from evidence, applies the appropriate Matt method, routes bounded work to capable execution venues, and independently verifies results.

## User-facing surface

The user interacts with only:

1. the current goal and constraints;
2. one primary daily synchronization;
3. bounded decision or approval requests that cannot be resolved safely through evidence, research, or reversible prototypes.

All Issue, Agent, model, test, PR, deployment, and recovery mechanics remain internal.

## Product principles

- User decisions are authoritative; model inference and observed behavior may propose but never silently activate a goal change.
- Goal, evidence, action, review, and operation history remains traceable and is never rewritten.
- Every cognitive planning and delivery action follows a verified applicable Matt method.
- Model choice, execution venue, and Session continuity are tactics, never authority.
- Sessions are disposable reasoning contexts; durable records and external evidence provide continuity.
- A negative, invalidated, superseded, blocked, or no-action result is legitimate when supported by evidence.
- External, irreversible, paid, public, or production-signal effects require their declared exact authorization.

## Success

V1 succeeds when one complete goal-to-evidence path can be replayed and shadowed through intent revision, Matt-governed planning and development, bounded execution, Feng exact-source verification, independent review, and one daily brief without stale authority, duplicate logical effects, or user management of engineering details.

## V1 boundary

V1 does not automatically merge or deploy. Replay and Shadow remain the only enabled modes until the proof Spikes and implementation gates pass.

## Non-goals

- One endlessly growing Hermes or Copilot Session.
- A fixed global sequence of Matt Skills.
- Automatic goal changes inferred from usage.
- Treating Worker prose, CI liveness, process health, or message delivery as outcome evidence by itself.
- Heavy required testing on the local Hermes host.
- Modifying Hermes core before a proven external seam requires it.
- Automatic irreversible, paid, public, or production-signal actions without exact authorization.

## References

- Concrete architecture and protocol: [`solution-design.md`](solution-design.md)
- Bootstrap payload for the initial provisional execution policy: [`../config/operating-profile.v1.json`](../config/operating-profile.v1.json). File presence does not activate policy; the ledger-owned Operating Profile Revision is authoritative after authenticated bootstrap.
- Domain vocabulary: [`../CONTEXT.md`](../CONTEXT.md)
- Hard-to-reverse decisions: [`adr/`](adr/)
