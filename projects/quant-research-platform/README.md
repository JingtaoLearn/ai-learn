# Gold Quant Research

A reproducible, research-only vertical slice for daily gold strategy analysis. It is designed for both human researchers and agents, uses transparent pandas code, and contains no broker integration or live-order path.

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
