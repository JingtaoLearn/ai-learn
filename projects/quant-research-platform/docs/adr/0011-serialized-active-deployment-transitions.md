# ADR-0011: Serialize active deployment changes per channel

- Status: Accepted
- Date: 2026-08-31

## Context

Schedulers need one unambiguous deployment identity, while operators need replacement and rollback without editing immutable Deployment records. A mutable file pointer can be torn, overwritten, or changed concurrently; deriving “active” from the newest deployment or approval would reintroduce forbidden implicit promotion. Treating paper and production as separate channels would also permit two simultaneous predictors for the same asset and purpose without explicit ensemble semantics.

## Decision

A Deployment Channel is identified only by asset and signal purpose. Environment is an immutable Deployment attribute. Every activation, replacement, deactivation, and Rollback is one serialized, audited transition scoped to that cross-environment channel. The transition validates an expected current Deployment, verifies the target’s exact release and environment-appropriate approval chain, records the transition, and changes the channel's Active Deployment atomically. At most one Deployment is active across all environments for a channel. There is no “newest wins” or fallback selection.

Deployment records and activation history are immutable. Rollback selects an existing prior Deployment through the same activation operation as any replacement. A missing, stale, retired, mismatched, or concurrently changed target causes no active-state change. Ensemble channels are unsupported until a separate contract defines their members, aggregation, and failure semantics.

## Consequences

A scheduler can read one authoritative Deployment identity and fail closed rather than choosing among candidates. Concurrent paper and production activations race on the same generation, so exactly one can commit. Rollback is fully attributable without mutating either Deployment. The active selection is a transactional projection over immutable history, so both current lookup and historical reconstruction are available.

Activation availability depends on the catalog transaction. Runtime consumers must not cache or infer a replacement from release creation, stage approval, creation time, or semantic version.

## Alternatives considered

Selecting the latest production-approved release was rejected because approval is not activation and “latest” is unsafe after release creation. Editing a deployment in place was rejected because it erases what prior scheduler runs consumed. Allowing multiple active deployments was rejected because v1 has no explicit ensemble contract or deterministic conflict rule.
