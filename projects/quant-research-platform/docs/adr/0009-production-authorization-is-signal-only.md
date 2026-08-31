# ADR-0009: Production authorization remains signal-only

- Status: Accepted
- Date: 2026-08-31

## Context

The platform has been research-only and deliberately has no broker path. Issue #189 introduces production deployments because reviewed models already produce operational BOCOM and Au99.99 recommendations. Calling these deployments “production” can misleadingly imply authority to trade, while forbidding any operational use would leave the existing external signal scripts outside the audited platform.

## Decision

A production Deployment authorizes Signal Production only. It may compute and deliver a recommendation or target position from completed market information, but it must not possess broker credentials or create, route, submit, simulate, or reconcile orders or fills. `PRODUCTION_FROZEN` means approved for an operational signal channel, not approved for paper or live trading.

The release and runtime contracts must retain an explicit no-automatic-ordering assertion. Any future order-producing capability requires a separate domain and a new architecture decision; it cannot be added as another Deployment Channel or runtime adapter under this decision.

## Consequences

The platform can replace the external BOCOM and Au99.99 signal controls without weakening the established no-broker boundary. API and UI language must say “signal” or “recommendation,” never imply execution, and fail closed if a release or runtime contract claims automatic ordering. Confirmation and one logical delivery event are transactional platform facts; delivery adapters provide at-least-once external delivery and may duplicate after a crash.

The platform does not model broker accounts, orders, fills, positions held at a broker, execution reconciliation, or trading permissions. A human remains responsible for any action taken from a signal.

## Alternatives considered

Keeping all production signal generation outside the platform was rejected because it preserves a second unaudited control plane. Adding paper or live order execution together with promotion was rejected because it collapses research governance and trading authority into one release and materially expands safety, credential, and regulatory scope.
