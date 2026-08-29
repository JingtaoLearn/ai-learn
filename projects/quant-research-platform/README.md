# Gold Quant Research

A reproducible, research-only vertical slice for daily gold strategy analysis. It is designed for both human researchers and agents, uses transparent pandas code, and contains no broker integration or live-order path.

## Generic platform foundation

The repository now also provides a market-agnostic `research` CLI for immutable data and experiment governance:

```bash
research data snapshot --input daily.csv --root state/platform \
  --instrument 601288.SS --provider vendor-name --market XSHG \
  --currency CNY --adjustment unadjusted
research data update --input daily.csv --expected-sessions sessions.csv \
  --start 2026-01-01 --end 2026-08-26 --root state/platform \
  --instrument 601288.SS --provider vendor-name --market XSHG \
  --currency CNY --adjustment unadjusted
research data status --root state/platform --instrument 601288.SS
research submit --spec experiment.json --project-root . --root state/platform
research submission show --root state/platform --submission-id SHA256
research run --root state/platform --submission-id SHA256 \
  --attempt-id attempt-001 --timeout-seconds 300
```

Dataset snapshots, update provenance, experiment submissions, and execution attempts are content-addressed or uniquely named, atomically published, and never overwritten. Daily updates require an independently supplied expected-session CSV whose schema is exactly one column named `Date` (case-sensitive); incomplete requested histories fail without moving the latest pointer. The authenticated experiment UI additionally resolves stable dataset catalog IDs and date ranges. If a selected range is incomplete, it fetches one complete provider generation, verifies every expected exchange session, publishes provenance, and only then freezes the resulting snapshot. Experiment submissions freeze the source bundle, dataset catalog item and range, concrete snapshot identity, immutable runner image, configuration, seed, checksums, and a fixed `1 CPU / 512 MiB / no-network` execution envelope. Every run receives a content-addressed contract and fresh artifact directory; success, process failure, timeout, and launch failure all leave a checksummed read-only attempt manifest. `quant_platform.reference_job` is a deterministic integrity demonstration that derives JSON and daily CSV evidence only from its supplied snapshot, not a promoted trading strategy. See [`docs/architecture/platform-foundation.md`](docs/architecture/platform-foundation.md) for the open-source adoption gates and the Feng Agricultural Bank non-interference boundary.

## Config-driven single-stock strategy runs

One strict JSON configuration can now bind an immutable daily snapshot, the complete
versioned operator graph, account assumptions, and the output root:

```bash
research strategy run --config examples/bocom-causal-slope.json
```

The command writes exactly one JSON success or failure object. Before using the BOCOM
example, replace its all-zero `dataset.snapshot_id` with the ID returned by
`research data snapshot` or `research data update`, and keep `dataset.instrument`
equal to that snapshot's instrument metadata. Relative dataset and output roots are
resolved from the command's working directory.

For an installed wheel, run from the project source checkout or pass its absolute path
with `--project-root`. `QUANT_PLATFORM_PROJECT_ROOT` provides the equivalent environment
override. Explicit roots are validated before the working directory is consulted.

Daily snapshot ingestion preserves canonical optional `AdjustedClose`; the legacy input
header `Adj Close` is accepted only as an ingestion alias and stored as
`AdjustedClose`. Snapshots containing optional research columns use a column-bound v2
identity, while required-only OHLCV snapshots retain their existing v1 identity. Raw
`Open` and `Close` always remain available for executable accounting and valuation.
Published snapshot directories are sealed `0555` with `0444` files; the writable
instrument parent and `latest.json` pointer remain outside that immutable boundary.

Configuration ownership is closed and exact:

| Owner | Fields |
|---|---|
| Top level | integer schema version, dataset root/instrument/snapshot ID, output root |
| Template | display name, evaluation dates, initial capital/state, terminal handling, explicit cost-assumption label |
| Fit | prior-only log OLS window and explicit `AdjustedClose` or raw `Close` signal selection |
| Smoothing | recursive log-EMA span |
| Statistic | adjacent smoothed-curve percent slope |
| Decision | post-start buy/sell crossing thresholds |
| Sizing | target fraction and A-share board-lot size |
| Cost | commission, minimum commission, transfer fee, sell stamp tax, and side-specific slippage |
| Report | no parameters or hidden defaults |

