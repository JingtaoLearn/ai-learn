# ADR-0002: Expose one deep kernel and one outbound effect seam

- Status: Accepted
- Date: 2026-09-01

## Context

The workflow contains intent, signals, Matt methods, routing, handoffs, reviews, operations, and delivery. Exposing each concept as a public module would make callers understand nearly the entire implementation and scatter policy across adapters.

## Decision

Expose one deep `WorkflowKernel` and one genuine outbound `ExternalEffects` seam. Keep policy-bearing concepts inside the kernel and connector-specific types behind the seam. The authoritative signatures and internal module shape live in [`../solution-design.md`](../solution-design.md#2-public-interface).

Use additional internal seams only where two real adapters vary: `Clock`, `DecisionAuthenticator`, and live versus scripted `ExternalEffects`.

## Consequences

- Callers learn three operations rather than the whole protocol.
- Tests exercise the same high seam as production callers.
- SQLite is tested directly with temporary databases instead of a generic repository interface.
- Connector details cannot leak into the kernel's external interface.

## Alternatives considered

- Separate public goal, signal, router, journal, approval, and outbox modules: rejected as shallow modules.
- One adapter interface per external provider: retained only as a private connector registry behind the single outbound seam.
