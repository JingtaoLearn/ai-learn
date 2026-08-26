import hashlib
import json
import os
import socket
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

import quant_platform.runner as runner_module
from quant_platform.datasets import publish_snapshot
from quant_platform.runner import (
    RunnerCallbackError,
    RunnerIntegrityError,
    run_submission,
)
from quant_platform.submissions import publish_submission


class FakeClock:
    def __init__(self):
        self.current = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
        self.elapsed = 100.0

    def now(self) -> datetime:
        value = self.current
        self.current += timedelta(seconds=2.5)
        return value

    def monotonic(self) -> float:
        value = self.elapsed
        self.elapsed += 2.5
        return value


class FakeProcess:
    def __init__(self, exit_status: int = 0, *, times_out: bool = False):
        self.exit_status = exit_status
        self.times_out = times_out
        self.pid = 43210
        self.wait_calls = 0

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls += 1
        if self.times_out and self.wait_calls == 1:
            raise subprocess.TimeoutExpired(["docker"], timeout)
        return self.exit_status


class FakeLauncher:
    def __init__(
        self,
        *,
        exit_status: int = 0,
        times_out: bool = False,
        artifact_kind: str = "regular",
    ):
        self.process = FakeProcess(exit_status, times_out=times_out)
        self.artifact_kind = artifact_kind
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, command: list[str], **kwargs) -> FakeProcess:
        self.calls.append((command, kwargs))
        kwargs["stdout"].write(b"internal standard output\n")
        kwargs["stderr"].write(b"internal standard error\n")
        kwargs["stdout"].flush()
        kwargs["stderr"].flush()
        artifact_dir = _mounted_artifact_dir(command)
        if self.artifact_kind == "regular":
            (artifact_dir / "result.json").write_text('{"value": 1}\n')
        elif self.artifact_kind == "symlink":
            (artifact_dir / "result-link").symlink_to(artifact_dir.parent)
        elif self.artifact_kind == "fifo":
            os.mkfifo(artifact_dir / "result.pipe")
        elif self.artifact_kind == "socket":
            descriptor = os.open(artifact_dir, os.O_RDONLY)
            try:
                service_socket = socket.socket(socket.AF_UNIX)
                service_socket.bind(f"/proc/self/fd/{descriptor}/result.sock")
                service_socket.close()
            finally:
                os.close(descriptor)
        return self.process


def _mounted_artifact_dir(command: list[str]) -> Path:
    mount = next(value for value in command if "dst=/artifacts" in value)
    return Path(mount.split("src=", 1)[1].split(",dst=", 1)[0])


def _foundation(tmp_path: Path) -> tuple[Path, dict[str, str], dict[str, str]]:
    root = tmp_path / "state"
    dataset = publish_snapshot(
        pd.DataFrame(
            {
                "Date": pd.to_datetime(["2026-08-18", "2026-08-19"]),
                "Open": [6.12, 6.18],
                "High": [6.20, 6.24],
                "Low": [6.08, 6.14],
                "Close": [6.18, 6.20],
                "Volume": [1200, 1100],
            }
        ),
        root,
        {
            "instrument": "601288.SS",
            "provider": "synthetic",
            "market": "XSHG",
            "currency": "CNY",
            "adjustment": "unadjusted",
        },
    )
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    (project / "tests").mkdir()
    (project / "src" / "main.py").write_text("print('reference')\n")
    (project / "tests" / "test_main.py").write_text("def test_true():\n    assert True\n")
    (project / "pyproject.toml").write_text("[project]\nname='runner-test'\nversion='0.1'\n")
    (project / "requirements.in").write_text("")
    (project / "requirements.lock").write_text("")
    submission = publish_submission(
        {
            "name": "runner-test",
            "entrypoint": "src/main.py",
            "dataset_snapshot_id": dataset["snapshot_id"],
            "runner_image": "sha256:" + "a" * 64,
            "config": {},
            "seed": 7,
        },
        project,
        root,
    )
    return root, dataset, submission


def _manifest(result: dict) -> dict:
    return json.loads((Path(result["path"]) / "attempt.json").read_text())