Unknown fields, missing fields, duplicate JSON keys, wrong types, booleans used as
numbers, non-finite values, unknown operator versions, slot mismatches, parameter
collisions, and undeclared parameters fail before publication. Operators are selected
only from the built-in registry; configuration cannot provide Python import paths.

For each evaluation session, the OLS history ends strictly before that session. The
hysteresis state starts flat, the first finite slope only initializes its zone, and an
upward threshold crossing executes at that session's raw Open. A sell crossing while
flat is ignored. Buys use the largest affordable board lot after itemized costs; sells
dispose of all held shares. The default terminal policy marks an open position at the
last raw Close without inventing a sale or exit cost. `force_liquidate` instead records
an actual final-session raw-Open sell.

Every successful run publishes a read-only directory keyed by canonical config,
verified dataset, and effective source identities:

- `config.json`
- `run_manifest.json`
- `daily_replay.csv`
- `events.csv`
- `trades.csv`
- `metrics.json`
- `cost_breakdown.json`
- `report.html`

The effective source identity hashes `datasets.py` and every strategy module from the
package directory that Python actually loaded, while retaining stable
`src/quant_platform/...` manifest labels. A complete validated source root is also
required so `pyproject.toml` and `requirements.lock` are always hashed. Source-root
discovery checks a validated explicit argument or environment override first, then the
working directory and its ancestors, and finally an editable `src` layout. Symlinked,
unsafe, incomplete, missing, or ambiguous roots fail closed. The identity also binds the
exact Python, pandas, NumPy, Matplotlib, and PyArrow runtime versions. The manifest
records Git state only for a discovered checkout; complete source archives without Git
metadata use explicit null/unavailable values. File and dependency hashes remain
authoritative.

The Chinese mobile-first report is self-contained, uses a verified CJK font, and plots
raw prices, causal fitted/smoothed trend, actual raw-Open BUY/SELL events, holding spans,
slope thresholds, and cumulative strategy/zero-cost/buy-and-hold equity. It derives its
literal rules from the frozen configuration and escapes user-controlled text. Exact
reruns verify every artifact and return the same run with `NO_CHANGE`; missing, changed,
writable, or conflicting artifacts fail closed and are never repaired in place.
The `trades.csv` machine column `return` is each trade's invested-position return and
excludes residual account cash; portfolio return remains the account-level metric in
`metrics.json` and the report summary.

The strategy, zero-cost comparison, and same-period buy-and-hold account are
research-only price-return accounts. Without explicit dividend or corporate-action cash
flows they are not total shareholder return. The committed BOCOM cost values are labeled
as a conservative research assumption, not Jingtao's or any other account's commission.
There is no broker, live-order, paper-order, network, or parameter-search path.

## Operator registry and experiment UI

The authenticated registry UI separates immutable operator publication from experiment
submission. Initialize or inspect the same domain layer without HTTP:

```bash
research operator list --root state/platform
research operator detail --root state/platform --operator-id prior_log_ols --version 1.0.0
research template detail --root state/platform --name single_stock_daily_causal --version 1
research task resolve --root state/platform --spec task.json
research task submit --root state/platform --spec task.json --action-id request-001
research task rerun --root state/platform --experiment-id EXPERIMENT_SHA256 --action-id rerun-001
research experiment list --root state/platform
research attempt list --root state/platform --experiment-id EXPERIMENT_SHA256
```

The initial migration seeds template `single_stock_daily_causal@1` and the seven built-in
operators as published `1.0.0` versions. Every custom operator supplies one `operator.py`, an exact
parameter schema and defaults, Markdown documentation, and deterministic JSON fixtures. All seven
slots have narrow JSON contracts. Submitted source is opaque to the CLI and web process: a
digest-pinned Docker runner compiles and tests it with no network, a read-only root/source, one CPU,
512 MiB memory, a PID limit, no capabilities, no-new-privileges, and a non-root UID. Evidence is
created by that runner and binds the candidate, fixtures, image, and execution envelope.
At experiment time, every resolved custom slot is assembled into one composition and the complete
causal daily replay runs in one isolated container launch for that attempt.

