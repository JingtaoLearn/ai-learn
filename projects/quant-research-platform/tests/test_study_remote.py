from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

import pytest

from quant_platform.study_remote import (
    SignedStudyClient,
    StudyTransportError,
    StudyWorkerServer,
    WorkerJobStore,
    freeze_synthetic_request,
)


IMAGE = "sha256:" + "1" * 64
COMMIT = "2" * 40
TREE = "3" * 40


def _keypair(root: Path, name: str = "control-private") -> Path:
    private_key = root / f"{name}.pem"
    public_key = root / f"{name}.pub.pem"
    subprocess.run(
        ["openssl", "genpkey", "-algorithm", "ED25519", "-out", str(private_key)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        [
            "openssl",
            "pkey",
            "-in",
            str(private_key),
            "-pubout",
            "-out",
            str(public_key),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return private_key


def test_many_iterations_are_one_bounded_idempotent_worker_job(tmp_path: Path):
    private_key = _keypair(tmp_path)
    state_root = tmp_path / "state"
    store = WorkerJobStore(state_root, IMAGE)
    server = StudyWorkerServer(("127.0.0.1", 0), store, private_key.with_suffix(".pub.pem"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = SignedStudyClient(f"http://127.0.0.1:{server.server_port}", private_key)
    request = freeze_synthetic_request(
        iterations=100_000,
        seed=17,
        checkpoint_count=5,
        source_commit=COMMIT,
        source_tree=TREE,
        worker_image=IMAGE,
    )
    try:
        first = client.submit(request)
        duplicate = client.submit(request)
        assert first.status_code == 202
        assert duplicate.status_code == 200
        assert duplicate.value["idempotent_replay"] is True
        deadline = time.monotonic() + 10
        while True:
            job = client.read(request["job_id"]).value["job"]
            if job["status"] in {"SUCCEEDED", "FAILED"}:
                break
            assert time.monotonic() < deadline
            time.sleep(0.01)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert job["status"] == "SUCCEEDED"
    assert job["result"]["iterations"] == 100_000
    assert job["result"]["checkpoint_count"] == 5
    assert len(job["checkpoints"]) == 5
    assert job["source_commit"] == COMMIT
    assert job["source_tree"] == TREE
    assert job["worker_image"] == IMAGE
    files = list(state_root.iterdir())
    assert [path.name for path in files] == [f"{request['job_id']}.json"]
    assert files[0].stat().st_size < 16_384


def test_wrong_signing_identity_is_rejected_without_dispatch(tmp_path: Path):
    admitted_key = _keypair(tmp_path, "admitted-private")
    wrong_key = _keypair(tmp_path, "wrong-private")
    state_root = tmp_path / "state"
    server = StudyWorkerServer(
        ("127.0.0.1", 0),
        WorkerJobStore(state_root, IMAGE),
        admitted_key.with_suffix(".pub.pem"),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = SignedStudyClient(f"http://127.0.0.1:{server.server_port}", wrong_key)
    request = freeze_synthetic_request(
        iterations=10,
        seed=1,
        checkpoint_count=1,
        source_commit=COMMIT,
        source_tree=TREE,
        worker_image=IMAGE,
    )
    try:
        with pytest.raises(StudyTransportError, match="403 AUTHENTICATION_REJECTED"):
            client.submit(request)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert list(state_root.iterdir()) == []
