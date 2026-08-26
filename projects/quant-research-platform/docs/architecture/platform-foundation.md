# Quant Research Platform Foundation

## Decision

The platform is **open-source first, contract driven, and minimal-custom-code**.
It composes mature tools instead of building a new all-in-one application:

- JupyterLab remains the human research and editing surface.
- Prefect remains the run orchestration, retry, timeout, and scheduling surface.
- MLflow remains the searchable experiment index and comparison UI.
- The existing transparent pandas engine remains a reference oracle, not the only source of truth.

The custom foundation is limited to gaps those tools do not solve safely for this project:

1. content-addressed and immutable daily-market-data snapshots;
2. replayable source/config/data submissions;
3. fixed, fail-closed resource and security envelopes;
4. later, A-share market-semantics adapters and promotion governance.

## Phase-one runtime contract

Every dataset snapshot binds canonical OHLCV data to instrument, provider, market,
currency, and adjustment metadata. Retrieval time and filesystem paths do not
change its identity. Invalid data cannot move the `latest.json` pointer.
Historical revisions create a new snapshot and preserve the old one.

Every experiment submission binds:

- an allowlisted source bundle;
- one immutable dataset snapshot;
- one digest-pinned runner image;
- canonical configuration and random seed;
- a fixed execution envelope;
- per-file and aggregate source checksums.

The execution command is generated from the immutable submission rather than
caller-provided Docker flags. It enforces:

- `1.0 CPU`, `512 MiB`, and `256` PIDs;
- no network;
- read-only root filesystem;
- all Linux capabilities dropped;
- `no-new-privileges`;
- read-only source and dataset mounts;
- one unique writable artifact mount;
- a digest-pinned runner image taken only from the verified submission.

Preparing an execution also creates an immutable, content-addressed `run.json`
that binds the submission, dataset, runner image, fixed execution envelope, and
unique attempt directory. The container receives read-only submission and run
contracts; the only writable host mount is the empty
`artifacts/<submission-id>/<attempt-id>` directory.

## Feng non-interference boundary

The Agricultural Bank research remains authoritative and untouched in:

- `/home/feng/abc-trend-strategy`;
- `/home/feng/quant-research`;
- Compose project `quant-research`;
- loopback ports `8888`, `5000`, and `4200`.

The foundation rejects protected Feng paths when constructing a run command.
Phase-one development and verification happen in the independent Git worktree
`/home/jingtao/worktrees/quant-platform-foundation`. No Feng service is stopped,
restarted, rebuilt, or reconfigured.

## Open-source adoption gates

### Reuse now

- **JupyterLab** for notebooks and interactive work.
- **Prefect** for orchestration.
- **MLflow** for experiment indexing and comparison.
- **pandas/NumPy/PyArrow** for the transparent reference implementation and
  deterministic data artifacts.

### Benchmark before adopting

- **Hikyuu** and **RQAlpha** are A-share engine candidates. They must run the same
  Agricultural Bank synthetic fixtures and frozen strategy semantics as the
  reference engine. Adoption requires matching signal timing, cost accounting,
  open-position handling, ledger/equity reconciliation, and acceptable small-node
  resource use.
- A candidate does not become the primary engine merely because it is open source.
  It must be actively maintainable, license-compatible, deterministic, agent
  accessible, and financially correct for the intended market.

### Defer until justified

- **Optuna** follows only after snapshot and submission contracts are stable.
- **Qlib** is for multi-stock factor/ML research, not the first single-stock daily
  strategy slice.
- **RD-Agent** follows only after deterministic validation, experiment budgets,
  and sealed-holdout governance exist.
- Broker, paper-trading, and live-order paths are out of scope.

## Promotion gates

An experiment may become a permanent research run only when:

1. the dataset and submission resolve to complete immutable artifacts;
2. schema, secret, source, static, unit, and synthetic-market tests pass;
3. signal availability and next-attainable execution timing pass;
4. transaction costs and the trade ledger reconcile to final equity;
5. the run executes in the fixed sandbox and records resource usage;
6. artifacts are checksummed and archived before success is published.

No agent can promote a research candidate to paper or live trading on its own.
