import json
from pathlib import Path

import pandas as pd
import pytest

from quant_platform.datasets import publish_snapshot
from quant_platform.submissions import (
    SubmissionValidationError,
    publish_submission,
    submission_status,
)


def _dataset(root: Path) -> str:
    frame = pd.DataFrame(
        {
            "Date": pd.to_datetime(["2026-08-18", "2026-08-19"]),
            "Open": [6.12, 6.18],
            "High": [6.20, 6.24],
            "Low": [6.08, 6.14],
            "Close": [6.18, 6.20],
            "Volume": [1200, 1100],
        }
    )
    result = publish_snapshot(
        frame,
        root,
        {
            "instrument": "601288.SS",
            "provider": "synthetic",
            "market": "XSHG",
            "currency": "CNY",
            "adjustment": "unadjusted",
        },
    )
    return result["snapshot_id"]


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    (project / "src" / "strategy").mkdir(parents=True)
    (project / "tests").mkdir()
    (project / "src" / "strategy" / "main.py").write_text("def run():\n    return 42\n")
    (project / "tests" / "test_main.py").write_text(
        "from strategy.main import run\n\ndef test_run():\n    assert run() == 42\n"
    )
    (project / "tests" / "test_browser.js").write_text("console.log('deterministic test')\n")
    (project / "src" / "strategy" / "config.json").write_text('{"version": 1}\n')
    (project / "src" / "sample.egg-info").mkdir()
    (project / "src" / "sample.egg-info" / "PKG-INFO").write_text("generated metadata")
    (project / "pyproject.toml").write_text("[project]\nname='sample'\nversion='0.1'\n")
    (project / "requirements.in").write_text("pandas==2.3.1\n")
    (project / "requirements.lock").write_text("# synthetic lock\n")
    (project / "runs").mkdir()
    (project / "runs" / "ignored.json").write_text("{}")
    (project / ".git").mkdir()
    (project / ".git" / "config").write_text("ignored")
    return project


def _spec(snapshot_id: str) -> dict:
    return {
        "name": "abc-breakout-baseline",
        "entrypoint": "src/strategy/main.py",
        "dataset_snapshot_id": snapshot_id,
        "runner_image": "quant-runner@sha256:" + "a" * 64,
        "config": {"entry_window": 20, "exit_window": 10},
        "seed": 20260820,
    }


def test_submission_is_replayable_content_addressed_and_idempotent(tmp_path: Path):
    root = tmp_path / "state"
    snapshot_id = _dataset(root)
    project = _project(tmp_path)

    first = publish_submission(_spec(snapshot_id), project, root)
    second = publish_submission(_spec(snapshot_id), project, root)

    assert first["status"] == "CREATED"
    assert second["status"] == "NO_CHANGE"
    assert first["submission_id"] == second["submission_id"]

    submission_dir = Path(first["path"])
    manifest = json.loads((submission_dir / "submission.json").read_text())
    assert (submission_dir / "source" / "src" / "strategy" / "main.py").exists()
    assert (submission_dir / "source" / "src" / "strategy" / "config.json").exists()
    assert (submission_dir / "source" / "tests" / "test_main.py").exists()
    assert (submission_dir / "source" / "tests" / "test_browser.js").exists()
    assert not (submission_dir / "source" / "runs").exists()
    assert not (submission_dir / "source" / ".git").exists()
    assert not (submission_dir / "source" / "src" / "sample.egg-info").exists()
    assert manifest["dataset_snapshot_id"] == snapshot_id
    assert manifest["runner_image"] == _spec(snapshot_id)["runner_image"]
    assert "dataset_path" not in manifest
    assert "created_at" not in manifest
    assert len(manifest["source_sha256"]) == 64
    assert all(len(value) == 64 for value in manifest["source_files"].values())
    assert manifest["execution_envelope"] == {
        "cap_drop": ["ALL"],
        "cpus": 1.0,
        "memory_mib": 512,
        "network": "none",
        "no_new_privileges": True,
        "pids_limit": 256,
        "read_only_root": True,
    }
    assert (submission_dir.stat().st_mode & 0o777) == 0o755
    assert ((submission_dir / "submission.json").stat().st_mode & 0o777) == 0o644
    assert submission_status(root, first["submission_id"])["path"] == first["path"]


