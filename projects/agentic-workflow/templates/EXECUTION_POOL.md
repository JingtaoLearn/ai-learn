# Specialist Pool and Remote Execution Template

Use one instance per Product Agent Suite. This document is the single source of truth for Specialist pool size, Action admission, workspace isolation, remote heavy execution, and cross-host synchronization.

## Topology

- Product Owner is singleton and keeps one canonical persistent Session.
- Every safely parallelizable concrete Specialist role is a pool of zero to three isolated Profile instances.
- There is no static cross-role global concurrency cap.
- Slot `01` may retain the original unsuffixed Profile ID; slots `02` and `03` use numeric suffixes.
- One Profile owns at most one active Agent process. A fourth slot for one concrete role is invalid.

## Action admission

Every formal Handoff or Kanban card records:

- role pool and exact Profile instance;
- task, run, correlation and idempotency identities;
- immutable base SHA and candidate SHA before formal verification;
- workspace ID and exact path;
- read set, write set and semantic shared seams;
- execution host and resource class;
- lease, heartbeat and fencing identity;
- acceptance commands, evidence paths and Owner callback.

The Owner fills the largest safe dependency-complete Action set. Work is `CAPACITY_SATURATED` only when all compatible role instances are occupied, no compatible execution host is available, or a declared conflict prevents admission. Waiting alone never justifies parking.

## Isolation and conflicts

- Read/read work may share immutable inputs.
- Read/write work may overlap only when the reader is pinned to an immutable snapshot.
- Write/write work requires distinct Profile instances, Sessions, physical workspaces, branches and run artifacts plus disjoint write sets and semantic seams.
- Schema, public interface, migration, dataset identity, generated registry, deployment target, production signal and target branch are conflict domains even when filenames differ.
- One physical workspace has one writer. One mutable semantic seam has one lease holder. One target integration branch has one Integrator.
- Reviewer workers read immutable candidates and write separate review artifacts; they never share the candidate writer's workspace or Profile.
- Stale fencing identities cannot publish evidence, push, request review or integrate.

## Uniform workspace layout

Each host defines `$AGENT_WORK_ROOT`. Every Action uses:

`<product>/<workstream>/<task-id>/<run-id>/`

with:

- `repo/` — isolated worktree or exact-SHA checkout;
- `input/` — immutable read-only inputs;
- `output/` — run-owned outputs;
- `logs/` — command, stdout, stderr, exit and environment evidence;
- `tmp/` — disposable run-local scratch;
- `manifest.json` — identities, host, hashes, commands, outputs, lease and fencing evidence.

No Agent writes into another run directory. Shared caches are content-addressed and read-only. Durable accepted evidence returns to the product's canonical run-artifact directory.

## Heavy execution

The control host owns orchestration, prompts, board state, bounded edits, Git metadata and serialized integration. It may run cheap syntax and formatting checks.

Formal tests, regression, builds, containers, large data transfer or transformation, backtests, bulk experiments, browser rendering and other heavy commands run on an eligible remote execution host chosen from live capability and availability evidence. Do not hard-code a permanent primary.

Formal remote evidence records host, workspace, source/candidate SHA, runtime identity, exact commands, times, output streams, exit status and relevant input/code/dependency/output hashes.

## Synchronization

### Source

- Prefer Git fetch and checkout of an immutable SHA.
- Formal remote verification never uses an uncommitted source tree.
- Never bidirectionally synchronize mutable source directories.
- When Git fetch is unavailable, transfer one immutable archive or bundle and verify its manifest hash before extraction.

### Non-Git input

- Transfer only manifest-listed files.
- Record source, target host, relative destination, size, SHA-256, sensitivity, immutability and direction.
- Normalize every destination against the run root and reject absolute paths, `..` traversal, symlinks, hardlinks, devices, sockets, FIFOs and other special files. Inspect archive members under the same rules before extraction.
- Stage under a run-local `.partial` path, verify size/hash, then atomically promote.
- Accepted inputs are read-only. Corrections create new identities.
- Never synchronize secrets, `.env`, tokens, cookies, SSH material or credential stores.

### Results and cleanup

- Return outputs one way into a new control-side staging path.
- Apply the same normalized-path and regular-file checks before accepting returned results.
- Verify manifest, counts and hashes before durable promotion.
- Remote output cannot overwrite source input or another run's output.
- Retain remote evidence until Owner acceptance and required Review. Cleanup resolves the target beneath the configured run root, rejects symlinks and root/ancestor targets, and deletes only the exact run directory.

## Recovery

A timeout or host loss leaves unknown partial state. Reconcile the manifest, process, workspace, Git state and transferred files before retry. A retry uses a new run ID and workspace while preserving prior evidence. Terminal callbacks return to the exact canonical Owner Session, which then reconciles the full Portfolio and refills compatible role slots.
