from __future__ import annotations

import os
import shutil
import stat
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Mapping

from .production_contract import SHA256
from .production_jobs import (
    ProductionComputation,
    ProductionInput,
    ProductionJobError,
    ProductionJobs,
    ProviderClient,
    staged_package_identity,
)
from .production_result import ProductionResultStore
from .production_store import ProductionStore


class SimulatedWorkerCrash(RuntimeError):
    """Test-only crash signal that preserves the durable nonterminal row."""


def _stat_fingerprint(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_uid,
        value.st_gid,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


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
    def _scheduled(row: Mapping[str, Any]) -> datetime | None:
        request = row.get("request_body")
        if isinstance(request, Mapping) and request.get("operation") is not None:
            return None
        return datetime.fromisoformat(row["scheduled_for"][:-1] + "+00:00")

    def _stage_root(self, row: Mapping[str, Any]) -> Path:
        return self.work_root / row["production_run_id"]

    @staticmethod
    def _generation_identity_path(target: Path) -> Path:
        return target.with_name(f".{target.name}.package-sha256")

    @classmethod
    def _read_generation_identity(cls, target: Path) -> str:
        path = cls._generation_identity_path(target)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
            try:
                before = os.fstat(descriptor)
                path_before = os.stat(path, follow_symlinks=False)
                payload = os.read(descriptor, 66)
                after = os.fstat(descriptor)
                path_after = os.stat(path, follow_symlinks=False)
            finally:
                os.close(descriptor)
        except OSError as exc:
            raise ProductionJobError("staged package identity is unavailable") from exc
        fingerprint = _stat_fingerprint(before)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_mode & 0o222
            or fingerprint != _stat_fingerprint(path_before)
            or fingerprint != _stat_fingerprint(after)
            or fingerprint != _stat_fingerprint(path_after)
        ):
            raise ProductionJobError("staged package identity is unsafe")
        try:
            identity = payload.decode("ascii").removesuffix("\n")
        except UnicodeError as exc:
            raise ProductionJobError("staged package identity is invalid") from exc
        if len(payload) != 65 or not payload.endswith(b"\n") or SHA256.fullmatch(identity) is None:
            raise ProductionJobError("staged package identity is invalid")
        return identity

    def _write_generation(self, target: Path, payloads: Mapping[str, bytes]) -> str:
        if target.exists():
            return self._read_generation_identity(target)
        payloads = dict(payloads)
        package_identity = staged_package_identity(payloads)
        self.work_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=target.parent))
        identity_path = self._generation_identity_path(target)
        identity_staging: Path | None = None
        try:
            for name, payload in payloads.items():
                with (staging / name).open("xb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                (staging / name).chmod(0o444)
            staging.chmod(0o555)
            if identity_path.exists():
                if self._read_generation_identity(target) != package_identity:
                    raise ProductionJobError("staged package identity conflicts with generation")
            else:
                descriptor, temporary = tempfile.mkstemp(
                    prefix=f".{target.name}.package-sha256.", dir=target.parent
                )
                identity_staging = Path(temporary)
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(package_identity.encode("ascii") + b"\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                identity_staging.chmod(0o444)
                os.rename(identity_staging, identity_path)
                identity_staging = None
            os.rename(staging, target)
            return package_identity
        finally:
            if identity_staging is not None and identity_staging.exists():
                identity_staging.chmod(0o600)
                identity_staging.unlink()
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
            package_identity = self._read_generation_identity(target)
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
            package_identity = self._read_generation_identity(target)
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
