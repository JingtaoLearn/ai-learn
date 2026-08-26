# Daily Data and Immutable Execution Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Deliver the smallest useful stock-research loop: reconcile a requested daily OHLCV history into immutable snapshots, freeze an experiment, execute it in the fixed Docker sandbox, and permanently publish success or failure evidence.

**Architecture:** Extend the merged content-addressed dataset and submission contracts instead of replacing them. A provider-neutral update function consumes fetched bars plus an independently supplied expected-session set, merges them with the latest verified snapshot, detects missing requested sessions, preserves source revisions as new snapshots, and never moves `latest.json` on incomplete input. A runner executes only `build_docker_command`, stores logs without printing them, hashes every artifact, and atomically seals an immutable attempt manifest. Prefect and MLflow will consume the resulting JSON records later; they are not required for correctness in this increment.

**Tech Stack:** Python 3.12, pandas, PyArrow, pytest, Docker CLI, existing content-addressed filesystem store.

---

## Immutable boundaries

- Research only: no broker, paper-trading, credential, or live-order path.
- Never mount `/home/feng/abc-trend-strategy`, `/home/feng/quant-research`, Docker sockets, or existing quant service state.
- Do not use or bind ports 8888, 5000, or 4200.
- Do not expose host environment variables to the run container.
- A failed/incomplete update must not change `latest.json`.
- A failed/timed-out experiment must still leave a sealed, checksummed attempt.
- Existing snapshots, submissions, run contracts, and attempts are never overwritten.

## Task 1: Runtime and runner-image identity contract

**Files:**
- Modify: `pyproject.toml`
- Modify: `src/quant_platform/submissions.py`
- Modify: `src/quant_platform/isolation.py`
- Modify: `tests/test_platform_submissions.py`
- Modify: `tests/test_platform_isolation.py`

**RED:** Add tests proving Python support is `>=3.12,<3.14`, registry digest images remain accepted, immutable local Docker image IDs (`sha256:<64 hex>`) are accepted, tags such as `latest` remain rejected, and the exact verified identity reaches the generated Docker command.

**GREEN:** Tighten `requires-python` and replace the single digest regex with one validator supporting only registry digests or full local image IDs.

**Gate:** Focused tests pass; no relaxation to tags or mutable image names.

## Task 2: Provider-neutral daily data reconciliation

**Files:**
- Create: `src/quant_platform/updates.py`
- Create: `tests/test_platform_updates.py`
- Modify: `src/quant_platform/__init__.py`

**RED:** Add deterministic tests for:
1. first use backfills the exact requested inclusive range;
2. later updates merge latest verified history with fetched bars;
3. identical input is idempotent;
4. a changed historical bar creates a new snapshot and preserves the old one;
5. duplicate/conflicting fetched rows fail closed;
6. any requested expected session absent after merge fails and leaves `latest.json` unchanged;
7. dates outside the requested range cannot satisfy completeness;
8. metadata/instrument/provider mismatches fail;
9. an update cannot use a corrupt latest snapshot;
10. update provenance records request range, expected-session hash, fetched range, prior snapshot, and revision count.

**GREEN:** Implement `reconcile_daily_history(...)`. Expected sessions are an explicit independent input from the adapter; do not infer completeness from returned bars or weekdays. Load and verify the previous snapshot, merge by Date with fetched data winning only after full validation, detect revisions, validate the complete requested expected-session set, then publish a new immutable snapshot. Add update provenance as a separate immutable record keyed by its canonical identity; do not put retrieval timestamps into snapshot identity.

**Gate:** No network in unit tests. Failure paths prove that `latest.json` bytes are unchanged.

## Task 3: Data update CLI

**Files:**
- Modify: `src/quant_platform/cli.py`
- Modify: `tests/test_platform_cli.py`
- Modify: `README.md`

**RED:** Add CLI tests for `research data update --input BARS.csv --expected-sessions SESSIONS.csv --start YYYY-MM-DD --end YYYY-MM-DD` including JSON success, incomplete-history JSON failure, and unchanged latest pointer.

