"""Training worker (Feng): stateless grid + walk-forward optimization over HTTP."""
from __future__ import annotations

import hmac
import os

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from . import optimize

TOKEN = os.environ.get("QUANT_WORKER_TOKEN", "")
app = FastAPI(title="Quant training worker", docs_url=None, redoc_url=None)


class Job(BaseModel):
    dates: list[str]
    open_: list[float]
    close: list[float]
    start_index: int
    buy_bps: float
    sell_bps: float
    objective: str = "sharpe"
    current: dict | None = None
    grid: dict | None = None


@app.get("/health")
def health():
    return {"status": "ok", "cpus": os.cpu_count()}


@app.post("/optimize")
def run(job: Job, authorization: str = Header("")):
    if not TOKEN or not hmac.compare_digest(authorization, f"Bearer {TOKEN}"):
        raise HTTPException(401, "unauthorized")
    try:
        return optimize.run(**job.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc))