Production uses the existing `/home/feng/quant-platform/state/platform` root. The catalog database,
operator bundles, experiments, controls, and results coexist there with authoritative immutable
`datasets/` snapshots and future daily updates. Initialization does not copy, move, or symlink
snapshot data.

Catalog migration recognizes the existing `601328.SS` snapshot only when its verified metadata
identifies `provider=yahoo-chart-api` and `market=XSHG`. It registers stable ID `601328.SS` with
display name `Bank of Communications (601328.SS)` while retaining the snapshot's exact provider,
currency, adjustment, hashes, files, and identity. The UI obtains the latest available close from
the canonical Yahoo chart endpoint and defaults the separate end-date control to that value.
Automatic repair validates one aligned daily Yahoo response, binds its URL and response SHA-256,
and requires the complete requested session set from pinned `exchange-calendars==4.13.2`.
That XSHG calendar embeds the published 2026 SSE holiday schedule and is deliberately bounded;
requests beyond its authoritative coverage fail. A missing exchange session remains an explicit
suspension/provider-gap error requiring independent evidence. The platform never substitutes a
weekday calendar or fabricates a bar. Update records are sealed as `0444` files in `0555`
content-addressed directories and fully verified before reuse. The experiment copies the canonical
producer update ID, provider response identity, calendar identity, and expected-session evidence
into its own immutable identity. The first resolution also seals a permanent per-snapshot lineage
claim, so later byte-identical reversions cannot replace it. Snapshots with no verified lineage
receive a permanent `legacy_snapshot` claim; lineage is never invented.

Task documents contain no source. They select `latest` or an explicit published version and set
only declared parameters. Submission freezes the dataset snapshot, template, resolved operator
versions/digests/parameters, and execution identity. An exact duplicate returns the existing
experiment without an attempt. Rerun accepts only the experiment ID and creates an idempotent new
attempt with the frozen resolution. Each attempt can be launched once; restart recovery marks an
abandoned running attempt failed and requires an explicit new rerun.

UI/API catalog tasks use
`{"dataset":{"dataset_id":"601328.SS","start":"YYYY-MM-DD","end":"YYYY-MM-DD"}}`.
The submitted template dates must match that selected interval. Calendar-day boundaries are
retained in request audit, while the first and last verified exchange sessions become the frozen
dataset/template range and canonical experiment identity. Equivalent weekend/session boundaries
therefore suppress duplicates. JSON-Schema enums render
as native selects for template and every operator version. Enum option values use canonical JSON,
so `null`, `""`, strings, booleans, integers, and numbers remain unambiguous in browser previews
and no-JavaScript forms. Non-enum booleans use true/false selects, numeric properties use bounded
number inputs, and ordinary strings remain text inputs. Integers are restricted to JavaScript's
exact safe-integer range and negative floating-point zero is rejected, preventing browser JSON
normalization from changing identity. Every page also exposes a keyboard-accessible light/dark/system
selector. Its choice is stored in `localStorage`; a small same-origin initializer runs before CSS,
and system mode follows `prefers-color-scheme`.

The FastAPI/Jinja2 application is started with:

```bash
python -m quant_platform.web
```

### Proofline design verification

Run the pinned design specification linter without adding a production dependency:

```bash
./scripts/design_lint.sh
```

The Chromium acceptance test uses one CDP harness with bounded scopes:

```bash
PROOFLINE_BROWSER_SCOPE=foundation pytest -q tests/test_browser_acceptance.py
PROOFLINE_BROWSER_SCOPE=overview pytest -q tests/test_browser_acceptance.py
PROOFLINE_BROWSER_SCOPE=report pytest -q tests/test_browser_acceptance.py
PROOFLINE_BROWSER_SCOPE=study pytest -q tests/test_browser_acceptance.py
pytest -q tests/test_browser_acceptance.py
```

