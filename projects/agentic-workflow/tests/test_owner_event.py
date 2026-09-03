from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from notification.owner_event import (
    EventRequest,
    build_delivery_environment,
    emit_event,
)


def request(tmp_path: Path) -> EventRequest:
    artifact = tmp_path / "RESULT.md"
    artifact.write_text("observed result\n", encoding="utf-8")
    return EventRequest(
        product_id="quant-research",
        event_type="RESULT_READY",
        source_profile="researchagentquantresearch",
        owner_profile="productowneragentquantresearch",
        owner_session_id="owner-session-1",
        run_id="run-1",
        action_id="action-1",
        artifact_path=artifact,
        summary="Research result is ready.",
    )


def completed(session_id: str = "owner-session-1") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["hermes"],
        returncode=0,
        stdout=f"session_id: {session_id}\nOWNER_PROCESSED\n",
        stderr="",
    )


def test_persists_event_before_delivery(tmp_path: Path) -> None:
    seen: dict[str, object] = {}

    def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
        event_path = next((tmp_path / "events").glob("*/event.json"))
        seen["event"] = json.loads(event_path.read_text(encoding="utf-8"))
        seen["command"] = command
        return completed()

    result = emit_event(request(tmp_path), tmp_path / "events", runner=runner)

    assert result.status == "delivered"
    assert seen["event"]["event_type"] == "RESULT_READY"
    assert seen["command"][:4] == [
        "hermes",
        "-p",
        "productowneragentquantresearch",
        "chat",
    ]


def test_records_delivery_only_for_expected_owner_session(tmp_path: Path) -> None:
    result = emit_event(
        request(tmp_path),
        tmp_path / "events",
        runner=lambda _: completed(),
    )

    delivered = json.loads((result.event_dir / "delivered.json").read_text(encoding="utf-8"))
    assert delivered["owner_session_id"] == "owner-session-1"
    assert delivered["response_sha256"] == hashlib.sha256(
        completed().stdout.encode("utf-8")
    ).hexdigest()


def test_accepts_owner_session_id_emitted_on_stderr(tmp_path: Path) -> None:
    result = emit_event(
        request(tmp_path),
        tmp_path / "events",
        runner=lambda _: subprocess.CompletedProcess(
            args=["hermes"],
            returncode=0,
            stdout="OWNER_PROCESSED\n",
            stderr='↻ Resumed session owner-session-1 "Bot Chat"\n\nsession_id: owner-session-1\n',
        ),
    )

    assert result.status == "delivered"
    assert (result.event_dir / "delivered.json").exists()


def test_wrong_owner_session_is_a_durable_failure(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="owner session mismatch"):
        emit_event(
            request(tmp_path),
            tmp_path / "events",
            runner=lambda _: completed("new-owner-session"),
        )

    event_dir = next((tmp_path / "events").iterdir())
    attempts = list((event_dir / "attempts").glob("*.json"))
    assert len(attempts) == 1
    assert not (event_dir / "delivered.json").exists()
    assert json.loads(attempts[0].read_text(encoding="utf-8"))["status"] == "failed"


def test_duplicate_terminal_event_does_not_trigger_owner_twice(tmp_path: Path) -> None:
    calls = 0

    def runner(_: list[str]) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        return completed()

    first = emit_event(request(tmp_path), tmp_path / "events", runner=runner)
    second = emit_event(request(tmp_path), tmp_path / "events", runner=runner)

    assert calls == 1
    assert first.event_id == second.event_id
    assert second.status == "deduplicated"


def test_artifact_content_is_part_of_event_identity(tmp_path: Path) -> None:
    first_request = request(tmp_path)
    first = emit_event(first_request, tmp_path / "events", runner=lambda _: completed())
    first_request.artifact_path.write_text("changed result\n", encoding="utf-8")
    second = emit_event(first_request, tmp_path / "events", runner=lambda _: completed())

    assert first.event_id != second.event_id


def test_nested_profile_environment_is_removed_for_owner_delivery() -> None:
    environment = build_delivery_environment(
        {
            "HOME": "/tmp/profile/home",
            "HERMES_HOME": "/tmp/profile",
            "HERMES_REAL_HOME": "/home/example",
            "PATH": "/usr/bin",
        }
    )

    assert "HERMES_HOME" not in environment
    assert environment["HOME"] == "/home/example"
    assert environment["PATH"] == "/usr/bin"


def test_rejects_non_terminal_event_type(tmp_path: Path) -> None:
    invalid = request(tmp_path)
    invalid = EventRequest(**{**invalid.__dict__, "event_type": "PROGRESS"})

    with pytest.raises(ValueError, match="unsupported terminal event type"):
        emit_event(invalid, tmp_path / "events", runner=lambda _: completed())
