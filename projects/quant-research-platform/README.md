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
./scripts/manage.sh infra-down
```

The `Makefile` is a convenience alias for hosts that already have GNU Make; the shell
entry point above is canonical and needs no package beyond Bash, Python, and Docker Compose.

A Yahoo/network failure raises `DataDownloadError` with the affected symbol and underlying HTTP/parse detail. Online acquisition is intentionally excluded from unit tests.

## Artifacts

Raw inputs live under `data/raw/` and complete research runs under `runs/<stable-run-id>/`:

- `config.json`, `run_manifest.json`
- `metrics.json`, `metrics.csv`
- `equity.csv`, `trades.csv`
- `report.html`

The stable run ID is derived from canonical configuration, a full normalized input hash (present OHLCV/adjusted columns plus index), and Git state. Retrieval timestamps are recorded but excluded from the stable ID.

## Development rules

Read `AGENTS.md` and `CLAUDE.md`. New behavior must follow strict RED-GREEN-REFACTOR using synthetic data. Do not read or print `.env`, tokens, or SSH keys. Do not push from this host.
