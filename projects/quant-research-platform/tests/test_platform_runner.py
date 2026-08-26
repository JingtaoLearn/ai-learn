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


@pytest.fixture(autouse=True)
def _never_signal_real_process_groups(monkeypatch):
    monkeypatch.setattr(os, "killpg", lambda pid, value: None)


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
    def __init__(
        self,
        exit_status: int = 0,
        *,
        times_out: bool = False,
        remains_running: bool = False,
        wait_error: OSError | None = None,
    ):
        self.exit_status = exit_status
        self.times_out = times_out
        self.remains_running = remains_running
        self.wait_error = wait_error
        self.exited = False
        self.pid = 43210
        self.wait_calls = 0

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls += 1
        if self.wait_error is not None:
            raise self.wait_error
        if self.remains_running or (self.times_out and self.wait_calls == 1):
            raise subprocess.TimeoutExpired(["docker"], timeout)
        self.exited = True
        return self.exit_status

    def poll(self) -> int | None:
        return self.exit_status if self.exited else None


class FakeLauncher:
    def __init__(
        self,
        *,
        exit_status: int = 0,
        times_out: bool = False,
        artifact_kind: str = "regular",
        write_cid: bool = True,
        remains_running: bool = False,
        wait_error: OSError | None = None,
    ):
        self.process = FakeProcess(
            exit_status,
            times_out=times_out,
            remains_running=remains_running,
            wait_error=wait_error,
        )
        self.artifact_kind = artifact_kind
        self.write_cid = write_cid
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, command: list[str], **kwargs) -> FakeProcess:
        self.calls.append((command, kwargs))
        cidfile = Path(command[command.index("--cidfile") + 1])
        if self.write_cid:
            cidfile.write_text("d" * 64 + "\n")
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
        elif self.artifact_kind == "reserved":
            (artifact_dir / "attempt.json").write_text('{"forged": true}\n')
        elif self.artifact_kind == "locked-reserved":
            (artifact_dir / "attempt.json").write_text('{"forged": true}\n')
            artifact_dir.chmod(0o000)
        elif self.artifact_kind == "hardlinks":
            first = artifact_dir / "first.json"
            first.write_text('{"value": 1}\n')
            os.link(first, artifact_dir / "second.json")
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


def _assert_unconfirmed_evidence(attempt: Path) -> dict:
    manifest = json.loads((attempt / "attempt.json").read_text())
    assert manifest["outcome"] == "TERMINATION_UNCONFIRMED"
    assert manifest["payload_integrity"] == "UNVERIFIED"
    assert manifest["rejected_entries"] == [
        {"path": "payload", "reason": "termination-unconfirmed"}
    ]
    assert not (attempt / "payload").exists()
    assert (attempt.parents[2] / manifest["quarantine_path"]).is_dir()
    assert attempt.stat().st_mode & 0o222 == 0
    return manifest


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
    launcher = FakeLauncher()
    result = run_submission(
        root,
        submission["submission_id"],
        "attempt-001",
        30,
        process_launcher=launcher,
        clock=FakeClock(),
    )

    assert "internal standard output" not in json.dumps(result)
    assert "stdout" not in result
    assert (Path(result["path"]) / "stdout.log").read_text() == "internal standard output\n"
    assert (Path(result["path"]) / "stderr.log").read_text() == "internal standard error\n"
    command = json.dumps(launcher.calls[0][0])
    assert "stdout.log" not in command
    assert "stderr.log" not in command


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
    terminated: list[str] = []

    def terminate(cidfile: Path, container_name: str, process) -> int:
        terminated.append(cidfile.read_text().strip())
        return process.wait()

    result = run_submission(
        root,
        submission["submission_id"],
        f"attempt-{expected_outcome.lower()}",
        30,
        process_launcher=launcher,
        clock=FakeClock(),
        container_terminator=terminate,
    )
    manifest = _manifest(result)

    assert result["outcome"] == expected_outcome
    assert manifest["outcome"] == expected_outcome
    assert manifest["exit_status"] == expected_status
    if expected_outcome == "TIMED_OUT":
        assert terminated == ["d" * 64]


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
    assert set(manifest["files"]) == {
        "container.cid",
        "payload/result.json",
        "stderr.log",
        "stdout.log",
    }
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
    artifact_name = {
        "symlink": "result-link",
        "fifo": "result.pipe",
        "socket": "result.sock",
    }[artifact_kind]
    assert manifest["outcome"] == "ARTIFACT_REJECTED"
    assert set(manifest["files"]) == {"container.cid", "stderr.log", "stdout.log"}
    assert manifest["rejected_entries"] == [
        {
            "path": f"payload/{artifact_name}",
            "reason": artifact_kind,
        }
    ]
    assert not any(path.is_symlink() for path in attempt.rglob("*"))
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


