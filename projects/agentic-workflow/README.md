# Agentic Workflow

A goal-driven, Matt-native workflow kernel that continuously reconciles user-owned intent with evidence, routes bounded work to capable execution venues, and independently verifies results without requiring the user to manage engineering execution.

## Status

V1 is in the proof-Spike and specification phase. Only Replay and Shadow are eligible; automatic merge and deployment are disabled.

## User surface

The public product surface is intentionally small:

```text
record(event) -> receipt
advance(project) -> progress | legal stop
view(project) -> current goal + daily brief + pending decisions
```

## Architecture

- One deep Python `WorkflowKernel`.
- One SQLite control ledger for intent, envelopes, receipts, operations, and outbox state.
- One outbound `ExternalEffects` seam for GitHub, Kanban, Copilot, Feng, and delivery adapters.
- Every authoritative artifact binds immutable Active Intent.
- Every cognitive action requires executor-attested Matt method evidence.
- Feng is the current authoritative venue for required and heavy tests.

## Documentation

- [Product intent](docs/product-intent.md)
- [Solution design](docs/solution-design.md)
- [Domain glossary](CONTEXT.md)
- [Architecture decisions](docs/adr/)
- [Proof Spikes](docs/spikes/)
- [Bootstrap Operating Profile](config/operating-profile.v1.json)

## Development process

Follow the configured Matt engineering flow:

```text
domain-modeling / codebase-design
→ proof Spikes
→ to-spec
→ to-tickets
→ implement with TDD
→ Spec review
→ Standards review
→ Feng verification
```

This sequence describes the current build handoff, not a global runtime state machine. Runtime method selection remains evidence-driven.

## Runtime and deployment

The V1 package will use Python 3.12 and the standard library for runtime behavior. No network service, Docker image, or production deployment exists yet. Deployment artifacts will be added only after the Replay/Shadow implementation proves a need and passes its gates.

## Safety boundary

- No Hermes core modification.
- No unbounded shared model Session.
- No automatic goal inference.
- No heavy required tests on the local Hermes host.
- No automatic merge, deploy, paid, public, or irreversible effect in V1.