**GREEN:** Wire the CLI to Task 2. Session input must contain exactly one date column; bars retain the existing strict OHLCV schema. CLI output returns IDs/paths/status only and never emits data rows or logs.

**Gate:** Real temp-directory CLI smoke performs first backfill, idempotent update, and one historical revision.

## Task 4: Immutable execution attempts

**Files:**
- Create: `src/quant_platform/runner.py`
- Create: `tests/test_platform_runner.py`
- Modify: `src/quant_platform/isolation.py`
- Modify: `src/quant_platform/cli.py`
- Modify: `tests/test_platform_cli.py`

**RED:** Add tests proving:
1. runner resolves a verified submission and its bound verified dataset;
2. it creates a fresh attempt directory itself and rejects reused IDs;
3. it invokes exactly the command returned by `build_docker_command` with `shell=False` and no inherited secret environment;
4. stdout/stderr go to files and are not returned by the API/CLI;
5. success, non-zero exit, timeout, and launch failure all publish an attempt manifest;
6. manifest records run/submission/dataset/image identity, declared resource envelope, timestamps, duration, exit status, outcome, and per-file SHA-256/size;
7. symlinks, sockets, devices, path traversal, and post-run artifact mutation are rejected;
8. attempt directory is sealed read-only only after manifest publication;
9. a callback receives a small JSON-safe terminal record suitable for later Prefect/MLflow ingestion, but callback failure cannot rewrite the attempt.

**GREEN:** Implement `run_submission(...)` with an injectable process launcher/clock for deterministic tests. Use a minimal scrubbed environment required to reach Docker, do not pass arbitrary host variables, use a process group and bounded timeout, and atomically publish `attempt.json`. Hash regular files only, exclude the manifest from its own checksum map, fsync, then chmod the full attempt tree read-only.

**Gate:** Focused tests exercise all terminal outcomes and integrity checks.

## Task 5: Run CLI and deterministic reference job

**Files:**
- Create: `src/quant_platform/reference_job.py`
- Create: `tests/test_platform_reference_job.py`
- Modify: `src/quant_platform/cli.py`
- Modify: `tests/test_platform_cli.py`
- Modify: `README.md`

**RED:** Add tests for a deterministic reference job that reads `/data/data.parquet`, validates the immutable contracts, writes a JSON result and a CSV daily output to `/artifacts`, and uses only information present in the supplied snapshot. Add `research run --root ... --submission-id ... --attempt-id ... --timeout-seconds ...` tests.

**GREEN:** Implement the reference job and CLI. The job is an execution/integrity demonstration, not a promoted trading strategy; do not add order or broker behavior.

**Gate:** CLI returns only the sealed attempt identity/outcome/path. Artifact content is available from the store, not copied into chat output.

## Task 6: Documentation, packaging, and full verification

**Files:**
- Modify: `docs/architecture/platform-foundation.md`
- Modify: `README.md`
- Modify: `requirements.in` and `requirements.lock` only if dependency changes are unavoidable

**Steps:**
1. Document the adapter contract: fetched bars and expected sessions must come from independently auditable sources; absence of an official/current calendar is a hard failure, not a weekday approximation.
2. Document first-use backfill, incremental overlap/revision handling, immutable attempts, and terminal outcomes.
3. Run all Python tests serially under Python 3.12.
4. Run Node tests, Ruff, Gitleaks, wheel build, and real CLI snapshot/update/submission smoke.
5. Build/identify a local immutable Docker image and execute one real no-network reference attempt.
6. Independently audit finance timing boundaries, artifact integrity, Docker command security, and provenance.

**Acceptance:**
- All old and new tests pass under Python 3.12.
- A real run produces a sealed attempt for both success and intentional failure.
- Recomputed checksums match every manifest.
- Git worktree is clean and contains no runtime data.
- Feng deployment uses a new path and state root, no ports, and does not stop/restart/rebuild existing Agricultural Bank services.
