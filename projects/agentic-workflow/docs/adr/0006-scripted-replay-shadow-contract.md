# ADR-0006: Bound V1 to a scripted Replay and Shadow tracer

- Status: Accepted
- Date: 2026-09-02

## Context

The journal and outbox design must prove crash recovery and one daily synchronization before the workflow is allowed to perform real autonomous effects. Active Intent supersession also needs a precise correctness rule: revoking authority is possible locally, while stopping a request that has reached an arbitrary remote target may not be. Treating those as the same promise would contradict both best-effort cancellation and the absence of exactly-once physical delivery.

## Decision

V1 is one Replay and Shadow tracer with scripted effects and scripted delivery transport. Replay performs no effects. Shadow may propose an Operation and may execute the deterministic scripted attempt/readback protocol, but it cannot authorize physical apply. One active writer Pulse per Workflow Project serializes protocol changes; overlapping authority transitions stop or wait rather than race.

When a new Active Intent supersedes an Operation, use Drain and Reconcile. Authorization not yet attempted is invalidated. A logically in-flight scripted Operation is observed by stable-target readback and concluded under its original Intent Binding. Its result remains Evidence but the Integration Fence prevents it from satisfying the new Active Intent. Physical Cancellation is best-effort cleanup, not the correctness mechanism.

At Asia/Shanghai day close, commit one immutable Daily Brief and one Outbox Event with one Logical Outbox Identity for each Workflow Project and closed local date. Transport retries reuse that identity and never rerun workflow work. Migration accepts every valid state of the closed v6 ledger, including `RESERVED` and `RESERVED` followed by `CONCLUDED`, and rejects malformed history.

The frozen acceptance matrix is:

- E1: the exact Operation and Approval lifecycle through `record`, `advance`, and `view`;
- E2: journal, one scripted attempt, stable-target readback, deterministic `APPLIED`, `NOT_APPLIED`, or `AMBIGUOUS` conclusion, and crash recovery without blind retry;
- E3: atomic Evidence plus one logical day-close Outbox Event with transport-only retry;
- E4: valid v6 migration plus one Replay/Shadow goal-to-brief tracer.

Target-scoped global physical fencing, connector compare-and-fence, hard cancellation, Goal Revision and physical-request concurrency, Outbox physical-send revocation, `revision_transitions` and migration 0008, real autonomous effects, automatic merge, and deployment are Execute-mode work.

## Consequences

- V1 proves logical recovery and authorization semantics without claiming control over real remote side effects.
- The single-writer rule removes high-availability and multi-owner races from this proof.
- Old-Intent outcomes remain auditable without gaining current authority.
- One daily synchronization has deterministic calendar and logical identities even though future physical delivery may be at least once.
- Superseded, unshipped experimental schemas are not migration inputs.

## Alternatives considered

- Prove real connector writes and physical-send fencing in V1: rejected because it turns the tracer into a generic Execute engine before the logical protocol is proven.
- Treat Active Intent change as guaranteed hard cancellation: rejected because remote systems cannot provide that universal guarantee.
- Discard an in-flight old-Intent result: rejected because it destroys Evidence and leaves the crash window unresolved.
- Emit a new Outbox Event for each retry: rejected because transport failure would manufacture new notification intent.