def test_runner_rejects_container_created_reserved_control_filename(tmp_path: Path):
    root, _, submission = _foundation(tmp_path)

    with pytest.raises(RunnerIntegrityError, match="reserved"):
        run_submission(
            root,
            submission["submission_id"],
            "attempt-reserved",
            30,
            process_launcher=FakeLauncher(artifact_kind="reserved"),
            clock=FakeClock(),
        )

    attempt = root / "artifacts" / submission["submission_id"] / "attempt-reserved"
    manifest = json.loads((attempt / "attempt.json").read_text())
    assert manifest["outcome"] == "ARTIFACT_REJECTED"
    assert manifest["rejected_entries"] == [
        {"path": "payload/attempt.json", "reason": "reserved"}
    ]
    assert not (attempt / "payload" / "attempt.json").exists()


def test_runner_exhaustively_validates_mode_zero_payload(tmp_path: Path):
    root, _, submission = _foundation(tmp_path)

    with pytest.raises(RunnerIntegrityError, match="reserved"):
        run_submission(
            root,
            submission["submission_id"],
            "attempt-locked",
            30,
            process_launcher=FakeLauncher(artifact_kind="locked-reserved"),
            clock=FakeClock(),
        )

    attempt = root / "artifacts" / submission["submission_id"] / "attempt-locked"
    manifest = json.loads((attempt / "attempt.json").read_text())
    assert manifest["rejected_entries"] == [
        {"path": "payload/attempt.json", "reason": "reserved"}
    ]
    assert not (attempt / "payload" / "attempt.json").exists()
    assert all(path.stat().st_mode & 0o222 == 0 for path in attempt.rglob("*"))


def test_timeout_does_not_treat_docker_daemon_error_as_confirmed_removal(
    tmp_path: Path, monkeypatch
):
    root, _, submission = _foundation(tmp_path)
    launcher = FakeLauncher(exit_status=-15, times_out=True)

    def daemon_unavailable(*args, **kwargs):
        return subprocess.CompletedProcess(
            args[0], 1, stdout="", stderr="Cannot connect to the Docker daemon"
        )

    monkeypatch.setattr(subprocess, "run", daemon_unavailable)

    with pytest.raises(runner_module.RunnerTerminationError, match="confirm"):
        run_submission(
            root,
            submission["submission_id"],
            "attempt-daemon-error",
            1,
            process_launcher=launcher,
            clock=FakeClock(),
        )

    attempt = root / "artifacts" / submission["submission_id"] / "attempt-daemon-error"
    _assert_unconfirmed_evidence(attempt)


