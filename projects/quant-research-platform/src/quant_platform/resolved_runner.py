from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .catalog import Catalog
from .isolation import build_composed_execution_command
from .experiment_service import ExperimentService
from .runner import RunnerTerminationError, _terminate_container, reconcile_container
from .schemas import canonical_json_bytes
from .seed import BUILTINS
from .strategy_runner import run_strategy_config


class ResolvedExecutionError(RuntimeError):
    """Raised when a resolved experiment cannot use the proven strategy runner."""


class ResolvedTerminationUnconfirmed(ResolvedExecutionError):
    termination_unconfirmed = True


BUILTIN_OPERATORS = {
    "fit": "prior_log_ols",
    "smoothing": "recursive_log_ema",
    "statistic": "adjacent_curve_pct_slope",
    "decision": "post_start_threshold_crossing_hysteresis",
    "sizing": "all_in_all_out_a_share_lots",
    "cost": "cms_china_a_share",
    "report": "concise_chinese_causal_trade",
}
BUILTIN_DEFAULTS = {item["slot"]: item["defaults"] for item in BUILTINS}
RESULT_FILES = (
    "daily_replay.csv",
    "events.csv",
    "trades.csv",
    "metrics.json",
    "cost_breakdown.json",
)


def build_legacy_config(
    resolved: dict[str, Any],
    *,
    state_root: Path,
    output_root: Path,
) -> dict[str, Any]:
    operators: dict[str, Any] = {}
    for slot, builtin_id in BUILTIN_OPERATORS.items():
        operator = resolved["operators"][slot]
        is_builtin = (
            operator["operator_id"] == builtin_id
            and operator["resolved_version"] == "1.0.0"
        )
        operators[slot] = {
            "name": builtin_id,
            "version": "1",
            "parameters": (
                operator["parameters"] if is_builtin else BUILTIN_DEFAULTS[slot]
            ),
        }
    return {
        "schema_version": 1,
        "dataset": {
            "root": str(state_root),
            "instrument": resolved["dataset"]["instrument"],
            "snapshot_id": resolved["dataset"]["snapshot_id"],
        },
        "output_root": str(output_root),
        "template": {
            "name": resolved["template"]["name"],
            "version": resolved["template"]["version"],
            "parameters": resolved["template"]["parameters"],
        },
        "operators": operators,
    }


def _result_digest(run_path: Path) -> str:
    digest = hashlib.sha256()
    for name in RESULT_FILES:
        path = run_path / name
        if path.is_symlink() or not path.is_file():
            raise ResolvedExecutionError(f"resolved run artifact is unavailable: {name}")
        payload = path.read_bytes()
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(payload)
        digest.update(b"\0")
    return digest.hexdigest()