def test_runner_resolves_verified_submission_and_bound_dataset(tmp_path: Path):
    root, dataset, submission = _foundation(tmp_path)
    launcher = FakeLauncher()

    result = run_submission(
        root,
        submission["submission_id"],
        "attempt-001",
        30,
        process_launcher=launcher,
        clock=FakeClock(),
    )

    command = launcher.calls[0][0]
    assert f"src={submission['path']},dst=/workspace" not in " ".join(command)
    assert (
        f"type=bind,src={Path(submission['path']) / 'source'},dst=/workspace,readonly"
        in command
    )
    assert f"type=bind,src={dataset['path']},dst=/data,readonly" in command
    assert result["submission_id"] == submission["submission_id"]
    assert result["dataset_snapshot_id"] == dataset["snapshot_id"]


def test_runner_creates_fresh_attempt_and_rejects_reuse_or_traversal(tmp_path: Path):
    root, _, submission = _foundation(tmp_path)
    result = run_submission(
        root,
        submission["submission_id"],
        "attempt-001",
        30,
        process_launcher=FakeLauncher(),
        clock=FakeClock(),
    )

    assert Path(result["path"]).is_dir()
    with pytest.raises(FileExistsError, match="attempt"):
        run_submission(
            root,
            submission["submission_id"],
            "attempt-001",
            30,
            process_launcher=FakeLauncher(),
            clock=FakeClock(),
        )
    with pytest.raises(ValueError, match="attempt_id"):
        run_submission(
            root,
            submission["submission_id"],
            "../escape",
            30,
            process_launcher=FakeLauncher(),
            clock=FakeClock(),
        )


def test_runner_invokes_exact_command_without_shell_or_host_environment(
    tmp_path: Path, monkeypatch
):
    root, _, submission = _foundation(tmp_path)
    launcher = FakeLauncher()
    built_commands: list[list[str]] = []
    real_build = runner_module.build_docker_command

    def capture_build(*args) -> list[str]:
        command = real_build(*args)
        built_commands.append(command)
        return command

    monkeypatch.setattr(runner_module, "build_docker_command", capture_build)
    monkeypatch.setenv("SENSITIVE_SENTINEL", "never-pass-this")

    run_submission(
        root,
        submission["submission_id"],
        "attempt-001",
        30,
        process_launcher=launcher,
        clock=FakeClock(),
    )
    command, kwargs = launcher.calls[0]

    assert command == built_commands[0]
    assert kwargs["shell"] is False
    assert kwargs["env"] == {"PATH": "/usr/local/bin:/usr/bin:/bin"}
    assert "SENSITIVE_SENTINEL" not in kwargs["env"]
    assert kwargs["start_new_session"] is True


def test_runner_keeps_logs_in_attempt_and_out_of_terminal_record(tmp_path: Path):
    root, _, submission = _foundation(tmp_path)
    result = run_submission(
        root,
        submission["submission_id"],
        "attempt-001",
        30,
        process_launcher=FakeLauncher(),
        clock=FakeClock(),
    )

    assert "internal standard output" not in json.dumps(result)
    assert "stdout" not in result
    assert (Path(result["path"]) / "stdout.log").read_text() == "internal standard output\n"
    assert (Path(result["path"]) / "stderr.log").read_text() == "internal standard error\n"


@pytest.mark.parametrize(
    ("launcher", "expected_outcome", "expected_status"),
    [
        (FakeLauncher(), "SUCCESS", 0),
        (FakeLauncher(exit_status=17), "FAILED", 17),
        (FakeLauncher(exit_status=-15, times_out=True), "TIMED_OUT", -15),
    ],
)
def test_runner_seals_process_terminal_outcomes(
    tmp_path: Path, monkeypatch, launcher, expected_outcome: str, expected_status: int
):
    root, _, submission = _foundation(tmp_path)
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "killpg", lambda pid, signal: signals.append((pid, signal)))

    result = run_submission(
        root,
        submission["submission_id"],
        f"attempt-{expected_outcome.lower()}",
        30,
        process_launcher=launcher,
        clock=FakeClock(),
    )
    manifest = _manifest(result)

    assert result["outcome"] == expected_outcome
    assert manifest["outcome"] == expected_outcome
    assert manifest["exit_status"] == expected_status
    if expected_outcome == "TIMED_OUT":
        assert signals


def test_runner_seals_launch_failure(tmp_path: Path):
    root, _, submission = _foundation(tmp_path)

    def fail_to_launch(command, **kwargs):
        raise OSError("docker unavailable")

    result = run_submission(
        root,
        submission["submission_id"],
        "attempt-launch-failure",
        30,
        process_launcher=fail_to_launch,
        clock=FakeClock(),
    )
    manifest = _manifest(result)

    assert result["outcome"] == "LAUNCH_FAILED"
    assert manifest["outcome"] == "LAUNCH_FAILED"
    assert manifest["exit_status"] is None
    assert manifest["error_type"] == "OSError"