def test_timeout_without_cid_uses_name_and_terminates_docker_process_group(
    tmp_path: Path, monkeypatch
):
    root, _, submission = _foundation(tmp_path)
    launcher = FakeLauncher(exit_status=-15, times_out=True, write_cid=False)
    signals: list[tuple[int, int]] = []
    commands: list[list[str]] = []

    def docker_absent(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(
            command, 1, stdout="", stderr="Error: No such object: quant-research-test"
        )

    monkeypatch.setattr(subprocess, "run", docker_absent)
    monkeypatch.setattr(os, "killpg", lambda pid, value: signals.append((pid, value)))

    with pytest.raises(runner_module.RunnerTerminationError, match="container ID"):
        run_submission(
            root,
            submission["submission_id"],
            "attempt-no-cid",
            1,
            process_launcher=launcher,
            clock=FakeClock(),
        )

    assert signals
    container_name = launcher.calls[0][0][launcher.calls[0][0].index("--name") + 1]
    assert any(container_name in command for command in commands)
    attempt = root / "artifacts" / submission["submission_id"] / "attempt-no-cid"
    manifest = _assert_unconfirmed_evidence(attempt)
    assert manifest["files"] == {}


def test_timeout_confirms_container_absence_by_immutable_cid(
    tmp_path: Path, monkeypatch
):
    root, _, submission = _foundation(tmp_path)
    launcher = FakeLauncher(exit_status=-15, times_out=True)
    commands: list[list[str]] = []

    def docker_absent(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(
            command,
            1,
            stdout="",
            stderr=f"Error: No such object: {'d' * 64}",
        )

    monkeypatch.setattr(subprocess, "run", docker_absent)

    result = run_submission(
        root,
        submission["submission_id"],
        "attempt-cid-confirmed",
        1,
        process_launcher=launcher,
        clock=FakeClock(),
    )

    assert result["outcome"] == "TIMED_OUT"
    inspect_commands = [command for command in commands if command[:2] == ["docker", "inspect"]]
    assert inspect_commands
    assert all(command[-1] == "d" * 64 for command in inspect_commands)


def test_timeout_reaps_docker_process_when_control_command_times_out(
    tmp_path: Path, monkeypatch
):
    root, _, submission = _foundation(tmp_path)
    launcher = FakeLauncher(exit_status=-15, times_out=True)
    signals: list[tuple[int, int]] = []

    def control_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 10)

    monkeypatch.setattr(subprocess, "run", control_timeout)
    monkeypatch.setattr(os, "killpg", lambda pid, value: signals.append((pid, value)))

    with pytest.raises(runner_module.RunnerTerminationError, match="confirm"):
        run_submission(
            root,
            submission["submission_id"],
            "attempt-control-timeout",
            1,
            process_launcher=launcher,
            clock=FakeClock(),
        )

    assert signals
    attempt = (
        root
        / "artifacts"
        / submission["submission_id"]
        / "attempt-control-timeout"
    )
    _assert_unconfirmed_evidence(attempt)


def test_unconfirmed_termination_seals_control_only_evidence_and_reraises(
    tmp_path: Path
):
    root, _, submission = _foundation(tmp_path)
    launcher = FakeLauncher(exit_status=-15, times_out=True)
    original_error = runner_module.RunnerTerminationError(
        "Docker removal could not be confirmed"
    )

    def unconfirmed(cidfile: Path, container_name: str, process):
        raise original_error

    with pytest.raises(runner_module.RunnerTerminationError) as caught:
        run_submission(
            root,
            submission["submission_id"],
            "attempt-unconfirmed",
            1,
            process_launcher=launcher,
            container_terminator=unconfirmed,
            clock=FakeClock(),
        )

    assert caught.value is original_error
    attempt = (
        root / "artifacts" / submission["submission_id"] / "attempt-unconfirmed"
    )
    manifest = json.loads((attempt / "attempt.json").read_text())
    assert manifest["outcome"] == "TERMINATION_UNCONFIRMED"
    assert manifest["error_type"] == "RunnerTerminationError"
    assert manifest["payload_integrity"] == "UNVERIFIED"
    assert manifest["rejected_entries"] == [
        {"path": "payload", "reason": "termination-unconfirmed"}
    ]
    assert manifest["files"] == {}
    assert not (attempt / "payload").exists()
    quarantine = root / manifest["quarantine_path"]
    assert quarantine.is_dir()
    assert (quarantine / "payload" / "result.json").is_file()
    assert (quarantine / "container.cid").is_file()
    assert (quarantine / "stdout.log").is_file()
    assert (quarantine / "stderr.log").is_file()
    assert attempt.stat().st_mode & 0o222 == 0
    assert all(path.stat().st_mode & 0o222 == 0 for path in attempt.rglob("*"))


def test_terminator_timeoutexpired_uses_control_only_evidence(tmp_path: Path):
    root, _, submission = _foundation(tmp_path)

    def control_timeout(cidfile: Path, container_name: str, process):
        raise subprocess.TimeoutExpired(["docker", "inspect"], 10)

    with pytest.raises(runner_module.RunnerTerminationError, match="termination"):
        run_submission(
            root,
            submission["submission_id"],
            "attempt-terminator-timeout",
            1,
            process_launcher=FakeLauncher(exit_status=-15, times_out=True),
            container_terminator=control_timeout,
            clock=FakeClock(),
        )

    attempt = (
        root
        / "artifacts"
        / submission["submission_id"]
        / "attempt-terminator-timeout"
    )
    _assert_unconfirmed_evidence(attempt)


def test_stream_finalization_error_preserves_termination_error_and_evidence(
    tmp_path: Path, monkeypatch
):
    root, _, submission = _foundation(tmp_path)
    original_error = runner_module.RunnerTerminationError("removal unconfirmed")
    calls = 0

    def fail_first_flush(stream) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("log fsync failed")
        stream.flush()
        os.fsync(stream.fileno())

    def unconfirmed(cidfile: Path, container_name: str, process):
        raise original_error

    monkeypatch.setattr(
        runner_module,
        "_flush_stream",
        fail_first_flush,
        raising=False,
    )

    with pytest.raises(runner_module.RunnerTerminationError) as caught:
        run_submission(
            root,
            submission["submission_id"],
            "attempt-stream-error",
            1,
            process_launcher=FakeLauncher(exit_status=-15, times_out=True),
            container_terminator=unconfirmed,
            clock=FakeClock(),
        )

    assert caught.value is original_error
    assert isinstance(caught.value.__cause__, OSError)
    _assert_unconfirmed_evidence(
        root / "artifacts" / submission["submission_id"] / "attempt-stream-error"
    )


def test_quarantine_creation_fsyncs_every_new_parent(tmp_path: Path, monkeypatch):
    root, _, submission = _foundation(tmp_path)
    original_error = runner_module.RunnerTerminationError("removal unconfirmed")
    real_fsync_directory = runner_module._fsync_directory
    fsynced: list[Path] = []

    def capture_fsync(directory: Path) -> None:
        fsynced.append(directory)
        real_fsync_directory(directory)

    def unconfirmed(cidfile: Path, container_name: str, process):
        raise original_error

    monkeypatch.setattr(runner_module, "_fsync_directory", capture_fsync)

    with pytest.raises(runner_module.RunnerTerminationError):
        run_submission(
            root,
            submission["submission_id"],
            "attempt-fsync-hierarchy",
            1,
            process_launcher=FakeLauncher(exit_status=-15, times_out=True),
            container_terminator=unconfirmed,
            clock=FakeClock(),
        )

    assert root in fsynced
    assert root / "quarantine" in fsynced
    assert root / "quarantine" / submission["submission_id"] in fsynced


def test_unconfirmed_termination_quarantines_controls_when_cli_reap_fails(
    tmp_path: Path, monkeypatch
):
    root, _, submission = _foundation(tmp_path)
    launcher = FakeLauncher(exit_status=-15, times_out=True)
    original_error = runner_module.RunnerTerminationError("removal unconfirmed")

    def unconfirmed(cidfile: Path, container_name: str, process):
        raise original_error

    monkeypatch.setattr(
        runner_module,
        "_terminate_process_group",
        lambda process: (_ for _ in ()).throw(OSError("cannot reap Docker CLI")),
    )

    with pytest.raises(runner_module.RunnerTerminationError) as caught:
        run_submission(
            root,
            submission["submission_id"],
            "attempt-reap-failed",
            1,
            process_launcher=launcher,
            container_terminator=unconfirmed,
            clock=FakeClock(),
        )

    assert caught.value is original_error
    attempt = (
        root / "artifacts" / submission["submission_id"] / "attempt-reap-failed"
    )
    manifest = json.loads((attempt / "attempt.json").read_text())
    assert manifest["files"] == {}
    assert not (attempt / "stdout.log").exists()
    assert not (attempt / "stderr.log").exists()
    assert not (attempt / "container.cid").exists()
    quarantine = root / manifest["quarantine_path"]
    assert (quarantine / "stdout.log").is_file()
    assert (quarantine / "stderr.log").is_file()
    assert (quarantine / "container.cid").is_file()


def test_terminator_return_without_cli_exit_uses_control_only_evidence(
    tmp_path: Path
):
    root, _, submission = _foundation(tmp_path)
    launcher = FakeLauncher(
        exit_status=-15,
        times_out=True,
        remains_running=True,
    )

    def returns_without_reaping(cidfile: Path, container_name: str, process):
        return -15

    with pytest.raises(runner_module.RunnerTerminationError, match="CLI exit"):
        run_submission(
            root,
            submission["submission_id"],
            "attempt-cli-running",
            1,
            process_launcher=launcher,
            container_terminator=returns_without_reaping,
            clock=FakeClock(),
        )

    attempt = (
        root / "artifacts" / submission["submission_id"] / "attempt-cli-running"
    )
    manifest = _assert_unconfirmed_evidence(attempt)
    assert manifest["files"] == {}


def test_post_launch_wait_oserror_uses_control_only_evidence(tmp_path: Path):
    root, _, submission = _foundation(tmp_path)
    launcher = FakeLauncher(wait_error=OSError("wait failed"))

    with pytest.raises(runner_module.RunnerTerminationError, match="state"):
        run_submission(
            root,
            submission["submission_id"],
            "attempt-wait-oserror",
            1,
            process_launcher=launcher,
            clock=FakeClock(),
        )

    attempt = (
        root / "artifacts" / submission["submission_id"] / "attempt-wait-oserror"
    )
    manifest = _assert_unconfirmed_evidence(attempt)
    assert manifest["files"] == {}


def test_reaped_docker_cli_is_not_signaled_again(tmp_path: Path, monkeypatch):
    root, _, submission = _foundation(tmp_path)
    launcher = FakeLauncher(exit_status=-15, times_out=True)
    original_error = runner_module.RunnerTerminationError("inspect ambiguous")

    def reaps_then_fails(cidfile: Path, container_name: str, process):
        process.wait()
        raise original_error

    monkeypatch.setattr(
        runner_module,
        "_terminate_process_group",
        lambda process: (_ for _ in ()).throw(
            AssertionError("reaped process group must not be signaled")
        ),
    )

    with pytest.raises(runner_module.RunnerTerminationError) as caught:
        run_submission(
            root,
            submission["submission_id"],
            "attempt-already-reaped",
            1,
            process_launcher=launcher,
            container_terminator=reaps_then_fails,
            clock=FakeClock(),
        )

    assert caught.value is original_error
    _assert_unconfirmed_evidence(
        root / "artifacts" / submission["submission_id"] / "attempt-already-reaped"
    )


def test_evidence_finalization_failure_does_not_mask_termination_error(
    tmp_path: Path, monkeypatch
):
    root, _, submission = _foundation(tmp_path)
    original_error = runner_module.RunnerTerminationError("removal unconfirmed")

    def unconfirmed(cidfile: Path, container_name: str, process):
        raise original_error

    monkeypatch.setattr(
        runner_module,
        "_quarantine_untrusted_execution",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RunnerIntegrityError("quarantine unavailable")
        ),
        raising=False,
    )

    with pytest.raises(runner_module.RunnerTerminationError) as caught:
        run_submission(
            root,
            submission["submission_id"],
            "attempt-finalization-failed",
            1,
            process_launcher=FakeLauncher(exit_status=-15, times_out=True),
            container_terminator=unconfirmed,
            clock=FakeClock(),
        )

    assert caught.value is original_error
    assert isinstance(caught.value.__cause__, RunnerIntegrityError)


