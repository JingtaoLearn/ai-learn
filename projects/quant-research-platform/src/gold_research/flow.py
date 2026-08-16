from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from prefect import flow, task
from prefect.runtime import flow_run

from .data import download_universe
from .run import run_research


@task(retries=2, retry_delay_seconds=10)
def acquire_data(start: str, end: str, data_dir: str):
    return download_universe(["GC=F", "GLD"], start, end, Path(data_dir))


@task
def execute_research(data, output_dir: str, cost_bps: float, tracking_uri: str | None, data_dir: str, orchestration_run_id: str):
    manifest_path = Path(data_dir) / "data_manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else None
    return run_research(data, Path(output_dir), cost_bps, tracking_uri, manifest, orchestration_run_id)


@flow(name="gold-quant-research-v1", log_prints=True)
def gold_research_flow(
    start: str = "2010-01-01",
    end: str | None = None,
    data_dir: str = "data/raw",
    output_dir: str = "runs",
    cost_bps: float = 5.0,
    tracking_uri: str | None = None,
):
    end = end or datetime.now(timezone.utc).date().isoformat()
    data = acquire_data(start, end, data_dir)
    result = execute_research(data, output_dir, cost_bps, tracking_uri, data_dir, str(flow_run.id))
    print("RESULT_JSON=" + json.dumps({"run_id": result["run_id"], "run_dir": result["run_dir"]}))
    return result


def main():
    parser = argparse.ArgumentParser(description="Run reproducible gold research")
    parser.add_argument("--start", default="2010-01-01")
    parser.add_argument("--end")
    parser.add_argument("--data-dir", default="data/raw")
    parser.add_argument("--output-dir", default="runs")
    parser.add_argument("--cost-bps", type=float, default=5.0)
    parser.add_argument("--tracking-uri", default=os.getenv("MLFLOW_TRACKING_URI"))
    args = parser.parse_args()
    gold_research_flow(**vars(args))


if __name__ == "__main__":
    main()
