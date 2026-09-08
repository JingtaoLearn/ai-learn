# Formal Handoff Template

## Goal and Gap

- Goal reference: `<path and revision>`
- Workstream: `<stable lane ID>`
- Gap: `<one missing result that prevents useful progress>`


## Principle Context

- Hard Boundaries: `<path>` / SHA-256 `<digest>`
- Product authorization: `<file path + SHA-256, or provider/message/event ID + immutable payload digest>`
- Principle Resolution: `<path>` / SHA-256 `<digest>`
- Default Principles: `<path>` / SHA-256 `<digest>`
- Product Principles: `<path>` / SHA-256 `<digest>`
- Role Principles: `<path>` / SHA-256 `<digest>`
- Authorized exception reference: `none` or `<explicit higher-authority decision>`
- External Handoff identity: `<Kanban attachment digest or run-manifest path containing this file's SHA-256>`

The worker verifies every listed identity, including the external Handoff digest, before acting. A mismatch returns `PRINCIPLE_CONTEXT_STALE`; an unresolved contradiction returns `PRINCIPLE_CONTEXT_CONFLICT`.

## Evidence

- `<Observed fact — owning source>`

## Action

`<One bounded outcome, not a sequence of speculative stages>`

## Agent and execution

- Role/Profile: `<one canonical Role Profile>`
- Task Session: `<isolated Session>`
- Workspace/run: `<exact path and run identity>`
- Immutable base/input: `<identity>`
- Read set: `<inputs>`
- Write set: `<owned paths/resources>`
- Shared seams: `<conflict domains>`
- Host/resource class: `<light control host or admitted remote host>`

## Method

- Selected Skill: `<one applicable method or none>`
- Why: `<how it reduces this Gap>`

## Validation

- Observable user/product effect: `<what the user or owning system can actually observe>`
- Minimum evidence: `<smallest check/read-back that can falsify the claim>`
- Critical checks: `<only critical regression or Hard Boundary checks, or none>`

## Safety

- Allowed effects: `<bounded effects>`
- Prohibited effects: `<hard boundaries>`
- Preserve: `<Goal, Principles, data, and production semantics>`

## Return

- Required artifact: `<RESULT/REVIEW path>`
- Owner Profile/Session: `<exact destination>`
- Correlation/idempotency: `<stable identities>`
- Record the Hard Boundary, Resolution, Default, Product, Role, and Handoff revisions, any authorized exception, and Validation evidence.
