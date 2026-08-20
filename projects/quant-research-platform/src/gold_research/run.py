from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

from .backtest import backtest, metrics, trade_ledger
from .strategies import donchian_signal, strategy_signals


def canonical_hash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def stable_run_id(config: dict, data_hash: str, git_state: str) -> str:
    return canonical_hash({"config": config, "data_hash": data_hash, "git_state": git_state})[:16]


def _source_tree_hash(root: Path | str = Path(".")) -> str:
    root = Path(root).resolve()
    candidates = ["src", "tests", "scripts"]
    explicit = [
        ".dockerignore",
        "compose.yaml",
        "Dockerfile",
        "Makefile",
        "pyproject.toml",
        "requirements.in",
        "requirements.lock",
    ]
    files = []
    ignored_parts = {"__pycache__", ".pytest_cache", ".ruff_cache"}
    for name in candidates:
        for path in (root / name).rglob("*"):
            is_runtime_cache = bool(ignored_parts.intersection(path.parts))
            is_bytecode = path.suffix in {".pyc", ".pyo"}
            if path.is_file() and not is_runtime_cache and not is_bytecode:
                files.append(path)
    files.extend(path for name in explicit if (path := root / name).is_file())
    files = sorted(set(files), key=lambda path: str(path.relative_to(root)))
    if not files:
        raise RuntimeError(f"no effective source files found under {root}")
    digest = hashlib.sha256()
    for path in files:
        relative = str(path.relative_to(root))
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def get_git_state(root: Path | str = Path(".")) -> dict:
    root = Path(root).resolve()

    def command(*args):
        try:
            result = subprocess.run(["git", *args], cwd=root, text=True, capture_output=True, check=False)
        except FileNotFoundError:
            return None
        return result.stdout.strip() if result.returncode == 0 else None

    commit = command("rev-parse", "HEAD")
    branch = command("branch", "--show-current")
    status = command("status", "--porcelain")
    return {
        "commit": commit or "unavailable",
        "branch": branch or "unavailable",
        "dirty": bool(status) if status is not None else None,
        "source_hash": _source_tree_hash(root),
        "provenance_mode": "git+source-tree" if commit else "source-tree",
    }