def test_attempt_manifest_records_contract_timing_and_artifact_checksums(tmp_path: Path):
    root, dataset, submission = _foundation(tmp_path)
    result = run_submission(
        root,
        submission["submission_id"],
        "attempt-001",
        30,
        process_launcher=FakeLauncher(),
        clock=FakeClock(),
    )
    manifest = _manifest(result)

    assert manifest["run_id"] == result["run_id"]
    assert manifest["submission_id"] == submission["submission_id"]
    assert manifest["dataset_snapshot_id"] == dataset["snapshot_id"]
    assert manifest["runner_image"] == "sha256:" + "a" * 64
    assert manifest["execution_envelope"]["network"] == "none"
    assert manifest["started_at"] == "2026-08-26T12:00:00+00:00"
    assert manifest["finished_at"] == "2026-08-26T12:00:02.500000+00:00"
    assert manifest["duration_seconds"] == 2.5
    assert set(manifest["files"]) == {"result.json", "stderr.log", "stdout.log"}
    for relative, identity in manifest["files"].items():
        payload = (Path(result["path"]) / relative).read_bytes()
        assert identity == {
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size": len(payload),
        }
    assert "attempt.json" not in manifest["files"]


@pytest.mark.parametrize("artifact_kind", ["symlink", "fifo", "socket"])
def test_runner_rejects_unsafe_artifacts_and_still_seals_attempt(
    tmp_path: Path, artifact_kind: str
):
    root, _, submission = _foundation(tmp_path)

    with pytest.raises(RunnerIntegrityError, match="artifact"):
        run_submission(
            root,
            submission["submission_id"],
            f"attempt-{artifact_kind}",
            30,
            process_launcher=FakeLauncher(artifact_kind=artifact_kind),
            clock=FakeClock(),
        )

    attempt = root / "artifacts" / submission["submission_id"] / f"attempt-{artifact_kind}"
    manifest = json.loads((attempt / "attempt.json").read_text())
    assert manifest["outcome"] == "ARTIFACT_REJECTED"
    assert attempt.stat().st_mode & 0o222 == 0


def test_runner_detects_post_run_artifact_mutation(tmp_path: Path, monkeypatch):
    root, _, submission = _foundation(tmp_path)
    original_hash = runner_module._hash_attempt_files
    calls = 0

    def mutate_after_hash(attempt_dir: Path):
        nonlocal calls
        result = original_hash(attempt_dir)
        calls += 1
        if calls == 1:
            (attempt_dir / "result.json").write_text('{"value": 2}\n')
        return result

    monkeypatch.setattr(runner_module, "_hash_attempt_files", mutate_after_hash)

    with pytest.raises(RunnerIntegrityError, match="changed"):
        run_submission(
            root,
            submission["submission_id"],
            "attempt-mutated",
            30,
            process_launcher=FakeLauncher(),
            clock=FakeClock(),
        )


def test_attempt_is_sealed_after_manifest_publication(tmp_path: Path):
    root, _, submission = _foundation(tmp_path)
    result = run_submission(
        root,
        submission["submission_id"],
        "attempt-001",
        30,
        process_launcher=FakeLauncher(),
        clock=FakeClock(),
    )
    attempt = Path(result["path"])

    assert (attempt / "attempt.json").is_file()
    assert attempt.stat().st_mode & 0o222 == 0
    assert all(path.stat().st_mode & 0o222 == 0 for path in attempt.rglob("*"))


def test_callback_receives_terminal_record_and_failure_cannot_rewrite_attempt(tmp_path: Path):
    root, _, submission = _foundation(tmp_path)
    received: list[dict] = []

    def callback(record: dict) -> None:
        received.append(record)
        (Path(record["path"]) / "attempt.json").write_text("{}")

    with pytest.raises(RunnerCallbackError, match="callback"):
        run_submission(
            root,
            submission["submission_id"],
            "attempt-001",
            30,
            process_launcher=FakeLauncher(),
            clock=FakeClock(),
            callback=callback,
        )

    assert received[0]["outcome"] == "SUCCESS"
    manifest = json.loads((Path(received[0]["path"]) / "attempt.json").read_text())
    assert manifest["outcome"] == "SUCCESS"
