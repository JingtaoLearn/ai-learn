from __future__ import annotations

from collections.abc import Callable
import time
from typing import Any

from .experiment_service import ExperimentService
from .parameter_study import ParameterStudy


class SerialStudyWorker:
    """Advance at most one privately discovered Parameter Study."""

    def __init__(
        self,
        service: ParameterStudy,
        *,
        idle_poll_seconds: float = 0.0,
        monotonic_clock: Callable[[], float] | None = None,
    ):
        if idle_poll_seconds < 0:
            raise ValueError("idle_poll_seconds must not be negative")
        self.service = service
        self.idle_poll_seconds = idle_poll_seconds
        self.monotonic_clock = monotonic_clock or time.monotonic
        self._next_poll_at = 0.0

    def run_once(self) -> bool:
        now = self.monotonic_clock()
        if now < self._next_poll_at:
            return False
        progressed = self.service._advance_next_runnable() is not None
        self._next_poll_at = now if progressed else now + self.idle_poll_seconds
        return progressed


class SerialAttemptWorker:
    """Claim and execute at most one attempt at a time."""

    def __init__(
        self,
        service: ExperimentService,
        *,
        executor: Callable[[dict[str, Any]], dict[str, Any]],
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
                postgres_publication=result.get("_postgres_publication"),
            )
        except Exception as exc:
            logs = f"{type(exc).__name__}: {exc}"
            if getattr(exc, "attempt_finalized", False):
                pass
            elif getattr(exc, "termination_unconfirmed", False):
                self.service.mark_termination_unconfirmed(
                    attempt["attempt_id"], logs=logs
                )
            else:
                self.service.finish_failure(attempt["attempt_id"], logs)
        return True