`foundation` covers login, an empty dashboard, skip-link traversal, POST logout,
forced colors, and reduced motion with JavaScript enabled and disabled. `overview`
covers mobile navigation and recent-attempt identity density. `report` covers the
report wrapper/full-screen action and responsive proxies. `study` covers the
Study lifecycle at mobile and desktop widths with JavaScript enabled and disabled. The 320
CSS-pixel DPR2 check is only a high-density reflow gate; DPR is not browser zoom. The separate
200% text-resize proxy doubles the root text size and checks layout, hit targets, utility
access, and fixed-navigation reserves. It does not automate browser page zoom, pinch
zoom, or `visualViewport` scaling. Those remain manual browser review gates. Screenshots
are written only to `PROOFLINE_SCREENSHOT_DIR` (or `/tmp/proofline-browser-artifacts`).

### Parameter Study UI

Authenticated researchers can open `/studies` to create and inspect immutable Parameter
Studies. The server-rendered wizard supports catalog Dataset number/name selection, date
ranges, fixed operator parameters, explicit research-parameter checkboxes, typed categorical,
integer, and float search distributions, deterministic Grid/Seeded Random, and adaptive
Optuna TPE. Cost, report, and template-protocol parameters remain fixed so the optimizer
cannot improve its score by changing accounting or evidence semantics. Search budgets,
chronological split controls, terminal holdout settings, and complete Study Lineage are
frozen before execution. Preview resolves the exact plan and split windows and reports a
baseline-only minimum plus a conditional adaptive upper bound for Experiment bindings.
Selection-dependent bindings and canonical Experiment reuse are resolved only at dispatch.
If an identity changes, submission creates nothing and returns a fresh preview for review.

Optuna is a version-frozen parameter suggester, not a platform fact store. Each outer round
and the final round follows an append-only `ask -> canonical inner folds -> independent
evaluate -> tell` journal. The adapter receives only same-round `INNER_SCORE` evidence;
outer-audit and terminal-holdout evidence are rejected. Restart reconstructs Optuna 4.9.0
from that journal and verifies every replayed proposal. Duplicate suggestions are recorded
without creating duplicate Trials, and failed candidates are told as failed without a
fabricated score.

Study detail and report views keep outer OOS selection-process evidence visibly separate
from terminal holdout access, outcome, and freshness. Trial rankings display eligibility,
constraint reasons, independent metrics, parameter identity, and Experiment bindings
when those fields are available from `ParameterStudy.detail()`. Linked Experiment
reports continue through the checksum-verified opaque-origin sandbox. Every creation,
preview, submit, control, and detail action has a plain HTML form path and remains usable
without JavaScript.

Production settings fail closed unless Microsoft SSO, session signing, allowed emails, the exact
`https://quant.ai.jingtao.fun/auth/callback`, the same exact callback as JWT audience, secure cookies, and a
digest-pinned runner image are configured. The app binds only `127.0.0.1:8090`. Reports are served
only from verified immutable attempt artifacts and embedded without script, same-origin,
navigation, popup, or download privileges.

Reviewed code-only deployment templates are under [`deploy/`](deploy/). They use the placeholder
`/home/feng/quant-platform/releases/REPLACE_WITH_RELEASE_ID`. In both files, deployment must
substitute the exact immutable release ID. Do not use the `current` symlink: project-root validation
intentionally rejects every symlink component. The
[ailearn SSH tunnel](../../vm/host-services/quant-research-tunnel/) resolves one nginx-proxy bridge
gateway and writes that exact address for both its SSH bind and the
[no-port nginx sidecar](../../vm/docker-services/quant-research-ui-proxy/). No deployment action is
performed by this repository phase.

## Decision boundary

- `GC=F` is Yahoo's continuous gold-futures **research proxy**. It is not an executable futures contract and hides roll construction.
- `GLD` is the tradable ETF proxy used for cross-validation.
- Results are hypothetical research, not investment advice. Do not deploy them directly.

