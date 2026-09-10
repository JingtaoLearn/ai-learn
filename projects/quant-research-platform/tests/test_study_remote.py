from __future__ import annotations

import hashlib
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

from quant_platform.study_remote import (
    SignedStudyClient,
    StudyDispatcher,
    StudyPostgresStore,
    StudyTransportError,
    StudyWorkerServer,
    WorkerJobStore,
    canonical_json_bytes,
    freeze_synthetic_request,
    freeze_training_request,
)


IMAGE = "sha256:" + "1" * 64
COMMIT = "2" * 40
TREE = "3" * 40


class _AuthoritativeStore(StudyPostgresStore):
    def __init__(self):
        self.rows = {}
        self.fail_unavailable_calls = 0

    def admit(self, request, endpoint):
        row = self.rows.get(request["job_id"])
        if row is None:
            row = {
                "study_id": request["job_id"],
                "worker_endpoint": endpoint,
                "status": "ACCEPTED",
                "acceptance_ambiguous": False,
                "ambiguous_through_generation": 0,
                "dispatch_generation": 0,
                "dispatch_claim_id": None,
                "dispatch_claim_expires_at": None,
                "dispatch_post_started": False,
                "latest_progress": None,
                "final_result": None,
                "failure": None,
                "negative_receipt": None,
            }
            self.rows[request["job_id"]] = row
            return dict(row), True
        return dict(row), False

    def begin_dispatch(self, study_id, dispatch_claim_id, *, lease_seconds):
        row = self.rows[study_id]
        if row["status"] in {"SUCCEEDED", "FAILED"}:
            return dict(row), False
        if row["dispatch_post_started"]:
            row["acceptance_ambiguous"] = True
            row["ambiguous_through_generation"] = max(
                row["ambiguous_through_generation"], row["dispatch_generation"]
            )
        row["dispatch_generation"] += 1
        row["dispatch_claim_id"] = dispatch_claim_id
        row["dispatch_claim_expires_at"] = lease_seconds
        row["dispatch_post_started"] = False
        return dict(row), True

    def mark_post_started(self, study_id, dispatch_claim_id):
        row = self.rows[study_id]
        if row["dispatch_claim_id"] != dispatch_claim_id or row["status"] != "ACCEPTED":
            return False
        row["dispatch_post_started"] = True
        return True

    def mark_dispatched(self, study_id, remote, dispatch_claim_id):
        return self._record_remote(
            study_id, remote, dispatch=True, dispatch_claim_id=dispatch_claim_id
        )

    def observe(self, study_id, remote):
        return self._record_remote(study_id, remote, dispatch=False)

    def mark_acceptance_ambiguous(self, study_id, dispatch_claim_id):
        row = self.rows[study_id]
        row["acceptance_ambiguous"] |= (
            row["dispatch_claim_id"] == dispatch_claim_id and row["dispatch_post_started"]
        )
        if row["dispatch_claim_id"] == dispatch_claim_id and row["dispatch_post_started"]:
            row["ambiguous_through_generation"] = max(
                row["ambiguous_through_generation"], row["dispatch_generation"]
            )
        if row["dispatch_claim_id"] == dispatch_claim_id:
            row["dispatch_claim_id"] = None
            row["dispatch_claim_expires_at"] = None
            row["dispatch_post_started"] = False
        return dict(row)

    def _record_remote(self, study_id, remote, *, dispatch, dispatch_claim_id=None):
        row = self.rows[study_id]
        status = remote["status"]
        row["status"] = "DISPATCHED" if dispatch and status == "ACCEPTED" else status
        row["latest_progress"] = remote.get("progress")
        row["final_result"] = remote.get("result") if status == "SUCCEEDED" else None
        row["failure"] = remote.get("failure") if status == "FAILED" else None
        row["acceptance_ambiguous"] = False
        row["ambiguous_through_generation"] = 0
        row["negative_receipt"] = None
        row["dispatch_claim_id"] = None
        row["dispatch_claim_expires_at"] = None
        row["dispatch_post_started"] = False
        return dict(row)

    def resolve_fenced_absence(self, study_id, dispatch_claim_id, receipt, message):
        row = self.rows[study_id]
        if row["dispatch_claim_id"] != dispatch_claim_id:
            return dict(row)
        row["status"] = "FAILED"
        row["acceptance_ambiguous"] = False
        row["ambiguous_through_generation"] = 0
        row["failure"] = {
            "code": "FENG_UNAVAILABLE",
            "message": message,
            "local_compute_attempted": False,
            "negative_receipt_id": receipt["negative_receipt_id"],
        }
        row["negative_receipt"] = receipt
        row["dispatch_claim_id"] = None
        row["dispatch_claim_expires_at"] = None
        row["dispatch_post_started"] = False
        return dict(row)

    def fail_unavailable(self, study_id, dispatch_claim_id, message):
        self.fail_unavailable_calls += 1
        row = self.rows[study_id]
        if row["dispatch_claim_id"] != dispatch_claim_id:
            return dict(row)
        if row["acceptance_ambiguous"]:
            row["dispatch_claim_id"] = None
            row["dispatch_claim_expires_at"] = None
            row["dispatch_post_started"] = False
            return dict(row)
        row["status"] = "FAILED"
        row["failure"] = {
            "code": "FENG_UNAVAILABLE",
            "message": message,
            "local_compute_attempted": False,
        }
        row["dispatch_claim_id"] = None
        row["dispatch_claim_expires_at"] = None
        row["dispatch_post_started"] = False
        return dict(row)

    def get(self, study_id):
        row = self.rows.get(study_id)
        return None if row is None else dict(row)


