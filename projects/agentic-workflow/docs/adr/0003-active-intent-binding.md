# ADR-0003: Bind every authoritative artifact to immutable active intent

- Status: Accepted
- Date: 2026-09-01

## Context

The user may revise the goal or temporary operating preferences while Agents, reviews, and external operations are running. Mutating old records or relying on conversation context would allow stale work to integrate under a new goal.

## Decision

Bind every authoritative artifact to one immutable Active Intent and invalidate old authority by default when any revision changes. Compatibility can create new authority but never mutate old authority. The authoritative binding and supersession protocol lives in [`../solution-design.md`](../solution-design.md#5-active-intent-and-supersession).

## Consequences

- Historical evidence remains reusable without inheriting stale authority.
- Every completion claim can be traced to the exact intent that authorized it.
- Revision changes can cancel more work than strictly necessary; compatibility rules must be narrow and tested.
- Best-effort worker cancellation is operational cleanup, while integration fencing is the authoritative control.

## Alternatives considered

- Mutate in-flight envelopes: rejected because it destroys provenance.
- Allow tasks believed to be unaffected to continue under the old binding: rejected because belief is not an authorization boundary.
