from __future__ import annotations

import json
import os
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Mapping

from .production_contract import canonical_json_bytes
from .production_jobs import JobComputation, ProductionJobs, ProviderClient
from .production_result import ProductionResultStore
from .production_store import ProductionStore


class SimulatedWorkerCrash(RuntimeError):
    """Test-only crash signal that preserves the durable nonterminal row."""


class ProductionWorker:
    """One durable claimant with create-once acquisition and computation stages."""

    def __init__(
        self,
        store: ProductionStore,
        jobs: ProductionJobs,
        provider: ProviderClient,
        results: ProductionResultStore,
        *,
        work_root: Path | str,
        owner: str,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        crash: Callable[[str], None] = lambda _point: None,
    ):
        self.store = store
        self.jobs = jobs
        self.provider = provider
        self.results = results
        self.work_root = Path(work_root).absolute()
        self.owner = owner
        self.clock = clock
        self.crash = crash

    @staticmethod
    def _scheduled(row: Mapping[str, Any]) -> datetime:
        return datetime.fromisoformat(row["scheduled_for"][:-1] + "+00:00")

    def _stage_root(self, row: Mapping[str, Any]) -> Path:
        return self.work_root / row["production_run_id"]

    def _write_generation(self, target: Path, payloads: Mapping[str, bytes]) -> None:
        if target.exists():
            return
        self.work_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=target.parent))
        try:
            for name, payload in payloads.items():
                with (staging / name).open("xb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                (staging / name).chmod(0o444)
            staging.chmod(0o555)
            os.rename(staging, target)
        finally:
            if staging.exists():
                staging.chmod(0o700)
                shutil.rmtree(staging)

    def _acquisition(self, row: Mapping[str, Any]) -> tuple[str, bytes]:
        target = self._stage_root(row) / "acquisition"
        if not target.exists():
            url, raw = self.jobs.acquire(row["job_id"], self.provider, self._scheduled(row))
            self._write_generation(
                target,
                {"identity.json": canonical_json_bytes({"provider_url": url}), "raw.bin": raw},
            )
        identity = json.loads((target / "identity.json").read_bytes())
        return identity["provider_url"], (target / "raw.bin").read_bytes()

    @staticmethod
    def _computation_payloads(value: JobComputation) -> dict[str, bytes]:
        identity = {
            "job_id": value.job_id,
            "model_id": value.model_id,
            "production_manifest_sha256": value.production_manifest_sha256,
            "report_uuid": value.report_uuid,
            "provider_url": value.provider_url,
            "raw_name": value.raw_name,
            "experiment_id": value.experiment_id,
            "attempt_id": value.attempt_id,
        }
        return {
            "identity.json": canonical_json_bytes(identity),
            "raw.bin": value.raw_bytes,
            "normalized.json": value.normalized_bytes,
            "action.json": canonical_json_bytes(value.action),
            "report.html": value.report_html,
            "notification.txt": value.notification_bytes,
        }

    @staticmethod
    def _load_computation(target: Path) -> JobComputation:
        identity = json.loads((target / "identity.json").read_bytes())
        return JobComputation(
            identity["job_id"],
            identity["model_id"],
            identity["production_manifest_sha256"],
            identity["report_uuid"],
            identity["provider_url"],
            identity["raw_name"],
            (target / "raw.bin").read_bytes(),
            (target / "normalized.json").read_bytes(),
            json.loads((target / "action.json").read_bytes()),
            (target / "report.html").read_bytes(),
            (target / "notification.txt").read_bytes(),
            identity["experiment_id"],
            identity["attempt_id"],
        )

    def _computation(self, row: Mapping[str, Any], url: str, raw: bytes) -> JobComputation:
        target = self._stage_root(row) / "computation"
        if not target.exists():
            computed = self.jobs.compute(row["job_id"], raw, url, self._scheduled(row))
            self._write_generation(target, self._computation_payloads(computed))
        return self._load_computation(target)

    def run_once(self) -> dict[str, Any] | None:
        row = self.store.claim(self.owner, now=self.clock())
        if row is None:
            return None
        try:
            if row["status"] == "ACCEPTED":
                row = self.store.transition(
                    row["request_id"], self.owner, "ACCEPTED", "ACQUIRING", now=self.clock()
                )
                self.crash("after_accepted")
            if row["status"] == "ACQUIRING":
                url, raw = self._acquisition(row)
                self.crash("after_acquisition_sealed")
                row = self.store.transition(
                    row["request_id"], self.owner, "ACQUIRING", "COMPUTING", now=self.clock()
                )
            else:
                url, raw = self._acquisition(row)
            if row["status"] == "COMPUTING":
                computation = self._computation(row, url, raw)
                self.crash("after_computation_sealed")
                row = self.store.transition(
                    row["request_id"], self.owner, "COMPUTING", "PUBLISHING", now=self.clock()
                )
            else:
                computation = self._computation(row, url, raw)
            manifest = self.results.publish(row, computation)
            self.crash("after_result_and_report_sealed")
            terminal = self.store.finish_success(
                row["request_id"],
                self.owner,
                experiment_id=computation.experiment_id,
                attempt_id=computation.attempt_id,
                result_id=manifest["result_id"],
                result_manifest=manifest,
                now=self.clock(),
            )
            self.crash("after_terminal_commit")
            return terminal
        except SimulatedWorkerCrash:
            raise
        except BaseException as exc:
            self.store.finish_failure(
                row["request_id"], self.owner, f"{type(exc).__name__}: {exc}", now=self.clock()
            )
            raise