class _LoseFirstAcceptedResponse(SignedStudyClient):
    def __init__(self, client):
        self.client = client
        self.endpoint = client.endpoint
        self.submit_calls = 0

    def submit(self, request, *, dispatch_generation=1, on_post_start=None):
        self.submit_calls += 1
        response = self.client.submit(
            request,
            dispatch_generation=dispatch_generation,
            on_post_start=on_post_start,
        )
        if self.submit_calls == 1:
            raise StudyTransportError("simulated response loss")
        return response

    def read(self, job_id):
        return self.client.read(job_id)

    def fence(self, request, through_generation):
        return self.client.fence(request, through_generation)


class _HideInitialAcceptedJob(SignedStudyClient):
    def __init__(self, client):
        self.client = client
        self.endpoint = client.endpoint
        self.submit_calls = 0
        self.read_calls = 0

    def submit(self, request, *, dispatch_generation=1, on_post_start=None):
        self.submit_calls += 1
        if self.submit_calls == 1:
            self.client.submit(
                request,
                dispatch_generation=dispatch_generation,
                on_post_start=on_post_start,
            )
            raise StudyTransportError("simulated accepted response loss")
        if self.submit_calls == 2:
            raise StudyTransportError("simulated unresolved replay")
        return self.client.submit(
            request,
            dispatch_generation=dispatch_generation,
            on_post_start=on_post_start,
        )

    def read(self, job_id):
        self.read_calls += 1
        if self.read_calls == 1:
            raise StudyTransportError("simulated unresolved immediate read")
        return self.client.read(job_id)

    def fence(self, request, through_generation):
        return self.client.fence(request, through_generation)


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


