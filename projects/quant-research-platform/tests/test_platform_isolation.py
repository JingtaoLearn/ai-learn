import json
import shutil
from pathlib import Path

import pandas as pd
import pytest

from quant_platform.datasets import publish_snapshot
from quant_platform.isolation import IsolationError, build_docker_command
from quant_platform.submissions import publish_submission


def _market_frame(close: float = 6.20) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Date": pd.to_datetime(["2026-08-18", "2026-08-19"]),
            "Open": [6.12, 6.18],
            "High": [6.20, 6.24],
            "Low": [6.08, 6.14],
            "Close": [6.18, close],
            "Volume": [1200, 1100],
        }
    )


def _foundation(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    root = tmp_path / "state"
    dataset = publish_snapshot(
        _market_frame(),
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
    (project / "src" / "main.py").write_text("print('ok')\n")
    (project / "tests" / "test_main.py").write_text("def test_true():\n    assert True\n")
    (project / "pyproject.toml").write_text("[project]\nname='isolation-test'\nversion='0.1'\n")
    (project / "requirements.in").write_text("")
    (project / "requirements.lock").write_text("")
    submission = publish_submission(
        {
            "name": "isolation-test",
            "entrypoint": "src/main.py",
            "dataset_snapshot_id": dataset["snapshot_id"],
            "runner_image": "quant-research-runner@sha256:" + "a" * 64,
            "config": {},
            "seed": 7,
        },
        project,
        root,
    )
    artifacts = root / "artifacts" / submission["submission_id"] / "attempt-001"
    artifacts.mkdir(parents=True)
    return root, Path(submission["path"]), Path(dataset["path"]), artifacts


def test_docker_command_enforces_fixed_research_sandbox(tmp_path: Path):
    _, submission, dataset, artifacts = _foundation(tmp_path)

    command = build_docker_command(
        submission,
        dataset,
        artifacts,
    )
    joined = " ".join(command)

    assert command[:3] == ["docker", "run", "--rm"]
    assert "--pull never" in joined
    assert "--network none" in joined
    assert "--cpus 1.0" in joined
    assert "--memory 512m" in joined
    assert "--pids-limit 256" in joined
    assert "--read-only" in command
    assert "--cap-drop ALL" in joined
    assert "--security-opt no-new-privileges:true" in joined
    assert "--tmpfs /tmp:rw,noexec,nosuid,size=64m" in joined
    assert f"type=bind,src={submission / 'source'},dst=/workspace,readonly" in command
    assert f"type=bind,src={dataset},dst=/data,readonly" in command
    assert f"type=bind,src={artifacts},dst=/artifacts" in command
    assert "quant-research-runner@sha256:" + "a" * 64 in command
    assert (
        f"type=bind,src={submission / 'submission.json'},dst=/run-contract/submission.json,readonly"
        in command
    )
    run_mount = next(
        value for value in command if "dst=/run-contract/run.json,readonly" in value
    )
    run_manifest_path = Path(run_mount.split("src=", 1)[1].split(",dst=", 1)[0])
    run_manifest = json.loads(run_manifest_path.read_text())
    assert len(run_manifest["run_id"]) == 64
    assert run_manifest["submission_id"] == submission.name
    assert run_manifest["dataset_snapshot_id"] == dataset.name
    assert run_manifest["runner_image"] == "quant-research-runner@sha256:" + "a" * 64
    assert command[-6:] == [
        "python",
        "/workspace/src/main.py",
        "--dataset=/data",
        "--submission=/run-contract/submission.json",
        "--run-contract=/run-contract/run.json",
        "--artifacts=/artifacts",
    ]
    assert "--privileged" not in command
    assert "-p" not in command
    assert "/var/run/docker.sock" not in joined


def test_isolation_rejects_protected_paths_and_tampered_runner_image(tmp_path: Path):
    _, submission, dataset, artifacts = _foundation(tmp_path)

    with pytest.raises(IsolationError, match="protected Feng path"):
        build_docker_command(
            submission,
            Path("/home/feng/quant-research/data"),
            artifacts,
        )
    with pytest.raises(IsolationError, match="protected Feng path"):
        build_docker_command(submission, Path("/home/feng"), artifacts)
    with pytest.raises(IsolationError, match="protected Feng path|symlink"):
        build_docker_command(submission, Path("/var/run"), artifacts)
    manifest_path = submission / "submission.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["runner_image"] = "runner:latest"
    manifest["spec"]["runner_image"] = "runner:latest"
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(IsolationError, match="submission integrity"):
        build_docker_command(submission, dataset, artifacts)


def test_isolation_rejects_arbitrary_or_reused_writable_artifact_directory(tmp_path: Path):
    _, submission, dataset, artifacts = _foundation(tmp_path)
    outside = tmp_path / "outside-artifacts"
    outside.mkdir()
    with pytest.raises(IsolationError, match="artifact directory"):
        build_docker_command(
            submission,
            dataset,
            outside,
        )

    (artifacts / "prior-result.json").write_text("{}")
    with pytest.raises(IsolationError, match="empty"):
        build_docker_command(
            submission,
            dataset,
            artifacts,
        )


def test_isolation_rejects_symlinked_artifact_directory(tmp_path: Path):
    _, submission, dataset, artifacts = _foundation(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    shutil.rmtree(artifacts)
    artifacts.symlink_to(outside, target_is_directory=True)

    with pytest.raises(IsolationError, match="symlink"):
        build_docker_command(submission, dataset, artifacts)


def test_isolation_rejects_tampered_submission_source(tmp_path: Path):
    _, submission, dataset, artifacts = _foundation(tmp_path)
    (submission / "source" / "src" / "main.py").write_text("print('tampered')\n")

    with pytest.raises(IsolationError, match="submission integrity"):
        build_docker_command(
            submission,
            dataset,
            artifacts,
        )


def test_isolation_rejects_dataset_other_than_submission_binding(tmp_path: Path):
    root, submission, _, artifacts = _foundation(tmp_path)
    other = publish_snapshot(
        _market_frame(close=6.19),
        root,
        {
            "instrument": "601288.SS",
            "provider": "synthetic",
            "market": "XSHG",
            "currency": "CNY",
            "adjustment": "unadjusted",
        },
    )

    with pytest.raises(IsolationError, match="dataset binding"):
        build_docker_command(
            submission,
            Path(other["path"]),
            artifacts,
        )


def test_isolation_rejects_tampered_execution_envelope(tmp_path: Path):
    _, submission, dataset, artifacts = _foundation(tmp_path)
    manifest_path = submission / "submission.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["execution_envelope"]["network"] = "bridge"
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(IsolationError, match="submission integrity"):
        build_docker_command(
            submission,
            dataset,
            artifacts,
        )
