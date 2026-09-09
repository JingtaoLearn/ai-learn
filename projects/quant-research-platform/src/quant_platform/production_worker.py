from __future__ import annotations

import os
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Mapping

from .production_jobs import (
    ProductionComputation,
    ProductionInput,
    ProductionJobError,
    ProductionJobs,
    ProviderClient,
    staged_package_identity,
)
from .production_package_authority import PackageIdentityAuthorityClient
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
        package_identity_authority: PackageIdentityAuthorityClient,
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
        self.package_identity_authority = package_identity_authority
        self.work_root = Path(work_root).absolute()
        self.owner = owner
        self.clock = clock
        self.crash = crash

    @staticmethod
    def _scheduled(row: Mapping[str, Any]) -> datetime | None:
        request = row.get("request_body")
        if isinstance(request, Mapping) and request.get("operation") is not None:
            return None
        return datetime.fromisoformat(row["scheduled_for"][:-1] + "+00:00")

    def _stage_root(self, row: Mapping[str, Any]) -> Path:
        return self.work_root / row["production_run_id"]

    def _seal_generation(self, target: Path) -> str:
        try:
            relative = target.relative_to(self.work_root)
        except ValueError as exc:
            raise ProductionJobError("staged generation path is outside the work root") from exc
        if len(relative.parts) != 2:
            raise ProductionJobError("staged generation path is invalid")
        return self.package_identity_authority.seal(relative.parts[0], relative.parts[1])

    def _write_generation(self, target: Path, payloads: Mapping[str, bytes]) -> str:
        if target.exists():
            return self._seal_generation(target)
        payloads = dict(payloads)
        package_identity = staged_package_identity(payloads)
        self.work_root.mkdir(parents=True, exist_ok=True, mode=0o750)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=target.parent))
        try:
            for name, payload in payloads.items():
                with (staging / name).open("xb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                (staging / name).chmod(0o440)
            staging.chmod(0o550)
            os.rename(staging, target)
            retained = self._seal_generation(target)
            if retained != package_identity:
                raise ProductionJobError("package authority attested a different generation")
            return retained
        finally:
            if staging.exists():
                staging.chmod(0o700)
                shutil.rmtree(staging)

    def _acquisition(self, row: Mapping[str, Any]) -> ProductionInput:
        target = self._stage_root(row) / "acquisition"
        if not target.exists():
            value = self.jobs.stage_input(
                row["job_id"],
                self.provider,
                self._scheduled(row),
                request_id=row["request_id"],
            )
            package_identity = self._write_generation(
                target,
                self.jobs.input_payloads(value),
            )
        else:
            package_identity = self._seal_generation(target)
        return self.jobs.read_input(target, expected_package_identity=package_identity)

    def _computation(
        self, row: Mapping[str, Any], value: ProductionInput
    ) -> ProductionComputation:
        target = self._stage_root(row) / "computation"
        if not target.exists():
            computed = self.jobs.compute_input(
                row["job_id"],
                value,
                self._scheduled(row),
                request_id=row["request_id"],
            )
            package_identity = self._write_generation(
                target, self.jobs.computation_payloads(computed)
            )
        else:
            package_identity = self._seal_generation(target)
        return self.jobs.read_computation(
            target, expected_package_identity=package_identity
        )

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
                production_input = self._acquisition(row)
                self.crash("after_acquisition_sealed")
                row = self.store.transition(
                    row["request_id"], self.owner, "ACQUIRING", "COMPUTING", now=self.clock()
                )
            else:
                production_input = self._acquisition(row)
            if row["status"] == "COMPUTING":
                computation = self._computation(row, production_input)
                self.crash("after_computation_sealed")
                row = self.store.transition(
                    row["request_id"], self.owner, "COMPUTING", "PUBLISHING", now=self.clock()
                )
            else:
                computation = self._computation(row, production_input)
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