def test_inaccessible_payload_failure_is_recorded_and_sealed(
    tmp_path: Path, monkeypatch
):
    root, _, submission = _foundation(tmp_path)

    def inaccessible(payload_dir: Path) -> None:
        raise RunnerIntegrityError("artifact payload is inaccessible")

    monkeypatch.setattr(
        runner_module, "_prepare_payload_for_validation", inaccessible
    )

    with pytest.raises(RunnerIntegrityError, match="inaccessible"):
        run_submission(
            root,
            submission["submission_id"],
            "attempt-inaccessible",
            30,
            process_launcher=FakeLauncher(),
            clock=FakeClock(),
        )

    attempt = (
        root / "artifacts" / submission["submission_id"] / "attempt-inaccessible"
    )
    manifest = json.loads((attempt / "attempt.json").read_text())
    assert manifest["outcome"] == "ARTIFACT_REJECTED"
    assert manifest["rejected_entries"] == [
        {"path": "payload", "reason": "inaccessible"}
    ]
    assert manifest["payload_integrity"] == "UNVERIFIED"
    assert set(manifest["files"]) == {"container.cid", "stderr.log", "stdout.log"}
    assert not any((attempt / "payload").iterdir())
    assert all(path.stat().st_mode & 0o222 == 0 for path in attempt.rglob("*"))


