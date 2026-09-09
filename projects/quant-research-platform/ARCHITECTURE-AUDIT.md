# Study execution architecture audit

Audit base: commit `b1d3758397ab77c3d96fc4e9bbb2a60f164093ee`, tree `a70fb210486b7368ff4b3280859711ff67fa56ab`.

## Current create-to-conclusion graph

The current Study path is not a lightweight training path. It is an orchestration layer over the complete Experiment/Attempt product:

1. `web.py` and `cli.py` compose `ParameterStudy` with the same `Catalog`, `DatasetService`, and `ExperimentService` used by interactive Attempts.
2. `ParameterStudy.preview()` resolves maintained data, templates, operators, validation folds, search space, and execution identity, then estimates Experiment bindings.
3. `ParameterStudy.submit()` freezes a Study row and appends action/event state.
4. `SerialStudyWorker` calls the private `_advance_next_runnable()` coordinator seam.
5. Candidate suggestion writes `parameter_study_trials` and append-only suggestion-journal rows.
6. Every candidate/fold binding calls `_dispatch_binding()`, materializes a dataset slice, builds a full task, calls `ExperimentService.preview_task()` and `submit_study_effect()`, then writes a binding with foreign keys to both `experiments` and `attempts`.
7. `SerialAttemptWorker` separately claims and executes each Attempt. The runner creates per-Attempt control, stdout/stderr, result, audit, and report-shaped evidence.
8. Study coordination re-reads each Experiment and Attempt, verifies a Metric Document, writes evaluation evidence, selects a candidate, and repeats the same full path for outer audit and terminal holdout.
9. PostgreSQL mirrors this coupling: `qr_catalog.parameter_study_bindings.experiment_id`, `submitted_attempt_id`, and `attempt_id` all reference the full execution ledgers. Study evidence, actions, events, suggestion events, trial rows, Attempt rows, and result/report artifacts amplify with the search plan.

`parameter_study.py` is 7,302 lines and owns schema migrations, request freezing, validation planning, suggestion coordination, leases/fencing, Experiment/Attempt dispatch, metric verification, evaluation, holdout authorization, control actions, and read projections. This is a coupling diagnosis, not a request to split the whole module in this Action.

## Measured write amplification

A read-only query against the deployed zhlearn PostgreSQL authority on 2026-09-09 observed:

- 6 Studies, 185 Study trial rows, 676 Study binding rows, 461 Study action rows, 14 Study event rows, 927 Study evidence rows, and 434 suggestion-journal rows;
- 621 Experiments and 624 Attempts in the same migrated authority;
- representative completed Study `64e1c628...be12`: 15 bindings, 13 distinct Experiments/Attempts after reuse, and 597 bytes of Attempt logs;
- another completed Study has 39 bindings / 33 Attempts; the largest has 580 bindings / 548 Attempts and 21,460 bytes of Attempt logs.

The accepted prior release evidence additionally records that the original two-trial real Study materialized 15 bindings, 15 verified Metric Documents, and 15 Attempts before reuse. The deployed counts show that the problem scales materially: internal training work becomes hundreds of full execution identities and evidence records. The size of log text alone understates write cost because every Attempt also drives lifecycle rows, task/result fields, binding/evidence documents, and historically per-Attempt filesystem control/result material.

## User-visible defects and risks

- Training latency includes full Experiment identity resolution, Attempt admission, runner startup, artifact production, report-oriented evidence, and repeated coordinator polling for each fold.
- PostgreSQL write and read amplification grows with candidate × fold count rather than with the number of Studies.
- Attempt history is crowded with internal training work, making the single-execution product less legible.
- zhlearn performs coordination and can execute all of the full Attempts, competing with UI/API/PostgreSQL responsiveness.
- The shared monolith makes a routing change risky: execution, evaluation, holdout, persistence, and control concerns are interleaved.
- Existing Study history is immutable accepted evidence and cannot be rewritten to look like the new model.

## Target boundaries

### zhlearn

- Formal UI/API and workload-admission authority.
- PostgreSQL authority for one frozen Study/job identity, lifecycle, latest bounded aggregate progress/checkpoint, final aggregate evidence/conclusion, and explicit failure.
- Authenticated dispatch and exact result read-back.
- No synthetic/training computation and no silent local fallback when Feng is unavailable.

### Feng

- Version-pinned compute worker only.
- Validates the admitted source/image identity and authenticated request.
- Performs all internal iterations transiently.
- Keeps one bounded job-state document with at most 20 checkpoints for restart/read-back; no Experiment, Attempt, report, per-iteration row, or per-iteration log.
- Never becomes the formal data authority or serving fallback.

### PostgreSQL

The prototype `qr_study.jobs` contract has exactly one authoritative row per Study. It stores frozen request/source/image identities, lifecycle, latest aggregate progress, one final aggregate result or one explicit failure, and timestamps. Terminal rows are immutable. Its schema has no Experiment/Attempt/report foreign keys and no per-iteration event table.

### Transient worker state

Internal candidate values, scores, loop state, and iteration history remain in worker memory. Only fixed-count checkpoints and the final minimum summary enter the Feng-local job document; zhlearn persists only the latest checkpoint and final aggregate result.

## Implemented vertical seam

`quant_platform.study_remote` is intentionally separate from legacy `ParameterStudy` and does not cut over current UI/API behavior:

- deterministic, content-addressed synthetic Study request;
- Ed25519 request signing with a zhlearn-held private key and Feng-held public key, a one-minute freshness window, and exact method/path/body binding;
- idempotent `POST /v1/studies` and authenticated `GET /v1/studies/{job_id}`;
- exact source commit/tree and Docker image identity on request and read-back;
- one PostgreSQL Study row and one bounded Feng job file irrespective of iteration count;
- explicit `FENG_UNAVAILABLE` terminal failure with `local_compute_attempted: false`;
- direct caller CLI suitable for an ephemeral zhlearn-to-Feng execution.

The synthetic fixture is deliberately non-market and cannot change strategy, report, production signal, Cron, order, or fund meaning.

## Ordered high-confidence change set

1. Validate this isolated caller path and storage shape with real zhlearn-to-Feng execution (this Action).
2. Independently review the remote protocol, bounded PostgreSQL seam, and measured evidence.
3. Add a narrow authenticated Study API/controller adapter on zhlearn for new Studies only; leave legacy reads untouched.
4. Extract real training kernel input/output contracts from the monolith, then run one admitted real Study family through the remote worker without Attempt creation.
5. Switch new Study creation to the lightweight path after direct read-back; retain legacy Study/Attempt history as read-only evidence.
6. Only then delete unreachable binding-to-Attempt creation for new Studies and split broader coordination/evaluation modules where the new seam demonstrates a useful boundary.

Not in this Action: bulk migration, dual-write, legacy rewrite, UI edits, production schema/deployment cutover, generalized queue framework, backup/rollback machinery, or broad test expansion.
