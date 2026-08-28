# ADR-0008: Keep adaptive-search truth in the platform journal

- Status: Accepted
- Date: 2026-08-28

## Context

Optuna can persist sampler Studies and Trials, while the platform already owns immutable Study, Trial, Experiment, Attempt, and evidence identities. Letting both stores become authoritative would create split-brain recovery, unclear deduplication, and unverifiable proposal histories.

## Decision

The platform persists an immutable ordered Suggestion Journal before and after every adaptive boundary. Optuna is a version-frozen, in-process ask/tell adapter reconstructed from that journal. Only canonical same-round inner-fold evaluations may be replayed as tell values. Optuna storage is never an authoritative platform dependency.

## Consequences

Crash recovery can replay and verify every proposal without trusting a mutable third-party database. Each outer round and final round has an independent adaptive history. The adapter and exact Optuna version become Frozen Study Plan identity. Upgrading Optuna, sampler settings, distributions, or budget forks a Study. Replay incompatibility fails closed rather than silently changing candidates.

## Alternatives considered

Using Optuna RDBStorage as the platform source of truth was rejected because it duplicates Trial and execution ownership. Persisting opaque sampler objects was rejected because pickles are unsafe, version-fragile, and not independently auditable. Batch-generating all TPE candidates before any evaluation was rejected because it is not adaptive ask/tell optimization.