## What the first slice does

1. Downloads Yahoo chart API daily OHLCV for `GC=F` and `GLD` from 2010 onward.
2. Writes CSV, Parquet, and `data_manifest.json` with URL, period, retrieval timestamp, SHA-256, row count, and observed date range.
3. Tests buy-and-hold, SMA 50/200, and Donchian 55/20 with explicit one-row signal delay.
4. Reports gross and net results with configurable one-way costs (default 5 bps).
5. Runs a chronological 70/30 research/out-of-sample split, 10 bps double-cost stress, and Donchian 50/20, 55/20, 60/20 stability checks.
6. Produces decision-first, 390 px-friendly HTML with comparisons, equity/trade charts, ledger, caveats, and run metadata.
7. Uses a Prefect flow and logs one MLflow run per symbol/strategy, including full, OOS, and cost-stress prefixed metrics plus complete research artifacts.

## Round-two robustness study

`gold_research.round2.run_round2_research` adds four frozen, long-only candidates without selecting a winner after seeing the results:

- close above the 200-session moving average;
- positive 252-session absolute momentum;
- a two-of-three vote across 63/126/252-session momentum;
- the 200-session trend filter scaled to a 10% volatility target.

The study aligns every strategy after a shared 315-session warm-up, evaluates 5/10/20 bps one-way costs, reports calendar-year pseudo-out-of-sample folds from 2010, checks small parameter neighborhoods, and compares each timing rule with buy-and-hold using a paired 20-session moving-block bootstrap. Its familywise confidence level is Bonferroni-adjusted across the 12 strategy/instrument comparisons. Because the complete historical period has already been inspected, these folds are retrospective stress tests, not a pristine final holdout.

Round-two runs add:

- `summary.csv` and `daily_returns.csv`;
- `annual_folds.csv`;
- `bootstrap.json`;
- `parameter_stability.csv`;
- `current_signals.csv`;
- `report.html`.

## Interactive strategy lab

`web/strategy-lab/` is a zero-backend, browser-local backtesting interface. It embeds public daily `GLD` and `GC=F` data into one self-contained HTML file. A researcher can change the instrument, evaluation period, one-way cost, and parameters for buy-and-hold, SMA, Donchian, trend, momentum, multi-horizon voting, volatility-managed trend, and the frozen strong-trend/low-volatility candidate. Metrics, current position, yearly returns, net equity, and drawdown update immediately without sending parameters to a server.

Build from saved runtime CSVs:

```bash
python3 scripts/build_strategy_lab.py \
  --data-dir data/raw \
  --output /tmp/gold-strategy-lab.html
```

Or refresh directly from Yahoo before building:

```bash
python3 scripts/build_strategy_lab.py \
  --refresh \
  --output /tmp/gold-strategy-lab.html
```

The output is research-only and contains no order path, credentials, or broker integration. JavaScript core behavior is covered by `node --test tests/test_strategy_lab.js`; the Python builder has a deterministic synthetic-data test.

## Trend-temperature study

`gold_research.round3.run_trend_temperature_research` adds a transparent,
Trend Animal-inspired state machine. It is explicitly not a reconstruction of
the proprietary model. The frozen score is 63-session log momentum divided by
63-session log-return volatility scaled by the square root of the lookback:

- cold: score below `-0.5`;
- flat: `-0.5` to below `0.5`;
- warm: `0.5` to below `1.0`;
- hot: `1.0` or above.

The binary strategy enters after the score reaches hot and exits after it cools
below `0.5`. The risk-managed variant applies a capped 10% annualized
volatility target. Both use prior-close information and execute at the next
open. The run compares them with buy-and-hold, Donchian 55/20, the 200-session
trend filter, and 252-session momentum; it also records 5/10/20 bps cost tests,
calendar-year folds, paired block bootstrap intervals, and one-at-a-time
parameter neighborhoods.

