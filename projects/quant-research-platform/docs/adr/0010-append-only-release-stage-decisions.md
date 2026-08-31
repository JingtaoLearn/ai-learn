# ADR-0010: Derive release stage from append-only exact-digest decisions

- Status: Accepted
- Date: 2026-08-31

## Context

A Model Release must remain immutable while its governance status advances from research to paper, production, and retirement. Storing stage as an editable property of the release would make the same release bytes acquire untraceable authority. Creating a replacement release for every stage would preserve immutability but fragment one reviewed artifact across identities and make approvals ambiguous.

## Decision

A Model Release freezes one canonical payload and Release Digest at creation and never changes. It begins at `EXPERIMENTAL`. Its effective Release Stage is derived from an append-only sequence of Promotion Approvals bound to that exact digest and to an explicit source and target stage.

Permitted transitions are `EXPERIMENTAL` to `PAPER_FROZEN`, `PAPER_FROZEN` to `PRODUCTION_FROZEN`, and `PRODUCTION_FROZEN` to terminal `RETIRED`. Each transition requires a separate decision; approvals cannot be reused, applied to another digest, backdated to repair an invalid runtime, or inferred from research evidence. In v1, native creation is eligible only from a Parameter Study-derived canonical successful Attempt carrying MetricDocumentFactory-issued evidence; a standalone Experiment or Attempt is not a native release source. Legacy import is a separate provenance path. Rollback changes the Active Deployment and never reverses a release stage. A retired release cannot be reactivated or unretired; restoring equivalent behavior requires a new Model Release and approvals.

## Consequences

Future readers can reconstruct who authorized every authority increase without trusting a mutable stage field. Qualifying Study lineage can make release creation eligible, but no Study, Experiment, Attempt, or passing holdout can advance stage. Promotion preview retains Study, Experiment, and Attempt lineage. Production activation must verify the complete approval chain against the exact release digest.

Retirement is deliberately irreversible. Historical deployments and signals remain attributable to the stage that was effective at their recorded time.

## Alternatives considered

A mutable stage column on Model Release was rejected because it destroys the audit trail and conflicts with release immutability. One release identity per stage was rejected because it duplicates identical payloads and weakens approval binding. Allowing direct experimental-to-production approval was rejected because it makes the paper review gate optional.
