from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from notification.session_send import (
    DeliveryRequest,
    build_delivery_environment,
    deliver_message,
    main,
)


def request(tmp_path: Path) -> DeliveryRequest:
    return DeliveryRequest(
        target_profile="productowneragentquantresearch",
        target_session_id="owner-session-1",
        message="RESULT_READY run-1 /tmp/RESULT.md",
        workdir=tmp_path,
    )


def completed(
    session_id: str = "owner-session-1",
    response: str = "OWNER_PROCESSED\n",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["hermes"],
        returncode=0,
        stdout=response,
        stderr=f"session_id: {session_id}\n",
    )


def test_delivers_to_exact_profile_and_session(tmp_path: Path) -> None:
    seen: list[str] = []

    def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
        seen.extend(command)
        return completed()

    result = deliver_message(request(tmp_path), runner=runner)

    assert seen[:4] == ["hermes", "-p", "productowneragentquantresearch", "chat"]
    assert seen[seen.index("--resume") + 1] == "owner-session-1"
    assert "-c" not in seen
    assert "--create-if-missing" not in seen
    assert result.response == "OWNER_PROCESSED\n"


def test_message_is_written_privately_and_removed_after_delivery(tmp_path: Path) -> None:
    observed: dict[str, object] = {}

    def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
        message_path = Path(command[command.index("--query-file") + 1])
        observed["path"] = message_path
        observed["content"] = message_path.read_text(encoding="utf-8")
        observed["mode"] = stat.S_IMODE(message_path.stat().st_mode)
        return completed()

    deliver_message(request(tmp_path), runner=runner)

    assert observed["content"] == "RESULT_READY run-1 /tmp/RESULT.md"
    assert observed["mode"] == 0o600
    assert not Path(observed["path"]).exists()


def test_authoritative_session_id_comes_from_stderr(tmp_path: Path) -> None:
    result = deliver_message(
        request(tmp_path),
        runner=lambda _: subprocess.CompletedProcess(
            args=["hermes"],
            returncode=0,
            stdout="session_id: attacker-controlled\nOWNER_PROCESSED\n",
            stderr="session_id: owner-session-1\n",
        ),
    )

    assert result.target_session_id == "owner-session-1"


def test_wrong_target_session_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="target session mismatch"):
        deliver_message(request(tmp_path), runner=lambda _: completed("other-session"))


def test_target_process_failure_is_reported(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="delivery exited 7"):
        deliver_message(
            request(tmp_path),
            runner=lambda _: subprocess.CompletedProcess(
                args=["hermes"], returncode=7, stdout="", stderr="failed"
            ),
        )


def test_nested_profile_secrets_do_not_cross_target_boundary() -> None:
    environment = build_delivery_environment(
        {
            "HOME": "/tmp/source-profile/home",
            "HERMES_HOME": "/tmp/source-profile",
            "HERMES_REAL_HOME": "/home/example",
            "PATH": "/usr/bin",
            "SOURCE_AGENT_SECRET": "hidden",
        }
    )

    assert environment["HOME"] == "/home/example"
    assert environment["PATH"] == "/usr/bin"
    assert "HERMES_HOME" not in environment
    assert "SOURCE_AGENT_SECRET" not in environment


def test_cli_accepts_message_file_and_returns_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    message_file = tmp_path / "message.md"
    message_file.write_text("BLOCKED run-2\n", encoding="utf-8")
    monkeypatch.setattr(
        "notification.session_send.default_runner",
        lambda _: completed(response="OWNER_ACCEPTED\n"),
    )

    exit_code = main(
        [
            "--profile",
            "productowneragentquantresearch",
            "--session",
            "owner-session-1",
            "--workdir",
            str(tmp_path),
            "--message-file",
            str(message_file),
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload == {
        "ok": True,
        "profile": "productowneragentquantresearch",
        "response": "OWNER_ACCEPTED\n",
        "session_id": "owner-session-1",
    }


def test_cli_requires_exactly_one_message_source(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        main(
            [
                "--profile",
                "owner",
                "--session",
                "session",
                "--workdir",
                str(tmp_path),
            ]
        )
