# ADR-0005: Treat search-budget changes as Study forks

- Status: Accepted
- Date: 2026-08-28

## Context

Increasing a candidate budget, narrowing a range after seeing results, or changing evaluation choices alters the pre-registered research protocol and may alter deterministic proposal history.

## Decision

A frozen Study is immutable. Budget, search-space, or evaluation changes create a new Study linked through Study Lineage. The new Study may reuse valid Experiments and evidence, but it starts its own proposal trajectory and counts prior inspected configurations.

## Consequences

The system does not misrepresent post-hoc continuation as an unchanged protocol. A future version may define an explicit tranche-continuation protocol, but v1 does not.

## Alternatives considered

In-place budget mutation was rejected because it destroys protocol identity. Pretending a fork continues the original random or adaptive sequence was rejected because exact recovery is not proven.
