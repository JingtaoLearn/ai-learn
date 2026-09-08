# Specialist Sessions and Execution Template

Use one instance per Product Agent Suite. It defines execution mechanics, not product Principles.

## Topology

- Product Owner: one stable Profile and one canonical persistent Session.
- Each Specialist Role: one stable Profile.
- Concurrency: zero to three isolated task Sessions under that Role Profile.
- No static cross-Role global cap; live host resources and conflict domains still constrain admission.
- One task Session owns one Action and one workspace/run identity.

## Action admission

Every formal Handoff records:

- exact Role Profile and task Session;
- task/run/correlation/idempotency identities;
- frozen Principle Context revisions;
- immutable base and input identities;
- workspace path, read set, write set, and semantic seams;
- execution host and resource class;
- acceptance, Validation evidence, and Owner return route.

Do not dispatch an incomplete Handoff and promise to attach the real contract later.

## Isolation

- Read/read may share immutable inputs.
- Read/write may overlap only when the reader is pinned to an immutable snapshot.
- Write/write requires distinct workspaces, disjoint write sets, and no shared mutable semantic seam.
- Schema, public interface, migration, dataset identity, registry, deployment target, production meaning, and integration branch are semantic conflicts even when paths differ.
- One physical workspace has one writer; one mutable seam has one owner; one target branch has one integration owner.
- Reviewers read immutable candidates and write separate review artifacts.

## Workspace

Every Action uses:

`<product>/<workstream>/<task-id>/<run-id>/`

with only the directories needed for that Action:

- `repo/` — isolated worktree or exact-SHA checkout;
- `input/` — immutable inputs;
- `output/` — Action-owned outputs;
- `logs/` — decision-relevant command and result evidence;
- `tmp/` — disposable scratch;
- `manifest.json` — identities and hashes when cross-host or formal evidence needs them.

Do not require the full directory/manifest ceremony for a tiny local prototype unless it protects isolation, reproducibility, or a Hard Boundary.

## Execution posture

The control host handles orchestration and light work. Use a remote host only when the Action's actual resource need justifies it.

Even on a production-named host, prefer the shortest reversible prototype deployment that exposes the intended user effect. Require only the operational controls needed for safe read-back and rollback. Defer broad hardening, supply-chain ceremony, and exhaustive testing until the effect is proven or a Hard Boundary demands them.

## Source and transfer

- Prefer immutable Git SHA for source movement.
- Transfer only declared non-Git inputs and verify identity when correctness or sensitivity requires it.
- Never synchronize secrets or mutable home directories.
- Remote output never overwrites source or another run's evidence.
- Preserve failed and partial evidence when it can change the next decision.

## Recovery

A timeout or host loss creates unknown partial state. Reconcile the process, workspace, side effects, and evidence before retry. Retry uses a new run identity when duplicate effects are possible. Terminal Results return to the exact Owner Session, which replans from evidence.
