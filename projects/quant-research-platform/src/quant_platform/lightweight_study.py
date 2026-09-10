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
        return cls(
            store=store,
            dispatcher=StudyDispatcher(store, client),
            source_commit=values["source_commit"],
            source_tree=values["source_tree"],
            worker_image=values["worker_image"],
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
            seed=int(action_id[:13], 16),
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

    @staticmethod
    def _view(row: dict[str, Any], *, sync_error: str | None = None) -> dict[str, Any]:
        request = row["frozen_request"]
        spec = request["training_spec"]
        return {
            "study_id": row["study_id"],
            "status": row["status"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "worker_endpoint": row["worker_endpoint"],
            "worker_image": row["worker_image"],
            "source_commit": row["source_commit"],
            "source_tree": row["source_tree"],
            "objective": spec["objective"],
            "trial_budget": spec["search"]["trial_budget"],
            "progress": row.get("latest_progress"),
            "result": row.get("final_result"),
            "failure": row.get("failure"),
            "sync_error": sync_error,
            "local_compute_attempted": False,
        }
