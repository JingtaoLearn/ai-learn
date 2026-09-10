# ADR-0014: Execute new training Studies through a lightweight kernel

- Status: Accepted
- Date: 2026-09-10

## Context

ADR-0001 reused Experiment and Attempt execution for every Parameter Study candidate because a second durable trial state machine would have split recovery truth. Deployed evidence now shows that this makes internal search iterations authoritative product records and amplifies hundreds of training iterations into Experiments, Attempts, logs, reports, and trial rows. The product now distinguishes an interactive Attempt from a lightweight training Study.

## Decision

New lightweight training Studies use one versioned Study Training Kernel task identity. Zhlearn remains the sole control, lifecycle, and result authority; Feng executes the frozen kernel and keeps only one bounded restart document. Internal suggestions, objective values, and search history remain transient and never become Experiment, Attempt, report, or per-trial PostgreSQL rows. Zhlearn persists only the frozen request, dispatch lifecycle, latest bounded aggregate progress, and final aggregate evidence or explicit failure.

ADR-0001 remains the interpretation of existing legacy Parameter Study history and its unchanged legacy creation path. There is no migration, rewrite, dual-write, or silent cutover. A new caller must select the lightweight kernel contract explicitly until the product UI/controller adopts it; newly admitted lightweight jobs never fall back to the legacy Experiment/Attempt path.

## Consequences

One Study identity can represent many internal iterations without crowding Experiment and Attempt history. Authenticated idempotent dispatch and deterministic restart replace per-iteration durable recovery. A worker restart replays the frozen deterministic kernel from the beginning and overwrites only bounded aggregate checkpoints; it does not recover or publish internal trial history. Feng refusal or unavailability is explicit and never invokes local computation on zhlearn.

Existing legacy Study evidence remains readable and immutable. Switching the existing Study UI/controller is deliberately deferred until it can select this contract without changing research meaning.

## Alternatives considered

Continuing Experiment/Attempt reuse was rejected because measured write amplification contradicts the lightweight Study product boundary. Persisting every kernel trial or an Optuna database was rejected because it recreates a second authoritative trial ledger. Migrating or rewriting legacy Studies was rejected because accepted evidence is append-only in meaning.