class ResolvedAttemptExecutor:
    def __init__(
        self,
        catalog: Catalog,
        *,
        output_root: Path,
        project_root: Path | None = None,
        runner_image: str | None = None,
        attempt_controller: ExperimentService | None = None,
        process_launcher: Any = subprocess.Popen,
        container_terminator: Any = _terminate_container,
        container_reconciler: Any = reconcile_container,
    ):
        self.catalog = catalog
        self.output_root = Path(output_root)
        self.project_root = project_root
        self.runner_image = runner_image
        self.attempt_controller = attempt_controller
        self.process_launcher = process_launcher
        self.container_terminator = container_terminator
        self.container_reconciler = container_reconciler

    def _verify_resolution(self, resolved: dict[str, Any]) -> None:
        for slot, operator in resolved["operators"].items():
            try:
                published = self.catalog.operator_detail(
                    operator["operator_id"], operator["resolved_version"]
                )
            except ValueError as exc:
                raise ResolvedExecutionError(str(exc)) from exc
            if published["slot"] != slot:
                raise ResolvedExecutionError(
                    f"resolved operator slot mismatch for {operator['operator_id']}"
                )
            if published["content_digest"] != operator["content_digest"]:
                raise ResolvedExecutionError(
                    f"resolved operator digest mismatch for {operator['operator_id']}"
                )

    def _publish_audit(
        self, attempt: dict[str, Any], run: dict[str, Any], result_digest: str
    ) -> None:
        root = self.catalog.state_root / "attempt-audit"
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = root / f"{attempt['attempt_id']}.json"
        audit = {
            "schema_version": 1,
            "attempt_id": attempt["attempt_id"],
            "experiment_id": attempt["experiment_id"],
            "requested": attempt["requested"],
            "template": attempt["resolved"]["template"],
            "dataset": attempt["resolved"]["dataset"],
            "operators": attempt["resolved"]["operators"],
            "execution_identity": attempt["resolved"]["execution_identity"],
            "run_id": run["run_id"],
            "result_path": run["path"],
            "result_digest": result_digest,
        }
        payload = (
            json.dumps(
                audit,
                indent=2,
                sort_keys=True,
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
            + b"\n"
        )
        if target.exists():
            if target.is_symlink() or target.read_bytes() != payload:
                raise ResolvedExecutionError(
                    f"immutable attempt audit conflicts: {attempt['attempt_id']}"
                )
            return
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{attempt['attempt_id']}.", dir=root
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.chmod(0o444)
            os.rename(temporary, target)
        finally:
            if temporary.exists():
                temporary.unlink()

    def __call__(self, attempt: dict[str, Any]) -> dict[str, str]:
        resolved = attempt["resolved"]
        self._verify_resolution(resolved)
        custom_slots = {
            slot
            for slot, operator in resolved["operators"].items()
            if (
                operator["operator_id"] != BUILTIN_OPERATORS[slot]
                or operator["resolved_version"] != "1.0.0"
            )
        }
        if custom_slots:
            return self._run_composed(attempt, custom_slots)
        legacy = build_legacy_config(
            resolved,
            state_root=self.catalog.state_root,
            output_root=self.output_root,
        )
        work_root = self.catalog.state_root / "work"
        work_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, config_name = tempfile.mkstemp(
            prefix=".resolved-", suffix=".json", dir=work_root
        )
        config_path = Path(config_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(canonical_json_bytes(legacy))
                stream.flush()
                os.fsync(stream.fileno())
            run = run_strategy_config(config_path, project_root=self.project_root)
        finally:
            config_path.unlink(missing_ok=True)
        run_path = Path(run["path"])
        result_digest = _result_digest(run_path)
        self._publish_audit(attempt, run, result_digest)
        return {
            "result_path": str(run_path),
            "result_digest": result_digest,
            "logs": f"Resolved strategy run status: {run['status']}",
        }

    def _run_composed(
        self, attempt: dict[str, Any], custom_slots: set[str]
    ) -> dict[str, str]:
        if self.runner_image is None:
            raise ResolvedExecutionError(
                "a pinned runner image is required for custom composition"
            )
        if self.attempt_controller is None:
            raise ResolvedExecutionError(
                "attempt controller is required for custom composition"
            )
        resolved = attempt["resolved"]
        composition_digest = hashlib.sha256(
            canonical_json_bytes(
                {
                    "experiment_id": attempt["experiment_id"],
                    "operators": resolved["operators"],
                    "execution_identity": resolved["execution_identity"],
                }
            )
        ).hexdigest()
        legacy = build_legacy_config(
            resolved,
            state_root=Path("/platform"),
            output_root=Path("/artifacts"),
        )
        self.output_root.mkdir(parents=True, exist_ok=True)
        work_root = self.catalog.state_root / "work"
        work_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        operator_bundles: dict[str, Path] = {}
        composition_operators: dict[str, Any] = {}
        for slot in sorted(custom_slots):
            operator = resolved["operators"][slot]
            detail = self.catalog.operator_detail(
                operator["operator_id"], operator["resolved_version"]
            )
            operator_bundles[slot] = self.catalog.state_root / detail["bundle_path"]
            composition_operators[slot] = {
                "bundle_path": f"/operators/{slot}",
                "parameters": operator["parameters"],
                "content_digest": operator["content_digest"],
                "evidence_digest": hashlib.sha256(
                    canonical_json_bytes(detail["validation_evidence"])
                ).hexdigest(),
            }
        composition = {
            "schema_version": 1,
            "composition_digest": composition_digest,
            "operators": composition_operators,
        }
        config_descriptor, config_name = tempfile.mkstemp(
            prefix=".composition-config-", suffix=".json", dir=work_root
        )
        composition_descriptor, composition_name = tempfile.mkstemp(
            prefix=".composition-", suffix=".json", dir=work_root
        )
        config_path = Path(config_name)
        composition_path = Path(composition_name)
        control_path = attempt.get("control_path")
        if control_path != f"attempt-control/{attempt['attempt_id']}":
            raise ResolvedExecutionError("attempt control path is invalid")
        control_dir = self.catalog.state_root / control_path
        if control_dir.is_symlink() or not control_dir.is_dir():
            raise ResolvedExecutionError("attempt control directory is unsafe")
        cidfile = control_dir / "container.cid"
        stdout_path = control_dir / "stdout.log"
        stderr_path = control_dir / "stderr.log"
        try:
            with os.fdopen(config_descriptor, "wb") as stream:
                stream.write(canonical_json_bytes(legacy))
            with os.fdopen(composition_descriptor, "wb") as stream:
                stream.write(canonical_json_bytes(composition))
            dataset = resolved["dataset"]
            dataset_dir = (
                self.catalog.state_root
                / "datasets"
                / dataset["instrument"]
                / dataset["snapshot_id"]
            )
            command = build_composed_execution_command(
                dataset_dir=dataset_dir,
                output_root=self.output_root,
                composition_file=composition_path,
                config_file=config_path,
                cidfile=cidfile,
                operator_bundles=operator_bundles,
                runner_image=self.runner_image,
            )
            container_name = command[command.index("--name") + 1]
            self.attempt_controller.record_physical_launch(
                attempt["attempt_id"], container_name=container_name
            )
            with stdout_path.open("xb") as stdout, stderr_path.open("xb") as stderr:
                try:
                    process = self.process_launcher(
                        command,
                        stdin=subprocess.DEVNULL,
                        stdout=stdout,
                        stderr=stderr,
                        shell=False,
                        env={"PATH": "/usr/local/bin:/usr/bin:/bin"},
                        start_new_session=True,
                        close_fds=True,
                    )
                except OSError as exc:
                    self.attempt_controller.record_termination(
                        attempt["attempt_id"],
                        exit_status=None,
                        outcome="LAUNCH_FAILED",
                    )
                    raise ResolvedExecutionError(
                        "custom composition process launch failed"
                    ) from exc
                try:
                    exit_status = process.wait(timeout=600)
                except subprocess.TimeoutExpired as exc:
                    try:
                        terminated_status = self.container_terminator(
                            cidfile, container_name, process
                        )
                    except RunnerTerminationError as termination_error:
                        raise ResolvedTerminationUnconfirmed(
                            "custom composition termination was not confirmed"
                        ) from termination_error
                    self.attempt_controller.record_termination(
                        attempt["attempt_id"],
                        exit_status=terminated_status,
                        outcome="TIMED_OUT",
                    )
                    raise ResolvedExecutionError(
                        "custom composition timed out after confirmed termination"
                    ) from exc
                if not self.container_reconciler(cidfile):
                    raise ResolvedTerminationUnconfirmed(
                        "custom composition container absence was not confirmed"
                    )
                self.attempt_controller.record_termination(
                    attempt["attempt_id"],
                    exit_status=exit_status,
                    outcome="SUCCEEDED" if exit_status == 0 else "FAILED",
                )
                for stream in (stdout, stderr):
                    stream.flush()
                    os.fsync(stream.fileno())
            stdout_payload = stdout_path.read_bytes()
            if exit_status != 0:
                raise ResolvedExecutionError(
                    "custom composition worker exited unsuccessfully"
                )
        finally:
            config_path.unlink(missing_ok=True)
            composition_path.unlink(missing_ok=True)
        lines = stdout_payload.splitlines()
        if len(stdout_payload) > 1_048_576 or len(lines) != 1:
            raise ResolvedExecutionError("custom composition launch failed")
        try:
            result = json.loads(lines[0])
        except json.JSONDecodeError as exc:
            raise ResolvedExecutionError(
                "custom composition returned invalid JSON"
            ) from exc
        if result.get("ok") is not True or not isinstance(result.get("run_id"), str):
            raise ResolvedExecutionError(
                f"custom composition failed: {result.get('error', 'unknown error')}"
            )
        run_path = self.output_root / result["run_id"]
        try:
            manifest = json.loads(
                (run_path / "run_manifest.json").read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            raise ResolvedExecutionError(
                "custom composition result manifest is invalid"
            ) from exc
        if (
            manifest.get("run_id") != result["run_id"]
            or manifest.get("identity", {}).get("composition_digest")
            != composition_digest
        ):
            raise ResolvedExecutionError(
                "custom composition result identity does not match its launch"
            )
        result_digest = _result_digest(run_path)
        run = result | {"path": str(run_path)}
        self._publish_audit(attempt, run, result_digest)
        return {
            "result_path": str(run_path),
            "result_digest": result_digest,
            "logs": f"Resolved custom composition status: {result['status']}",
        }
