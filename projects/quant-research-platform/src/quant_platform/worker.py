from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .experiment_service import ExperimentService


class SerialAttemptWorker:
    """Claim and execute at most one attempt at a time."""

    def __init__(
        self,
        service: ExperimentService,
        *,
        executor: Callable[[dict[str, Any]], dict[str, str]],
    ):
        self.service = service
        self.executor = executor

    def run_once(self) -> bool:
        attempt = self.service.claim_next_attempt()
        if attempt is None:
            return False
        try:
            result = self.executor(attempt)
            self.service.finish_success(
                attempt["attempt_id"],
                result_path=result["result_path"],
                result_digest=result["result_digest"],
                logs=result.get("logs", ""),
            )
        except Exception as exc:
            self.service.finish_failure(
                attempt["attempt_id"], f"{type(exc).__name__}: {exc}"
            )
        return True
