from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .catalog import Catalog
from .schemas import canonical_json_bytes
from .strategy_runner import run_strategy_config


class ResolvedExecutionError(RuntimeError):
    """Raised when a resolved experiment cannot use the proven strategy runner."""


BUILTIN_OPERATORS = {
    "fit": "prior_log_ols",
    "smoothing": "recursive_log_ema",
    "statistic": "adjacent_curve_pct_slope",
    "decision": "post_start_threshold_crossing_hysteresis",
    "sizing": "all_in_all_out_a_share_lots",
    "cost": "cms_china_a_share",
    "report": "concise_chinese_causal_trade",
}
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
        if (
            operator["operator_id"] != builtin_id
            or operator["resolved_version"] != "1.0.0"
        ):
            raise ResolvedExecutionError(
                f"isolated custom {slot} execution is required for "
                f"{operator['operator_id']}@{operator['resolved_version']}"
            )
        operators[slot] = {
            "name": builtin_id,
            "version": "1",
            "parameters": operator["parameters"],
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
    ):
        self.catalog = catalog
        self.output_root = Path(output_root)
        self.project_root = project_root

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