def _redacted_tracking_uri(uri: str | None) -> str:
    if not uri:
        return "disabled"
    parts = urlsplit(uri)
    hostname = parts.hostname or ""
    netloc = hostname
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def _normalize_data(data: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    normalized = {}
    for symbol, frame in data.items():
        item = frame.copy()
        if "Date" in item.columns:
            item["Date"] = pd.to_datetime(item["Date"])
            item = item.set_index("Date")
        item.index = pd.to_datetime(item.index)
        required = [column for column in ["Open", "Close"] if column in item.columns]
        if required != ["Open", "Close"]:
            raise ValueError(f"{symbol} requires Open and Close columns")
        normalized[symbol] = item.sort_index().dropna(subset=required)
    return normalized


def _data_hash(data: dict[str, pd.DataFrame]) -> str:
    digest = hashlib.sha256()
    canonical = ["Open", "High", "Low", "Close", "Adj Close", "Volume"]
    for symbol in sorted(data):
        digest.update(symbol.encode())
        frame = data[symbol].sort_index()
        columns = [column for column in canonical if column in frame.columns]
        digest.update(frame.loc[:, columns].to_csv(index=True, float_format="%.17g", na_rep="NA").encode())
    return digest.hexdigest()


def _safe_metrics(result: pd.DataFrame) -> dict:
    if len(result) < 2:
        return {}
    return metrics(result)


def _chart(equity: pd.DataFrame, trades: pd.DataFrame, symbol: str, cost_bps: float) -> str:
    fig, axes = plt.subplots(2, 1, figsize=(10, 7), constrained_layout=True)
    subset = equity[equity["symbol"] == symbol]
    for name, group in subset.groupby("strategy"):
        axes[0].plot(pd.to_datetime(group["date"]), group["equity_net"], label=name, linewidth=1.4)
    axes[0].set_title(f"{symbol}: net equity ({cost_bps:g} bps one-way)")
    axes[0].set_ylabel("Growth of 1.0")
    axes[0].grid(alpha=.25)
    axes[0].legend(fontsize=8)
    prices = subset[subset["strategy"] == "donchian_55_20"]
    axes[1].plot(pd.to_datetime(prices["date"]), prices["open"], color="#27364b", linewidth=1)
    ledger = trades[(trades["symbol"] == symbol) & (trades["strategy"] == "donchian_55_20")]
    if not ledger.empty:
        axes[1].scatter(pd.to_datetime(ledger["entry_date"]), ledger["entry_price"], marker="^", s=25, color="#16855b", label="entry")
        closed = ledger[~ledger["is_open"].astype(bool)]
        if not closed.empty:
            axes[1].scatter(pd.to_datetime(closed["exit_date"]), closed["exit_price"], marker="v", s=25, color="#c93f3f", label="exit")
        axes[1].legend(fontsize=8)
    axes[1].set_title("Donchian 55/20 next-open executions")
    axes[1].set_ylabel("Price")
    axes[1].grid(alpha=.25)
    buffer = io.BytesIO()
    fig.savefig(buffer, format="png", dpi=130)
    plt.close(fig)
    return base64.b64encode(buffer.getvalue()).decode()


def _render_report(run_id: str, config: dict, manifest: dict, metrics_df: pd.DataFrame, equity: pd.DataFrame, trades: pd.DataFrame) -> str:
    base = metrics_df[(metrics_df["scenario"] == "base") & (metrics_df["segment"] == "full")]
    oos = metrics_df[(metrics_df["scenario"] == "base") & (metrics_df["segment"] == "out_of_sample")]
    stress = metrics_df[(metrics_df["scenario"] == "cost_stress") & (metrics_df["segment"] == "full")]
    best = oos.sort_values("sharpe", ascending=False).iloc[0]
    comparison_cols = ["symbol", "strategy", "cagr", "cumulative_return", "max_drawdown", "sharpe", "trade_count", "open_trade_count", "market_exposure"]
    table = base[comparison_cols].copy()
    for col in ["cagr", "cumulative_return", "max_drawdown", "market_exposure"]:
        table[col] = table[col].map(lambda x: f"{x:.1%}")
    table["sharpe"] = table["sharpe"].map(lambda x: f"{x:.2f}")
    oos_table = oos[comparison_cols].copy()
    stress_table = stress[comparison_cols].copy()
    stable = metrics_df[metrics_df["scenario"] == "parameter_stability"][["symbol", "strategy", "cagr", "max_drawdown", "sharpe", "trade_count"]]
    trade_view = trades.sort_values("entry_date", ascending=False).head(30).copy()
    if not trade_view.empty:
        trade_view["net_return"] = trade_view["net_return"].map(lambda x: f"{x:.2%}")
    charts = "".join(f'<h3>{symbol}</h3><img alt="{symbol} equity and trades" src="data:image/png;base64,{_chart(equity, trades, symbol, config["cost_bps"])}">' for symbol in sorted(equity["symbol"].unique()))
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Gold research {run_id}</title><style>
:root{{--ink:#172033;--muted:#5d6878;--paper:#f4f6f8;--card:#fff;--accent:#9a6b12}}*{{box-sizing:border-box}}body{{margin:0;background:var(--paper);color:var(--ink);font:15px/1.55 system-ui,sans-serif}}main{{max-width:1080px;margin:auto;padding:20px}}h1{{font-size:30px;line-height:1.15}}h2{{margin-top:30px}}.decision{{border-left:5px solid var(--accent);background:#fff8e8;padding:16px;border-radius:8px}}.card{{background:var(--card);padding:16px;border-radius:10px;margin:14px 0;box-shadow:0 1px 4px #0001}}.scroll{{overflow-x:auto}}table{{border-collapse:collapse;width:100%;font-size:13px}}th,td{{padding:8px;border-bottom:1px solid #e1e5eb;text-align:right;white-space:nowrap}}th:first-child,td:first-child{{text-align:left}}img{{width:100%;height:auto}}code{{overflow-wrap:anywhere}}.muted{{color:var(--muted)}}
@media(max-width:390px){{main{{padding:12px}}h1{{font-size:24px}}.card,.decision{{padding:12px}}table{{font-size:12px}}th,td{{padding:6px}}}}
</style></head><body><main>
<p class="muted">DECISION-FIRST RESEARCH NOTE · {manifest['created_at'][:10]}</p><h1>Gold strategy research: first vertical slice</h1>
<section class="decision"><h2>Conclusion</h2><p><strong>Best out-of-sample Sharpe in this mechanical comparison:</strong> {best['symbol']} / {best['strategy']} ({best['sharpe']:.2f}). This is a research ranking, not a trading recommendation.</p><p>Do not deploy. The evidence uses daily Yahoo proxies, simplified one-way costs, no slippage/roll mechanics/taxes, and only one historical path.</p></section>
<section class="card"><h2>GC=F vs GLD strategy comparison ({config['cost_bps']:g} bps one-way)</h2><div class="scroll">{table.to_html(index=False, border=0)}</div></section>
<section class="card"><h2>Out-of-sample (last 30%)</h2><p>Signals use closes through day <code>t-1</code>, execute at the next available daily open <code>t</code>, and earn open-to-next-open returns. This avoids an unattainable same-close fill, but still does not model opening-auction slippage or intraday execution. The chronological split is 70% research / 30% out-of-sample.</p><div class="scroll">{oos_table.to_html(index=False, border=0, float_format=lambda x:f'{x:.4f}')}</div></section>
<section class="card"><h2>Double-cost stress ({config['stress_cost_bps']:g} bps one-way)</h2><div class="scroll">{stress_table.to_html(index=False, border=0, float_format=lambda x:f'{x:.4f}')}</div></section>
<section class="card"><h2>Donchian parameter stability</h2><p>Neighbor check: entry 50/55/60 days, exit 20 days.</p><div class="scroll">{stable.to_html(index=False, border=0, float_format=lambda x:f'{x:.4f}')}</div></section>
<section class="card"><h2>Equity and trade charts</h2>{charts}</section>
<section class="card"><h2>Recent trades (up to 30)</h2><div class="scroll">{trade_view.to_html(index=False, border=0)}</div></section>
<section class="card"><h2>Data definition and limitations</h2><ul><li><strong>GC=F:</strong> continuous gold-futures research proxy from Yahoo; it is not an executable contract and obscures roll construction.</li><li><strong>GLD:</strong> tradable ETF proxy used for cross-validation; dividends/adjustments depend on Yahoo fields.</li><li>Daily next-open model from 2010 onward; no opening-auction slippage, bid/ask spread model, market impact, financing, tax, futures margin, or contract-roll implementation.</li><li>Survivorship is not the main issue for two fixed proxies, but vendor corrections and symbol methodology can change.</li></ul></section>
<section class="card"><h2>Reproducibility metadata</h2><pre><code>{json.dumps({'run_id':run_id,'config':config,'data_hash':manifest['data_hash'],'git':manifest['git'],'data_manifest':manifest.get('data_manifest')}, indent=2)}</code></pre></section>
</main></body></html>"""


def _mlflow_payloads(rows: list[dict]) -> dict[tuple[str, str], dict[str, float]]:
    payloads: dict[tuple[str, str], dict[str, float]] = {}
    prefixes = {
        ("base", "full"): "full",
        ("base", "out_of_sample"): "oos",
        ("cost_stress", "full"): "stress",
    }
    base_strategies = {
        (row["symbol"], row["strategy"])
        for row in rows
        if row["scenario"] == "base" and row["segment"] == "full"
    }
    for key in base_strategies:
        payloads[key] = {}
    for row in rows:
        key = (row["symbol"], row["strategy"])
        prefix = prefixes.get((row["scenario"], row["segment"]))
        if key not in payloads or prefix is None:
            continue
        for metric_name, value in row.items():
            if metric_name in {"symbol", "strategy", "scenario", "segment"}:
                continue
            if isinstance(value, (int, float)) and math.isfinite(float(value)):
                payloads[key][f"{prefix}_{metric_name}"] = float(value)
    return payloads


def _log_mlflow(tracking_uri: str, run_dir: Path, rows: list[dict], run_id: str, config: dict, manifest: dict):
    import mlflow
    mlflow.set_tracking_uri(tracking_uri)
    mlflow.set_experiment("gold-quant-research")
    for (symbol, strategy), metric_payload in sorted(_mlflow_payloads(rows).items()):
        name = f"{run_id}-{symbol}-{strategy}"
        artifact = run_dir / "mlflow" / f"{symbol.replace('=', '_')}-{strategy}.json"
        artifact.parent.mkdir(exist_ok=True)
        artifact.write_text(json.dumps(metric_payload, indent=2, default=str) + "\n")
        with mlflow.start_run(run_name=name):
            mlflow.set_tags({
                "research_run_id": run_id,
                "prefect_flow_run_id": manifest.get("orchestration_run_id") or "disabled",
                "source_hash": manifest["git"]["source_hash"],
            })
            mlflow.log_params({"run_id": run_id, "symbol": symbol, "strategy": strategy, "cost_bps": config["cost_bps"], "stress_cost_bps": config["stress_cost_bps"], "split_ratio": config["split_ratio"], "data_hash": manifest["data_hash"][:16], "git_commit": manifest["git"]["commit"][:12], "source_hash": manifest["git"]["source_hash"][:16]})
            mlflow.log_metrics(metric_payload)
            mlflow.log_artifact(str(artifact), artifact_path="strategy-results")
            for filename in ["metrics.csv", "trades.csv", "report.html"]:
                mlflow.log_artifact(str(run_dir / filename), artifact_path="research-run")


def run_research(
    data: dict[str, pd.DataFrame],
    output_root: Path,
    cost_bps: float = 5.0,
    tracking_uri: str | None = None,
    data_manifest: dict | None = None,
    orchestration_run_id: str | None = None,
) -> dict:
    data = _normalize_data(data)
    config = {"version": 1, "symbols": sorted(data), "strategies": ["buy_and_hold", "sma_50_200", "donchian_55_20"], "cost_bps": cost_bps, "split_ratio": 0.7, "stress_cost_bps": cost_bps * 2, "donchian_stability_entries": [50, 55, 60], "donchian_exit": 20}
    data_hash = _data_hash(data)
    git = get_git_state()
    run_id = stable_run_id(config, data_hash, canonical_hash(git))
    run_dir = Path(output_root) / run_id
    if run_dir.exists():
        raise FileExistsError(f"immutable run already exists: {run_dir}")
    run_dir.mkdir(parents=True, exist_ok=False)
    manifest = {
        "run_id": run_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "data_hash": data_hash,
        "git": git,
        "data_manifest": data_manifest,
        "tracking_uri": _redacted_tracking_uri(tracking_uri),
        "orchestration_run_id": orchestration_run_id,
    }
    rows, equities, ledgers = [], [], []
    for symbol, frame in data.items():
        close = frame["Close"]
        open_price = frame["Open"]
        cut = int(len(close) * config["split_ratio"])
        for strategy, signal in strategy_signals(close).items():
            for segment, slc in [("full", slice(None)), ("research", slice(0, cut)), ("out_of_sample", slice(cut, None))]:
                result = backtest(open_price.iloc[slc], signal.iloc[slc], cost_bps)
                rows.append({"symbol": symbol, "strategy": strategy, "segment": segment, "scenario": "base", **_safe_metrics(result)})
                if segment == "full":
                    eq = result.reset_index(names="date")
                    eq.insert(0, "strategy", strategy)
                    eq.insert(0, "symbol", symbol)
                    equities.append(eq)
                    ledger = trade_ledger(result)
                    if not ledger.empty:
                        ledger.insert(0, "strategy", strategy)
                        ledger.insert(0, "symbol", symbol)
                        ledgers.append(ledger)
            stress_result = backtest(open_price, signal, cost_bps * 2)
            rows.append({"symbol": symbol, "strategy": strategy, "segment": "full", "scenario": "cost_stress", **_safe_metrics(stress_result)})
        for entry in config["donchian_stability_entries"]:
            result = backtest(open_price, donchian_signal(close, entry, 20), cost_bps)
            rows.append({"symbol": symbol, "strategy": f"donchian_{entry}_20", "segment": "full", "scenario": "parameter_stability", **_safe_metrics(result)})
    metrics_df = pd.DataFrame(rows)
    equity_df = pd.concat(equities, ignore_index=True)
    trades_df = pd.concat(ledgers, ignore_index=True) if ledgers else pd.DataFrame(columns=["symbol", "strategy", "entry_date", "exit_date", "entry_price", "exit_price", "net_return", "bars", "is_open"])
    validations = {"research": "first 70% chronological", "out_of_sample": "last 30% chronological", "execution": "prior-close signal, next-open fill, open-to-open return", "cost_stress": f"{cost_bps * 2:g} bps one-way", "parameter_stability": "Donchian entry 50/55/60, exit 20"}
    (run_dir / "config.json").write_text(json.dumps(config, indent=2, sort_keys=True) + "\n")
    (run_dir / "run_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True, default=str) + "\n")
    (run_dir / "metrics.json").write_text(json.dumps(rows, indent=2, default=str) + "\n")
    metrics_df.to_csv(run_dir / "metrics.csv", index=False)
    equity_df.to_csv(run_dir / "equity.csv", index=False)
    trades_df.to_csv(run_dir / "trades.csv", index=False)
    report = _render_report(run_id, config, manifest, metrics_df, equity_df, trades_df)
    (run_dir / "report.html").write_text(report)
    if tracking_uri:
        _log_mlflow(tracking_uri, run_dir, rows, run_id, config, manifest)
    return {"run_id": run_id, "run_dir": str(run_dir), "validations": validations, "metrics": rows}