def test_runner_rejects_every_path_in_hard_link_set(tmp_path: Path):
    root, _, submission = _foundation(tmp_path)

    with pytest.raises(RunnerIntegrityError, match="hardlink"):
        run_submission(
            root,
            submission["submission_id"],
            "attempt-hardlinks",
            30,
            process_launcher=FakeLauncher(artifact_kind="hardlinks"),
            clock=FakeClock(),
        )

    attempt = root / "artifacts" / submission["submission_id"] / "attempt-hardlinks"
    manifest = json.loads((attempt / "attempt.json").read_text())
    assert manifest["rejected_entries"] == [
        {"path": "payload/first.json", "reason": "hardlink"},
        {"path": "payload/second.json", "reason": "hardlink"},
    ]
    assert not any((attempt / "payload").iterdir())


def test_post_seal_verification_detects_attempt_manifest_replacement(
    tmp_path: Path, monkeypatch
):
    root, _, submission = _foundation(tmp_path)
    real_seal = runner_module._seal_attempt

    def replace_manifest_after_seal(attempt_dir: Path) -> None:
        real_seal(attempt_dir)
        manifest_path = attempt_dir / "attempt.json"
        manifest_path.chmod(0o644)
        manifest_path.write_text("{}")
        manifest_path.chmod(0o444)

    monkeypatch.setattr(runner_module, "_seal_attempt", replace_manifest_after_seal)

    with pytest.raises(RunnerIntegrityError, match="manifest"):
        run_submission(
            root,
            submission["submission_id"],
            "attempt-manifest-replaced",
            30,
            process_launcher=FakeLauncher(),
            clock=FakeClock(),
        )


def test_post_seal_verification_rejects_hard_linked_attempt_manifest(
    tmp_path: Path, monkeypatch
):
    root, _, submission = _foundation(tmp_path)
    real_seal = runner_module._seal_attempt
    alias = tmp_path / "attempt-manifest-alias.json"

    def hard_link_manifest_after_seal(attempt_dir: Path) -> None:
        real_seal(attempt_dir)
        os.link(attempt_dir / "attempt.json", alias)

    monkeypatch.setattr(runner_module, "_seal_attempt", hard_link_manifest_after_seal)

    with pytest.raises(RunnerIntegrityError, match="manifest.*hard link"):
        run_submission(
            root,
            submission["submission_id"],
            "attempt-manifest-hardlink",
            30,
            process_launcher=FakeLauncher(),
            clock=FakeClock(),
        )


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