def test_real_optuna_training_kernel_keeps_only_bounded_aggregate_state(tmp_path: Path):
    private_key = _keypair(tmp_path)
    state_root = tmp_path / "state"
    store = WorkerJobStore(state_root, IMAGE)
    server = StudyWorkerServer(("127.0.0.1", 0), store, private_key.with_suffix(".pub.pem"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = SignedStudyClient(f"http://127.0.0.1:{server.server_port}", private_key)
    request = freeze_training_request(
        trial_budget=24,
        seed=17,
        checkpoint_count=4,
        parameter_low=0.0,
        parameter_high=1.0,
        objective_target=0.61803398875,
        source_commit=COMMIT,
        source_tree=TREE,
        worker_image=IMAGE,
    )
    try:
        first = client.submit(request)
        duplicate = client.submit(request)
        deadline = time.monotonic() + 15
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

    assert first.status_code == 202
    assert duplicate.status_code == 200
    assert job["status"] == "SUCCEEDED"
    assert job["result"]["conclusion"] == "SYNTHETIC_OBJECTIVE_SEARCH_COMPLETED"
    assert job["result"]["completed_trials"] == 24
    assert job["result"]["data_classification"] == "SYNTHETIC_NON_MARKET"
    assert job["progress"]["completed_trials"] == 24
    assert len(job["checkpoints"]) == 4
    assert "history" not in job and "trials" not in job
    files = list(state_root.iterdir())
    assert [path.name for path in files] == [f"{request['job_id']}.json"]
    assert files[0].stat().st_size < 16_384


def test_incomplete_training_restarts_from_frozen_request(tmp_path: Path):
    private_key = _keypair(tmp_path)
    state_root = tmp_path / "state"
    request = freeze_training_request(
        trial_budget=12,
        seed=29,
        checkpoint_count=3,
        parameter_low=0.0,
        parameter_high=1.0,
        objective_target=0.25,
        source_commit=COMMIT,
        source_tree=TREE,
        worker_image=IMAGE,
    )
    store = WorkerJobStore(state_root, IMAGE)
    store._write(
        {
            "schema_version": 1,
            "protocol": "quantresearch-study-worker/v1",
            "job_id": request["job_id"],
            "request_digest": hashlib.sha256(canonical_json_bytes(request)).hexdigest(),
            "frozen_request": request,
            "source_commit": COMMIT,
            "source_tree": TREE,
            "worker_image": IMAGE,
            "status": "RUNNING",
            "progress": {"completed_trials": 7, "total_trials": 12},
            "checkpoints": [{"sequence": 1, "completed_trials": 4}],
            "result": None,
            "failure": None,
            "updated_at": "2026-09-10T00:00:00Z",
        }
    )
    server = StudyWorkerServer(("127.0.0.1", 0), store, private_key.with_suffix(".pub.pem"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = SignedStudyClient(f"http://127.0.0.1:{server.server_port}", private_key)
    try:
        deadline = time.monotonic() + 15
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
    assert job["progress"]["completed_trials"] == 12
    assert len(job["checkpoints"]) == 3
    assert job["result"]["objective"]["target"] == 0.25


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


def test_unenveloped_v1_dispatch_is_rejected_without_worker_document(tmp_path: Path):
    private_key = _keypair(tmp_path)
    state_root = tmp_path / "state"
    server = StudyWorkerServer(
        ("127.0.0.1", 0),
        WorkerJobStore(state_root, IMAGE),
        private_key.with_suffix(".pub.pem"),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = SignedStudyClient(f"http://127.0.0.1:{server.server_port}", private_key)
    request = freeze_synthetic_request(
        iterations=10,
        seed=2,
        checkpoint_count=1,
        source_commit=COMMIT,
        source_tree=TREE,
        worker_image=IMAGE,
    )
    try:
        with pytest.raises(StudyTransportError, match="422 INVALID_REQUEST"):
            client._request("POST", "/v1/studies", canonical_json_bytes(request))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert list(state_root.iterdir()) == []


def test_accept_then_response_loss_converges_same_authoritative_row(tmp_path: Path):
    private_key = _keypair(tmp_path)
    state_root = tmp_path / "state"
    worker_store = WorkerJobStore(state_root, IMAGE)
    server = StudyWorkerServer(
        ("127.0.0.1", 0), worker_store, private_key.with_suffix(".pub.pem")
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = _LoseFirstAcceptedResponse(
        SignedStudyClient(f"http://127.0.0.1:{server.server_port}", private_key)
    )
    authoritative_store = _AuthoritativeStore()
    dispatcher = StudyDispatcher(authoritative_store, client)
    request = freeze_synthetic_request(
        iterations=100_000,
        seed=29,
        checkpoint_count=5,
        source_commit=COMMIT,
        source_tree=TREE,
        worker_image=IMAGE,
    )
    try:
        observed = dispatcher.submit(request)
        deadline = time.monotonic() + 10
        while observed["authoritative"]["status"] not in {"SUCCEEDED", "FAILED"}:
            assert time.monotonic() < deadline
            time.sleep(0.01)
            observed = dispatcher.read(request["job_id"])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert observed["authoritative"]["status"] == "SUCCEEDED"
    assert observed["authoritative"]["final_result"]["conclusion"] == "SYNTHETIC_MINIMUM_FOUND"
    assert observed["local_compute_attempted"] is False
    assert authoritative_store.fail_unavailable_calls == 0
    assert len(authoritative_store.rows) == 1
    assert client.submit_calls == 1
    assert [path.name for path in state_root.iterdir()] == [f"{request['job_id']}.json"]


def test_connection_refusal_remains_explicit_no_fallback_failure(tmp_path: Path):
    private_key = _keypair(tmp_path)
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    authoritative_store = _AuthoritativeStore()
    dispatcher = StudyDispatcher(
        authoritative_store,
        SignedStudyClient(f"http://127.0.0.1:{port}", private_key, timeout_seconds=0.1),
    )
    request = freeze_synthetic_request(
        iterations=10,
        seed=1,
        checkpoint_count=1,
        source_commit=COMMIT,
        source_tree=TREE,
        worker_image=IMAGE,
    )

    observed = dispatcher.submit(request)

    assert observed["authoritative"]["status"] == "FAILED"
    assert observed["authoritative"]["failure"]["code"] == "FENG_UNAVAILABLE"
    assert observed["authoritative"]["failure"]["local_compute_attempted"] is False
    assert observed["local_compute_attempted"] is False
    assert authoritative_store.fail_unavailable_calls == 1
    assert len(authoritative_store.rows) == 1


def test_prior_acceptance_ambiguity_survives_later_connection_refusal(tmp_path: Path):
    private_key = _keypair(tmp_path)
    state_root = tmp_path / "state"
    worker_store = WorkerJobStore(state_root, IMAGE)
    server = StudyWorkerServer(
        ("127.0.0.1", 0), worker_store, private_key.with_suffix(".pub.pem")
    )
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    underlying_client = SignedStudyClient(f"http://127.0.0.1:{port}", private_key)
    client = _HideInitialAcceptedJob(underlying_client)
    authoritative_store = _AuthoritativeStore()
    dispatcher = StudyDispatcher(authoritative_store, client)
    request = freeze_synthetic_request(
        iterations=100_000,
        seed=37,
        checkpoint_count=5,
        source_commit=COMMIT,
        source_tree=TREE,
        worker_image=IMAGE,
    )

    first = dispatcher.submit(request)
    deadline = time.monotonic() + 10
    while True:
        remote_before_retry = underlying_client.read(request["job_id"]).value["job"]
        if remote_before_retry["status"] in {"SUCCEEDED", "FAILED"}:
            break
        assert time.monotonic() < deadline
        time.sleep(0.01)
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)

    retry = dispatcher.submit(request)

    server = StudyWorkerServer(
        ("127.0.0.1", port), worker_store, private_key.with_suffix(".pub.pem")
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        converged = dispatcher.read(request["job_id"])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert first["authoritative"]["status"] == "ACCEPTED"
    assert first["authoritative"]["acceptance_ambiguous"] is True
    assert first["remote"] is None
    assert remote_before_retry["status"] == "SUCCEEDED"
    assert retry["authoritative"]["status"] == "ACCEPTED"
    assert retry["authoritative"]["failure"] is None
    assert retry["remote"] is None
    assert retry["local_compute_attempted"] is False
    assert converged["authoritative"]["status"] == "SUCCEEDED"
    assert converged["authoritative"]["final_result"] == remote_before_retry["result"]
    assert converged["local_compute_attempted"] is False
    assert authoritative_store.fail_unavailable_calls == 0
    assert len(authoritative_store.rows) == 1
    assert client.submit_calls == 2
    assert [path.name for path in state_root.iterdir()] == [f"{request['job_id']}.json"]
