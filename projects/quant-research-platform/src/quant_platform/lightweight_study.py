from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .study_remote import (
    TERMINAL_STATES,
    SignedStudyClient,
    StudyDispatcher,
    StudyPostgresStore,
    StudyRemoteError,
    StudyTransportError,
    StudyValidationError,
    freeze_market_request,
    freeze_training_request,
)

ACTION_ID = re.compile(r"^[0-9a-f]{32}$")
MAX_UI_TRIAL_BUDGET = 64


class LightweightStudyNotFound(StudyRemoteError):
    pass


@dataclass(frozen=True)
class LightweightStudyService:
    store: StudyPostgresStore
    dispatcher: StudyDispatcher
    source_commit: str
    source_tree: str
    worker_image: str
    platform: Any | None = None

    @classmethod
    def from_environment(cls) -> LightweightStudyService | None:
        if os.environ.get("QR_LIGHTWEIGHT_STUDY_ENABLED", "false").lower() != "true":
            return None
        required = {
            "endpoint": "QR_STUDY_WORKER_ENDPOINT",
            "private_key": "QR_STUDY_SIGNING_PRIVATE_KEY",
            "tls_ca_file": "QR_STUDY_TLS_CA_FILE",
            "source_commit": "QR_STUDY_SOURCE_COMMIT",
            "source_tree": "QR_STUDY_SOURCE_TREE",
            "worker_image": "QR_STUDY_WORKER_IMAGE",
        }
        values: dict[str, str] = {}
        for name, variable in required.items():
            value = os.environ.get(variable, "")
            if not value:
                raise StudyRemoteError(f"{variable} is required when lightweight Studies are enabled")
            values[name] = value
        store = StudyPostgresStore.from_environment()
        store.verify_initialized()
        client = SignedStudyClient(
            values["endpoint"],
            Path(values["private_key"]),
            tls_ca_file=Path(values["tls_ca_file"]),
        )
        from .full_persistence import FullPostgresPersistence

        return cls(
            store=store,
            dispatcher=StudyDispatcher(store, client),
            source_commit=values["source_commit"],
            source_tree=values["source_tree"],
            worker_image=values["worker_image"],
            platform=FullPostgresPersistence.from_environment(),
        )

    def submit(self, *, action_id: str, trial_budget: int) -> dict[str, Any]:
        if ACTION_ID.fullmatch(action_id) is None:
            raise StudyValidationError("lightweight Study action identity is invalid")
        if type(trial_budget) is not int or not 1 <= trial_budget <= MAX_UI_TRIAL_BUDGET:
            raise StudyValidationError(
                f"trial_budget must be between 1 and {MAX_UI_TRIAL_BUDGET}"
            )
        request = freeze_training_request(
            trial_budget=trial_budget,
            seed=int(action_id[:8], 16),
            checkpoint_count=min(8, trial_budget),
            parameter_low=0.0,
            parameter_high=1.0,
            objective_target=0.61803398875,
            source_commit=self.source_commit,
            source_tree=self.source_tree,
            worker_image=self.worker_image,
        )
        result = self.dispatcher.submit(request)
        return self._view(result["authoritative"])

    def submit_msft(self, *, action_id: str, snapshot_id: str) -> dict[str, Any]:
        if ACTION_ID.fullmatch(action_id) is None:
            raise StudyValidationError("lightweight Study action identity is invalid")
        if self.platform is None:
            raise StudyRemoteError("PostgreSQL MSFT Snapshot authority is unavailable")
        snapshot = self.platform.msft_snapshot(snapshot_id)
        request = freeze_market_request(
            snapshot=snapshot,
            source_commit=self.source_commit,
            source_tree=self.source_tree,
            worker_image=self.worker_image,
        )
        result = self.dispatcher.submit(request)
        return self._view(result["authoritative"])

    def detail(self, study_id: str) -> dict[str, Any]:
        row = self.store.get(study_id)
        if row is None:
            raise LightweightStudyNotFound(f"unknown lightweight Study: {study_id}")
        sync_error = None
        if row["status"] not in TERMINAL_STATES:
            try:
                row = self.dispatcher.read(study_id)["authoritative"]
            except StudyTransportError:
                sync_error = (
                    "Feng read-back is currently unavailable. "
                    "This page shows the last authoritative PostgreSQL checkpoint."
                )
        return self._view(row, sync_error=sync_error)

    def report(self, study_id: str) -> dict[str, Any]:
        detail = self.detail(study_id)
        if detail["status"] != "SUCCEEDED" or detail["result"] is None:
            raise StudyRemoteError("MSFT Study report is unavailable before successful read-back")
        if not detail["kind"].startswith("MSFT_") or self.platform is None:
            raise StudyRemoteError("canonical report is available only for the MSFT market Study")
        return self.platform.publish_msft_study_report(
            study_id=study_id,
            result=detail["result"],
            provenance={
                "source_commit": detail["source_commit"],
                "source_tree": detail["source_tree"],
                "worker_image": detail["worker_image"],
                "snapshot_id": detail["snapshot_id"],
                "classification": detail["classification"],
            },
        )

    @staticmethod
    def _view(row: dict[str, Any], *, sync_error: str | None = None) -> dict[str, Any]:
        request = row["frozen_request"]
        market = request.get("job_type") == "xnys-msft-trend-study-v1"
        spec = request.get("training_spec")
        market_snapshot = request.get("snapshot") if market else None
        classification = (
            market_snapshot.get("classification")
            if isinstance(market_snapshot, dict)
            else "IMMUTABLE_XNYS_TOTAL_RETURN_SNAPSHOT"
            if market
            else "SYNTHETIC_NON_MARKET"
        )
        return {
            "study_id": row["study_id"],
            "status": row["status"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "worker_endpoint": row["worker_endpoint"],
            "worker_image": row["worker_image"],
            "source_commit": row["source_commit"],
            "source_tree": row["source_tree"],
            "kind": (
                "MSFT_YAHOO_ADJUSTED_OHLC_PROXY"
                if market and isinstance(market_snapshot, dict) and market_snapshot.get("classification")
                else "MSFT_MARKET"
                if market
                else "SYNTHETIC_TRAINING"
            ),
            "classification": classification,
            "objective": (
                {"data_classification": classification}
                if market
                else spec["objective"]
            ),
            "trial_budget": 15 if market else spec["search"]["trial_budget"],
            "snapshot_id": request["snapshot"]["snapshot_id"] if market else None,
            "progress": row.get("latest_progress"),
            "result": row.get("final_result"),
            "failure": row.get("failure"),
            "sync_error": sync_error,
            "local_compute_attempted": False,
        }
