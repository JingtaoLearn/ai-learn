# ADR-0004: Separate holdout access, outcome, and freshness

- Status: Accepted
- Date: 2026-08-28

## Context

A single holdout status conflates whether data was accessed, whether the candidate passed, and whether platform history records earlier exposure. Those statements have different evidence and change independently.

## Decision

The Holdout Ledger records access as `SEALED`, `GRANTED`, or `ACCESSED`. Holdout outcome is separately `NOT_RUN`, `PASSED`, or `FAILED`. Freshness is separately `NO_RECORDED_PLATFORM_EXPOSURE`, `PREVIOUSLY_EXPOSED`, or `LEGACY_UNKNOWN` and is derived from append-only exposure history.

## Consequences

Reports can state exactly what is known without claiming global novelty. Legacy uncertainty cannot be promoted to fresh evidence.

## Alternatives considered

A combined holdout enum and a `globally_pristine` flag were rejected because they overstate evidence and create invalid state combinations.
