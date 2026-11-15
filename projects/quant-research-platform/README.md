# Trend Sentinel (趋势哨兵)

Private trend-following signal system: tells you **when to buy and sell** stocks, indices and gold, and pushes the signal to Feishu after the close and the moment an intraday price crosses a trigger. Signals only — no broker, no orders.

Live at `https://quant.ai.jingtao.fun` (ms-login SSO).

## The signal

For each instrument with parameters `window`, `ema`, `buy`, `sell`:

1. Fit an OLS line to the last `window` log closes and project it one step ahead.
2. Smooth that projection with an EMA of span `ema` → the gold trend curve in the UI.
3. Slope = day-over-day change of the smoothed curve, in %/day.
4. Hysteresis: when flat, the slope crossing **up through `buy`** → BUY; when long, crossing **down through `sell`** → SELL. Decided at close t, executed at the open of t+1.

Because the slope is monotonic in the next close, the app solves in closed form for the **trigger price**: the next close that would fire the next signal. That price drives the overview cards ("+7.3% to buy"), the daily message and the intraday alerts.

`tests/test_engine.py` proves the engine is bit-for-bit equivalent to the retired production implementation (`tests/reference_v1.py`).

## Features

| Area | What it does |
|---|---|
| Overview | Written from your real position: to-do list (signals to execute or confirm, model/you divergences), your holdings with cost, P&L, days held and sell trigger, watchlist cards (slope gauge, distance to trigger), your realized trades, signal log |
| Feedback | Every BUY/SELL signal asks "did you do it?": confirm with your actual date/price, or mark it skipped. Your confirmed trades define your real position; until you record anything the model position is used |
| Detail | Candles + trend curve + buy/sell markers + holding shading, slope pane with threshold lines, strategy-vs-buy&hold equity and metrics, trade list |
| What-if | Edit parameters → charts and backtest re-run live; save to make them the signal parameters |
| Optimization | ~9.5k-combo grid on the Feng training VM, neighbour-smoothed scores (stable region, not a lucky peak), 5-fold expanding walk-forward vs current params vs buy&hold, window×EMA heatmap, one-click apply |
| Alerts | Close signal per market; intraday alert once per day per side when the live price crosses the trigger |
| Watchlist | Add A-shares (`600519`), indices (`sh000300`), SGE gold (`AU9999`), US stocks (`AAPL`) from the UI |

Data: Yahoo (stocks, dividend/split adjusted), Sina K-line (CN indices), Sina SGE history (gold), Sina hq (live quotes). Costs are bps per side per instrument.

## Topology

```
          Feishu ◄── Hermes cron (no-agent scripts) ──┐
                                                       │ POST /api/jobs/* (bearer token)
browser ─► nginx-proxy ─► quant-research-ui-proxy ──── ailearn ── ssh -L ──► zhlearn 127.0.0.1:18090
                                                       │                    quant-signal (FastAPI + SQLite)
                                                       │                         │ POST /optimize
                                            quant-train-tunnel (-L feng, -R zhlearn) :18095
                                                       └────────────────► Feng 127.0.0.1:18095
                                                                            quant-train-worker
```

- **zhlearn** (serving): `compose.yaml`, container `quant-signal`, SQLite in volume `quant-data`. Falls back to local optimization when Feng is unreachable.
- **Feng** (training, 8 cores): `compose.worker.yaml`, container `quant-train-worker`, stateless.
- **ailearn** (hub): public proxy (`vm/docker-services/quant-research-ui-proxy`), tunnels, Hermes jobs.

| Hermes job | Schedule (Asia/Shanghai) | Script |
|---|---|---|
| `quant-signal-cn-daily` | 15:40 Mon–Fri | `quant_daily_cn.py` — A-shares, CN indices, SGE gold |
| `quant-signal-us-daily` | 06:30 Tue–Sat | `quant_daily_us.py` — US stocks |
| `quant-signal-intraday-alerts` | every 10 min Mon–Sat | `quant_alerts.py` — silent unless a trigger is crossed |

Each session is reported once; a repeated run prints nothing, and Hermes stays silent on empty output.

## Deploy

Secrets live only in `~/quant-signal/.env` on zhlearn and Feng and in `~/.config/quant-signal/env` on ailearn (see `.env.example`).

```bash
deploy/deploy.sh all      # or: serve | train | hermes
```

The script ships the tree through `ailearn`, builds on zhlearn (`sudo docker compose`) and Feng, installs the Hermes scripts and the training tunnel unit.

## Develop

```bash
pip install -e '.[dev]'
pytest
QUANT_AUTH=none QUANT_DB=/tmp/quant.db uvicorn quant.app:app --port 8000
```

| Module | Role |
|---|---|
| `quant/engine.py` | slope, hysteresis positions (vectorised across thresholds), trigger price |
| `quant/backtest.py` | next-open execution, costs, metrics, trades |
| `quant/optimize.py` | grid + smoothing + walk-forward |
| `quant/data.py` | history providers, live quotes, market sessions |
| `quant/service.py` | refresh, views, daily signals, alerts, optimization dispatch |
| `quant/portfolio.py` | your real position from confirmed trades, pending confirmations, divergence |
| `quant/app.py` / `quant/worker.py` | serving app / training worker |
| `quant/static/` | dashboard (vanilla JS + vendored TradingView Lightweight Charts) |
