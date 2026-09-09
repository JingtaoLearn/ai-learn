from __future__ import annotations

import os
import socket
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from quant_platform.study_remote import (
    SignedStudyClient,
    StudyDispatcher,
    StudyPostgresStore,
    StudyTransportError,
    StudyValidationError,
    StudyWorkerServer,
    WorkerJobStore,
    freeze_synthetic_request,
)


pytestmark = pytest.mark.skipif(
    not os.environ.get("QR_STUDY_POSTGRES_PASSWORD_FILE"),
    reason="requires disposable PostgreSQL connection settings",
)

IMAGE = "sha256:" + "4" * 64
COMMIT = "5" * 40
TREE = "6" * 40


def _keypair(root: Path) -> Path:
    private_key = root / "control-private.pem"
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
            str(private_key.with_suffix(".pub.pem")),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return private_key


class _PauseBeforePost(SignedStudyClient):
    def __init__(self, client: SignedStudyClient):
        self.client = client
        self.endpoint = client.endpoint
        self.entered = threading.Event()
        self.release = threading.Event()

    def submit(self, request):
        self.entered.set()
        assert self.release.wait(timeout=10)
        return self.client.submit(request)

    def read(self, job_id):
        return self.client.read(job_id)


class _PauseAfterAcceptedResponse(SignedStudyClient):
    def __init__(self, client: SignedStudyClient):
        self.client = client
        self.endpoint = client.endpoint
        self.submit_calls = 0
        self.read_calls = 0
        self.accepted = threading.Event()
        self.release = threading.Event()

    def submit(self, request):
        self.submit_calls += 1
        if self.submit_calls == 1:
            self.client.submit(request)
            self.accepted.set()
            assert self.release.wait(timeout=10)
            raise StudyTransportError("simulated accepted response loss")
        raise StudyTransportError("simulated unresolved replay")

    def read(self, job_id):
        self.read_calls += 1
        raise StudyTransportError("simulated unresolved immediate read")


def _request(*, seed: int):
    return freeze_synthetic_request(
        iterations=100_000,
        seed=seed,
        checkpoint_count=5,
        source_commit=COMMIT,
        source_tree=TREE,
        worker_image=IMAGE,
    )


def test_concurrent_caller_cannot_erase_durable_acceptance_ambiguity(tmp_path: Path):
    store = StudyPostgresStore.from_environment()
    store.initialize()
    seed = int.from_bytes(os.urandom(4), "big")
    private_key = _keypair(tmp_path)
    worker_store = WorkerJobStore(tmp_path / "worker-state", IMAGE)
    server = StudyWorkerServer(
        ("127.0.0.1", 0), worker_store, private_key.with_suffix(".pub.pem")
    )
    port = server.server_port
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    endpoint = f"http://127.0.0.1:{port}"
    creator_client = _PauseBeforePost(SignedStudyClient(endpoint, private_key, timeout_seconds=0.2))
    creator = StudyDispatcher(store, creator_client)
    later_client = _PauseAfterAcceptedResponse(
        SignedStudyClient(endpoint, private_key, timeout_seconds=0.2)
    )
    later = StudyDispatcher(store, later_client)
    request = _request(seed=seed)

    with ThreadPoolExecutor(max_workers=2) as pool:
        creator_result = pool.submit(creator.submit, request)
        assert creator_client.entered.wait(timeout=10)
        later_future = pool.submit(later.submit, request)
        assert later_client.accepted.wait(timeout=10)
        authoritative_after_acceptance = store.get(request["job_id"])
        assert not later_future.done()
        deadline = time.monotonic() + 10
        while True:
            remote = SignedStudyClient(endpoint, private_key).read(request["job_id"]).value["job"]
            if remote["status"] in {"SUCCEEDED", "FAILED"}:
                break
            assert time.monotonic() < deadline
            time.sleep(0.01)
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)
        creator_client.release.set()
        refused_result = creator_result.result(timeout=10)
        authoritative_after_refusal = store.get(request["job_id"])
        assert not later_future.done()
        later_client.release.set()
        later_result = later_future.result(timeout=10)

    assert authoritative_after_acceptance is not None
    assert authoritative_after_acceptance["status"] == "ACCEPTED"
    assert authoritative_after_acceptance["acceptance_ambiguous"] is True
    assert authoritative_after_refusal is not None
    server = StudyWorkerServer(
        ("127.0.0.1", port), worker_store, private_key.with_suffix(".pub.pem")
    )
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        reader = StudyDispatcher(
            store, SignedStudyClient(endpoint, private_key, timeout_seconds=0.2)
        )
        converged = reader.read(request["job_id"])
        terminal_before_replay = store.get(request["job_id"])
        terminal_replay = reader.read(request["job_id"])
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)

    admission_request = _request(seed=seed ^ 1)
    with ThreadPoolExecutor(max_workers=16) as pool:
        admissions = list(
            pool.map(lambda _: store.admit(admission_request, endpoint), range(16))
        )

    assert later_result["authoritative"]["status"] == "ACCEPTED"
    assert later_result["authoritative"]["acceptance_ambiguous"] is True
    assert later_result["remote"] is None
    assert remote["status"] == "SUCCEEDED"
    assert refused_result["authoritative"]["status"] == "ACCEPTED"
    assert refused_result["authoritative"]["failure"] is None
    assert authoritative_after_refusal["status"] == "ACCEPTED"
    assert authoritative_after_refusal["acceptance_ambiguous"] is True
    assert converged["authoritative"]["status"] == "SUCCEEDED"
    assert converged["authoritative"]["final_result"] == remote["result"]
    assert converged["local_compute_attempted"] is False
    assert terminal_replay["authoritative"] == terminal_before_replay
    storage_shape = store.storage_shape(request["job_id"])
    assert storage_shape["study_rows"] == 1
    assert storage_shape["attempt_rows"] == 0
    assert storage_shape["experiment_rows"] == 0
    assert storage_shape["per_iteration_event_rows"] == 0
    assert storage_shape["tables"] == ["jobs", "schema_identity"]
    assert [path.name for path in (tmp_path / "worker-state").iterdir()] == [
        f"{request['job_id']}.json"
    ]
    assert sum(created for _, created in admissions) == 1
    assert all(row["study_id"] == admission_request["job_id"] for row, _ in admissions)
    conflicting = dict(request)
    conflicting["seed"] ^= 1
    with pytest.raises(StudyValidationError, match="identity does not match"):
        store.admit(conflicting, endpoint)

    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        closed_port = reservation.getsockname()[1]
    fresh = StudyDispatcher(
        store,
        SignedStudyClient(
            f"http://127.0.0.1:{closed_port}", private_key, timeout_seconds=0.1
        ),
    ).submit(_request(seed=seed ^ 2))
    assert fresh["authoritative"]["status"] == "FAILED"
    assert fresh["authoritative"]["failure"]["code"] == "FENG_UNAVAILABLE"
    assert fresh["local_compute_attempted"] is False