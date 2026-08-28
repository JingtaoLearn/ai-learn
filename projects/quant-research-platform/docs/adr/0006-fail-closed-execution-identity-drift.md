# ADR-0006: Fail closed on Study execution-identity drift

- Status: Accepted
- Date: 2026-08-28

## Context

A long-running Study can span deployments. Silently switching source, dependencies, metric semantics, or runner image would combine incomparable Trial evidence under one Study identity.

## Decision

The Frozen Study Plan stores a canonical deep copy of Execution Identity. Before every new effect, `ParameterStudy` compares the current content identity with the frozen value. Drift records one fail-closed event and prevents new Trial, Experiment, rerun, or holdout effects. Recovery requires an explicit lineage-linked fork on the new release; a filesystem release path is only a locator.

## Consequences

Old Studies remain auditable but cannot silently continue on new code. Deploy tooling must detect non-terminal Studies and default to refusing unsafe release switching.

## Alternatives considered

Path-based identity and automatic cross-release continuation were rejected because neither proves behavior equivalence.
