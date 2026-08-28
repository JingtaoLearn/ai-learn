# ADR-0002: Bind physical Execution Dataset Slices into Experiment identity

- Status: Accepted
- Date: 2026-08-28

## Context

A causal operator interface does not prove that untrusted code could not read future rows mounted in the execution environment. Full historical snapshots also cannot distinguish two runs with different visibility limits.

## Decision

Every validated inner, outer, and holdout run uses an immutable physical Execution Dataset Slice. Its derived-view lineage, parent identity, Fold Window, projection identity, projected bytes digest, scoring mask, and access-boundary digest are committed by the derived snapshot identity consumed by the existing Experiment task.

## Consequences

Validated Studies cannot reuse legacy full-snapshot Experiments that lack access-boundary evidence. Warm-up rows may be readable but are excluded from scoring. The parent snapshot is never mounted into the Study run.

## Alternatives considered

Interface conventions and read-time row filtering were rejected because they do not enforce physical future-data isolation.
