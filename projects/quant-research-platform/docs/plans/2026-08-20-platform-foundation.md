# Quant Research Platform Foundation Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Build a generic, test-first foundation for immutable market-data snapshots and replayable experiment submissions without changing or restarting the Feng Agricultural Bank research stack.

**Architecture:** Extend the existing research-only Python project with a new `quant_platform` package. Keep JupyterLab, Prefect, and MLflow as the open-source human, orchestration, and registry surfaces. Add only the missing governance glue: content-addressed dataset snapshots, strict experiment contracts, atomic publication, and a fail-closed execution envelope. Do not deploy or change the existing Feng Compose project in this phase.

**Tech Stack:** Python 3.12, pandas, pyarrow, pytest, Ruff, existing Docker/JupyterLab/Prefect/MLflow stack.

---

## Protection boundary

The following Feng resources are read-only and out of scope for all implementation tasks:

- `/home/feng/abc-trend-strategy`
- `/home/feng/quant-research`
- Compose project `quant-research`
- Containers `quant-research-jupyter-1`, `quant-research-mlflow-1`, and `quant-research-prefect-1`
- Host ports `127.0.0.1:8888`, `127.0.0.1:5000`, and `127.0.0.1:4200`

Development happens only in `/home/jingtao/worktrees/quant-platform-foundation`, based on `origin/main` commit `ac482b36c5208cf0cf864f56a153d9836954b14e`.

## Task 1: Immutable dataset snapshot contract

**Objective:** Validate daily OHLCV data and publish a deterministic, content-addressed snapshot atomically.

**Files:**
- Create: `src/quant_platform/__init__.py`
- Create: `src/quant_platform/datasets.py`
- Create: `tests/test_platform_datasets.py`

**Required behavior:**

1. Require `Date`, `Open`, `High`, `Low`, `Close`, and `Volume`.
2. Reject duplicate dates, non-finite values, non-positive OHLC, negative volume, and invalid high/low relationships.
3. Normalize rows by ascending date and canonical column order.
4. Derive `snapshot_id` from canonical data plus normalized metadata, not retrieval time or filesystem path.
5. Publish under `<root>/datasets/<instrument>/<snapshot_id>/` using a same-parent temporary directory followed by atomic rename.
6. Write `data.parquet` and `manifest.json`; record canonical-data and Parquet SHA-256 values.
7. Update `latest.json` only after the snapshot is complete.
8. Re-publishing identical data returns `NO_CHANGE`; a historical revision creates a new snapshot and leaves the old snapshot readable.
9. Use directory mode `0755` and file mode `0644`.

**TDD:** Write one failing behavior test at a time, run the exact test to confirm RED, implement minimally, then run the module and full suite for GREEN.

## Task 2: Replayable experiment submission contract

**Objective:** Create an immutable submission that binds source, dataset, configuration, and an enforced resource envelope.

**Files:**
- Create: `src/quant_platform/submissions.py`
- Create: `tests/test_platform_submissions.py`

**Required behavior:**

1. Accept only these top-level specification fields: `name`, `entrypoint`, `dataset_snapshot_id`, `runner_image`, `config`, and optional `seed`; `runner_image` must be digest pinned.
2. Reject unknown fields, absolute entrypoints, path traversal, missing entrypoints, symlinks, and secret-like files.
3. Copy an allowlisted source bundle into `<root>/submissions/<submission_id>/source/`.
4. Include source resources, all tests (including non-Python tests), scripts/web resources when present, `pyproject.toml`, `requirements.in`, and `requirements.lock`; exclude runtime data, runs, caches, VCS metadata, and environment files.
5. Derive `submission_id` from the source hash, dataset snapshot ID, runner image digest, canonical spec, and the fixed execution envelope.
6. Freeze the envelope at `1.0 CPU`, `512 MiB`, `256 PIDs`, `network=none`, `read_only_root=true`, `cap_drop=ALL`, and `no_new_privileges=true`.
7. Publish atomically and return `NO_CHANGE` for identical re-submission.
8. Write `submission.json` with all provenance fields and artifact checksums.

**TDD:** Follow strict RED-GREEN-REFACTOR per behavior.

## Task 3: CLI for humans and agents

**Objective:** Expose the same deterministic interface to Jupyter users and agents.

**Files:**
- Create: `src/quant_platform/cli.py`
- Create: `tests/test_platform_cli.py`
- Modify: `pyproject.toml`

**Commands:**

- `research data snapshot --input CSV --root PATH --instrument CODE --provider NAME --market NAME --currency CODE --adjustment MODE`
- `research data status --root PATH --instrument CODE`
- `research submit --spec JSON --project-root PATH --root PATH`
- `research submission show --root PATH --submission-id ID`

All commands emit one JSON object to stdout and use non-zero exit status for invalid input. They must never print source contents, credentials, or environment values.

## Task 4: Static isolation and open-source integration decision record

**Objective:** Make the non-interference boundary and open-source adoption gate explicit and testable.

**Files:**
- Create: `docs/architecture/platform-foundation.md`
- Create: `src/quant_platform/isolation.py`
- Create: `tests/test_platform_isolation.py`

**Required behavior:**

1. Generate a Docker execution argument list without invoking Docker.
2. Enforce `--network none`, `--cpus 1.0`, `--memory 512m`, `--pids-limit 256`, `--read-only`, `--cap-drop ALL`, and `no-new-privileges`; take the image only from the verified submission.
3. Mount source, dataset, submission contract, and content-addressed run contract read-only; mount only a fresh `<platform>/artifacts/<submission-id>/<attempt-id>` directory read-write.
4. Reject protected Feng paths, host-port publication, Docker socket mounts, privileged mode, and arbitrary caller overrides.
5. Document component decisions:
   - reuse JupyterLab, Prefect, and MLflow now;
   - benchmark Hikyuu and RQAlpha against the transparent engine before selecting an A-share primary engine;
   - add Optuna only after the submission and snapshot contracts are stable;
   - defer Qlib and RD-Agent until multi-stock ML research is justified;
   - retain the existing pandas engine as a reference oracle, not the only truth source.

## Task 5: Verification and integration

**Objective:** Prove the foundation works without accessing the Feng protected resources.

**Files:**
- Modify: `README.md`
- Modify: `.gitignore` only if new local runtime paths need exclusion.

**Verification:**

1. Run the full pytest suite serially.
2. Run Ruff on the complete project.
3. Create a synthetic A-share CSV, publish a snapshot, re-publish it for `NO_CHANGE`, revise one historical row, and confirm a new immutable snapshot.
4. Submit a tiny deterministic experiment against the snapshot and confirm identical re-submission is `NO_CHANGE`.
5. Verify the generated Docker arguments contain all safety controls and no protected path.
6. Verify Feng container IDs, start times, mounts, health, and Agricultural Bank directory mtimes are unchanged from the preflight snapshot.
7. Compute the effective source hash and run an independent finance/provenance/security review before commit.

## Explicit non-goals

- Do not restart, stop, recreate, or modify any Feng quant container.
- Do not write into `/home/feng/abc-trend-strategy` or `/home/feng/quant-research`.
- Do not add a new web frontend.
- Do not add Kubernetes, DVC, lakeFS, Delta Lake, Qlib, RD-Agent, Optuna, or broker connectivity in this phase.
- Do not execute live orders or expose a public network service.