```python
from pathlib import Path

from gold_research.round3 import run_trend_temperature_research

result = run_trend_temperature_research(data, Path("runs/trend-temperature"))
```

Each immutable run writes `summary.csv`, `annual_folds.csv`, `bootstrap.json`,
`parameter_stability.csv`, `current_signals.csv`, `state_history.csv`,
`trades.csv`, and a decision-first `report.html`.

## Three-year walk-forward study

`gold_research.round4.run_round4_research` evaluates exactly the latest three
calendar years ending at the latest common completed daily bar. Earlier bars
warm indicators only. Its fixed seven-candidate set is buy-and-hold, the
200-session trend filter, 252-session momentum, Donchian 55/20, the
63/126/252 momentum vote, 10% volatility-managed trend, and the transparent
63-session temperature proxy.

The study uses anchored expanding training (at least 252 sessions) followed by
complete 63-session test blocks. Within each fold it aggregates equal-weight
CAGR, Sharpe, and Calmar ranks across `GC=F`, `GLD`, and 5/20 bps one-way costs,
then breaks ties by lower turnover and candidate name. It writes stitched
**retrospective pseudo-OOS** paths for the adaptive selector and every frozen
candidate. `GC=F` and `GLD` are correlated confirmations, not independent
samples; alternative score weights, especially a risk-first objective, can
change the winner.

```python
from pathlib import Path

from gold_research.round4 import run_round4_research

result = run_round4_research(data, Path("runs/three-year-walk-forward"))
```

Each immutable run writes `candidate_summary.csv`, `walk_forward_folds.csv`,
`walk_forward_daily.csv`, `pseudo_oos_summary.csv`, `latest_signals.csv`,
`markers.csv`, `trades.csv`, and a mobile-readable `report.html` with an
embedded transition chart, alongside configuration and provenance manifests.
Fold selection retains the known train-end rebalance cost but excludes its
forward return because that return is only known at the test-start open.
`markers.csv` records fractional `ADD`/`REDUCE` rebalances that incur costs,
while `trades.csv` remains a zero-to-positive round-trip ledger.

## Agricultural Bank Round-2 evidence study

`gold_research.abc_round2_study.run_abc_round2_study` runs the frozen Round-2
protocol for Agricultural Bank of China (`601288.SS`) and six predeclared bank
peers. It evaluates exactly four public-formula long-or-cash candidates after
each symbol's own 315-session warm-up. There is no parameter search, candidate
selection, or forced winner.

Every manifest entry is bound to the exact symbol's canonical Yahoo chart URL
and verified against the referenced CSV checksum, row count, date range, and
parsed contents. Session dates are normalized to Shanghai dates, and bars on or
after the analysis date are excluded before scoring. Yahoo's adjusted-close
factor puts all OHLC fields on one total-return scale. The base 8/13 bps and
stress 20/25 bps cost cases are optimistic comparability scenarios, not claims
of real fill completeness.

```python
from pathlib import Path

from gold_research.abc_round2_study import run_abc_round2_study

result = run_abc_round2_study(
    data_dir=Path("data/agricultural-bank-round2"),
    output_root=Path("runs/agricultural-bank-round2"),
    analysis_date="2026-08-20T13:00:00+08:00",
    protocol_path=Path("protocol.yaml"),
)
```

The runner applies every promotion gate independently to all four candidates.
`execution_complete` defaults to `False`, so the execution-completeness gate
fails until separate fill evidence exists. Target-only evidence includes exact
circular-shift timing placebos, complete four-calendar-year blocks, every
modeled trade, daily returns, and a next-open target derived only from the last
completed real close. Peer validation compares each rule with each peer's own
buy-and-hold Sharpe.

Publishing uses a temporary sibling directory and atomic rename. The immutable
run ID binds canonical configuration, completed normalized data, protocol bytes,
and Git/source state. The non-HTML artifact contract contains configuration and
provenance JSON, a four-row trial registry, candidate and benchmark summaries,
peer validation, target daily returns and trades, subperiods, timing results,
independent gate decisions, and current modeled signals. The study remains
retrospective and research-only; it has no broker or live-order path.