def test_source_change_creates_new_submission_without_overwriting_old(tmp_path: Path):
    root = tmp_path / "state"
    snapshot_id = _dataset(root)
    project = _project(tmp_path)
    first = publish_submission(_spec(snapshot_id), project, root)
    (project / "src" / "strategy" / "main.py").write_text("def run():\n    return 43\n")
    second = publish_submission(_spec(snapshot_id), project, root)

    assert second["submission_id"] != first["submission_id"]
    assert Path(first["path"]).exists()
    assert Path(second["path"]).exists()


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (lambda spec: spec | {"unknown": True}, "unknown specification fields"),
        (lambda spec: spec | {"entrypoint": "/tmp/main.py"}, "relative"),
        (lambda spec: spec | {"entrypoint": "../main.py"}, "path traversal"),
        (lambda spec: spec | {"entrypoint": "src/missing.py"}, "does not exist"),
        (lambda spec: spec | {"dataset_snapshot_id": "0" * 64}, "dataset snapshot"),
    ],
)
def test_submission_rejects_invalid_specification(tmp_path: Path, mutator, message: str):
    root = tmp_path / "state"
    snapshot_id = _dataset(root)
    project = _project(tmp_path)
    with pytest.raises(SubmissionValidationError, match=message):
        publish_submission(mutator(_spec(snapshot_id)), project, root)


def test_submission_rejects_symlinks_and_secret_like_files(tmp_path: Path):
    root = tmp_path / "state"
    snapshot_id = _dataset(root)
    project = _project(tmp_path)
    (project / "src" / "strategy" / "link.py").symlink_to(project / "src" / "strategy" / "main.py")
    with pytest.raises(SubmissionValidationError, match="symlink"):
        publish_submission(_spec(snapshot_id), project, root)

    (project / "src" / "strategy" / "link.py").unlink()
    (project / ".env").write_text("DO_NOT_READ=opaque\n")
    with pytest.raises(SubmissionValidationError, match="secret-like"):
        publish_submission(_spec(snapshot_id), project, root)


def test_submission_rejects_entrypoint_outside_allowlisted_bundle(tmp_path: Path):
    root = tmp_path / "state"
    snapshot_id = _dataset(root)
    project = _project(tmp_path)
    (project / "README.md").write_text("not executable source")

    with pytest.raises(SubmissionValidationError, match="allowlisted source bundle"):
        publish_submission(
            _spec(snapshot_id) | {"entrypoint": "README.md"}, project, root
        )


def test_existing_submission_corruption_fails_closed(tmp_path: Path):
    root = tmp_path / "state"
    snapshot_id = _dataset(root)
    project = _project(tmp_path)
    published = publish_submission(_spec(snapshot_id), project, root)
    (Path(published["path"]) / "source" / "src" / "strategy" / "main.py").write_text(
        "def run():\n    return 'tampered'\n"
    )

    with pytest.raises(RuntimeError, match="corrupt submission"):
        publish_submission(_spec(snapshot_id), project, root)


def test_submission_requires_replay_dependencies_and_all_tests(tmp_path: Path):
    root = tmp_path / "state"
    snapshot_id = _dataset(root)
    project = _project(tmp_path)
    (project / "requirements.lock").unlink()

    with pytest.raises(SubmissionValidationError, match="requirements.lock"):
        publish_submission(_spec(snapshot_id), project, root)


@pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), float("-inf")])
def test_submission_rejects_nonstandard_json_numbers(tmp_path: Path, bad_value: float):
    root = tmp_path / "state"
    snapshot_id = _dataset(root)
    project = _project(tmp_path)
    spec = _spec(snapshot_id) | {"config": {"threshold": bad_value}}

    with pytest.raises(SubmissionValidationError, match="finite JSON"):
        publish_submission(spec, project, root)


def test_submission_rejects_fabricated_dataset_snapshot(tmp_path: Path):
    root = tmp_path / "state"
    fake_id = "f" * 64
    fake = root / "datasets" / "601288.SS" / fake_id
    fake.mkdir(parents=True)
    (fake / "manifest.json").write_text("{}")
    (fake / "data.parquet").write_bytes(b"not parquet")
    project = _project(tmp_path)

    with pytest.raises(SubmissionValidationError, match="dataset snapshot integrity"):
        publish_submission(_spec(fake_id), project, root)


def test_submission_requires_digest_pinned_runner_image(tmp_path: Path):
    root = tmp_path / "state"
    snapshot_id = _dataset(root)
    project = _project(tmp_path)

    with pytest.raises(SubmissionValidationError, match="runner_image"):
        publish_submission(_spec(snapshot_id) | {"runner_image": "runner:latest"}, project, root)


def test_submission_accepts_immutable_local_docker_image_id(tmp_path: Path):
    root = tmp_path / "state"
    snapshot_id = _dataset(root)
    project = _project(tmp_path)
    spec = _spec(snapshot_id) | {"runner_image": "sha256:" + "b" * 64}

    assert publish_submission(spec, project, root)["status"] == "CREATED"


def test_submission_rejects_hardcoded_credential_content(tmp_path: Path):
    root = tmp_path / "state"
    snapshot_id = _dataset(root)
    project = _project(tmp_path)
    credential_name = "api" + "_key"
    credential_value = "not-a-" + "placeholder-secret"
    (project / "src" / "strategy" / "main.py").write_text(
        f'{credential_name} = "{credential_value}"\n'
    )

    with pytest.raises(SubmissionValidationError, match="credential content"):
        publish_submission(_spec(snapshot_id), project, root)
