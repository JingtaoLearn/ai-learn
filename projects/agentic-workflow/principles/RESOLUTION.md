# Principle Resolution Contract

## Effective stack

Every Action resolves one effective Principle Context:

1. **Hard Boundaries** — [`HARD_BOUNDARIES.md`](HARD_BOUNDARIES.md), stricter system/platform policy, and product authorization sources; never overridable.
2. **Default Principles** — shared engineering judgment for every product and Role.
3. **Product Principles** — product-specific value, evidence, and business boundaries owned by Jingtao.
4. **Role Principles** — the professional judgment and refusal conditions of the selected Role.
5. **Task Constraints** — the current Action's scope, acceptance, safety, and explicit exceptions.

A lower layer may specialize or narrow a higher layer. It may not silently weaken or contradict it. An explicit product override may replace a Default Principle for one product, but the override must name the replaced principle, reason, owner, and effective revision. Hard Boundaries remain unchanged.

## Authority

- Jingtao owns the Product Goal, Product Principles, material risk posture, and substantive authorization boundaries.
- `AgenticWorkflow-Assistant` maintains Default and Role Principle templates without taking product ownership.
- A Product Owner resolves the effective stack, selects Actions, and raises conflicts; it does not silently rewrite any Principle source.
- Specialists act independently inside the resolved context and return conflicts, missing authority, and unverifiable claims as evidence.
- Reviewers use the same frozen Principle Context and do not treat the Owner's preferred solution as evidence.

## What belongs in a Principle

A Principle is a durable rule that changes how an Agent chooses among multiple otherwise legal actions.

Do include:

- value and evidence priorities;
- conflict resolution;
- professional judgment and refusal conditions;
- stable product boundaries.

Do not include:

- Session IDs, host names, Issue numbers, current task status, or callback addresses;
- command sequences, transport steps, retries, workspace paths, or deployment procedures;
- one incident's corrective recipe;
- facts that belong in State, Evidence, or a Handoff.

## Version binding

A formal Handoff records the path and SHA-256 of the Hard Boundary file, this Resolution Contract, and the exact Default, Product, and Role files used to create it. Every authorization source carries an immutable identity: file path plus SHA-256, or provider/message/event identifier plus immutable payload digest. The Handoff's SHA-256 is stored outside the Handoff in Kanban attachment metadata or a run manifest so Task Constraints have a verifiable, non-self-referential identity. The worker verifies the full set before acting and records it in its Result.

If a current file no longer matches the bound revision, the worker does not guess. It returns `PRINCIPLE_CONTEXT_STALE`. A newly stricter Hard Boundary always invalidates affected work and requires a newly bound Handoff. If two layers conflict and the precedence rule does not resolve them, the worker returns `PRINCIPLE_CONTEXT_CONFLICT` with the exact clauses.

The current files remain the live source of truth. Each Action preserves an immutable snapshot or content-addressed copy only as execution evidence; historical snapshots never become a second live authority.

## Completion receipt

Every Result or Review records:

- the resolved Hard Boundary, Resolution, Default, Product, Role, and Handoff revisions;
- any authorized exception reference;
- any Principle conflict or requested exception;
- the observable Validation evidence.

It references Principles by identity and does not copy their full text.