## Execution convention

Signals use closes through day `t-1`, enter at the next available daily open `t`, and earn the open-`t` to open-`t+1` return. This removes the unattainable same-close fill from the first prototype. The model still does **not** simulate opening-auction slippage, bid/ask spread, market impact, or intraday execution.

## Pinned and local-only infrastructure

The Docker base image is pinned by digest. `requirements.lock` is generated with hashes for the complete transitive Python environment, and the Docker build installs it with `--require-hashes`. Compose builds one fixed local image, `gold-quant-research:0.1.0`, and reuses it for:

| Service | Host binding | Persistence |
|---|---|---|
| JupyterLab + server proxy | `127.0.0.1:8888` | repository and `state/jupyter` |
| MLflow | `127.0.0.1:5000` | `state/mlflow` |
| Prefect server | `127.0.0.1:4200` | `state/prefect` |

No PostgreSQL, Redis, Kubernetes, or custom frontend is used. Memory/CPU limits are set for an 8 GiB, 2-core host.

Jupyter uses a random token, runs as UID 1000 rather than container root, and remains bound to `127.0.0.1`. Generate the private `.env` once without printing the token:

```bash
umask 077
python3 -c 'from pathlib import Path; import secrets; p=Path(".env"); p.exists() or p.write_text("JUPYTER_TOKEN="+secrets.token_urlsafe(32)+"\n")'
```

Never change the binding to a public interface without TLS and an explicit authentication review.

Example tunnels from a trusted workstation:

```bash
ssh -L 8888:127.0.0.1:8888 -L 5000:127.0.0.1:5000 -L 4200:127.0.0.1:4200 feng-learn
```

After connecting, run `./scripts/jupyter_url.sh` on the server yourself to display the tokenized local URL. Agents must not run that script or print `.env`.

## Commands

```bash
./scripts/manage.sh infra-up       # build and start all three services
./scripts/manage.sh infra-health   # Compose state plus real HTTP health checks
./scripts/manage.sh test           # deterministic synthetic unit tests; no network dependency
./scripts/manage.sh lint           # Ruff
./scripts/manage.sh run            # Yahoo -> Prefect -> research -> MLflow
./scripts/manage.sh cmb-snapshot   # Append CMB public SGE market snapshot
./scripts/manage.sh infra-down
```

The `Makefile` is a convenience alias for hosts that already have GNU Make; the shell
entry point above is canonical and needs no package beyond Bash, Python, and Docker Compose.

A Yahoo/network failure raises `DataDownloadError` with the affected symbol and underlying HTTP/parse detail. Online acquisition is intentionally excluded from unit tests.

## CMB public gold snapshot

`./scripts/manage.sh cmb-snapshot` appends the current public CMB gold-market payload to
`data/cmb/` as CSV and Parquet with a manifest. The endpoint exposes Shanghai Gold Exchange
reference-market snapshots such as `Au(T+D)` and `Au99.99`.

This is **not** the authenticated CMB Gold Account purchase or redemption quote, contains no
bank bid/ask spread, and provides no historical backfill. It can be used for timestamped
reference-market cross-checks and forward data collection, but the public snapshot may be delayed
or stale. It must not be presented as an executable CMB Gold Account price or used alone for a
historical strategy backtest. Each collected row stores a canonical payload checksum for provenance.

## Artifacts

Raw inputs live under `data/raw/` and complete research runs under `runs/<stable-run-id>/`:

- `config.json`, `run_manifest.json`
- `metrics.json`, `metrics.csv`
- `equity.csv`, `trades.csv`
- `report.html`

The stable run ID is derived from canonical configuration, a full normalized input hash (present OHLCV/adjusted columns plus index), and Git state. Retrieval timestamps are recorded but excluded from the stable ID.

## Development rules

Read `AGENTS.md` and `CLAUDE.md`. New behavior must follow strict RED-GREEN-REFACTOR using synthetic data. Do not read or print `.env`, tokens, or SSH keys. Do not push from this host.
