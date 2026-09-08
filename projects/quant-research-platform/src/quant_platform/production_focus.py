from __future__ import annotations

import hashlib
from pathlib import Path

from gold_research.focus_runner import _calibration_authority, calibrate_once

from .production_contract import (
    FOCUS_CALIBRATION_JOB_ID,
    FOCUS_CALIBRATION_OPERATION,
    SHA256,
    canonical_json_bytes,
)
from .production_jobs import FormalComputation, ProductionInput, ProductionJobError


class FocusCalibrationProductionJob:
    """Production-owned, no-network adapter for the frozen FOCuS calibration."""

    job_id = FOCUS_CALIBRATION_JOB_ID
    operation = FOCUS_CALIBRATION_OPERATION

    def __init__(self, authority_path: Path | str, output_root: Path | str):
        self.authority_path = Path(authority_path).absolute()
        self.output_root = Path(output_root).absolute()
        _authority, self.production_manifest_sha256 = _calibration_authority(
            self.authority_path
        )

    def acquire_no_network(self, request_id: str) -> ProductionInput:
        if not isinstance(request_id, str) or SHA256.fullmatch(request_id) is None:
            raise ProductionJobError("FOCuS calibration request identity is invalid")
        payload = self.authority_path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != self.production_manifest_sha256:
            raise ProductionJobError("FOCuS calibration authority changed after admission")
        return ProductionInput(
            "no-network-operation",
            {
                "operation": self.operation,
                "request_id": request_id,
                "authority_sha256": self.production_manifest_sha256,
                "network_access": False,
            },
            payload,
        )

    def compute_no_network(
        self, value: ProductionInput, request_id: str
    ) -> FormalComputation:
        expected_identity = {
            "operation": self.operation,
            "request_id": request_id,
            "authority_sha256": self.production_manifest_sha256,
            "network_access": False,
        }
        if (
            value.kind != "no-network-operation"
            or dict(value.identity) != expected_identity
            or hashlib.sha256(value.payload).hexdigest()
            != self.production_manifest_sha256
            or value.payload != self.authority_path.read_bytes()
        ):
            raise ProductionJobError("FOCuS calibration input identity does not verify")
        result = calibrate_once(
            self.authority_path, self.output_root, claim_identity=request_id
        )
        calibration = self.output_root / "synthetic-calibration" / "calibration.json"
        ledger = self.output_root / "phase-ledger"
        files = {
            "calibration.json": calibration.read_bytes(),
            "03-CALIBRATION_CLAIMED.json": (
                ledger / "03-CALIBRATION_CLAIMED.json"
            ).read_bytes(),
            "04-CALIBRATION_SEALED.json": (
                ledger / "04-CALIBRATION_SEALED.json"
            ).read_bytes(),
        }
        if hashlib.sha256(files["calibration.json"]).hexdigest() != result[
            "evidence_sha256"
        ]:
            raise ProductionJobError("FOCuS calibration seal changed before result staging")
        experiment_id = hashlib.sha256(
            b"quantresearch-production-focus-calibration/v1\0"
            + self.production_manifest_sha256.encode("ascii")
        ).hexdigest()
        attempt_id = hashlib.sha256(
            b"quantresearch-production-formal-attempt/v1\0"
            + canonical_json_bytes(
                {
                    "experiment_id": experiment_id,
                    "request_id": request_id,
                    "calibration_sha256": result["evidence_sha256"],
                }
            )
        ).hexdigest()
        return FormalComputation(
            job_id=self.job_id,
            production_manifest_sha256=self.production_manifest_sha256,
            operation=self.operation,
            authority_sha256=self.production_manifest_sha256,
            files=files,
            experiment_id=experiment_id,
            attempt_id=attempt_id,
        )
