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
    SCHEMA_IDENTITY,
    SignedStudyClient,
    StudyDispatcher,
    StudyPostgresStore,
    StudyRemoteError,
    StudyTransportError,
    StudyValidationError,
    StudyWorkerServer,
    WorkerJobStore,
    canonical_json_bytes,
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

    def submit(self, request, *, on_post_start=None):
        self.entered.set()
        assert self.release.wait(timeout=10)
        return self.client.submit(request, on_post_start=on_post_start)

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

    def submit(self, request, *, on_post_start=None):
        self.submit_calls += 1
        if self.submit_calls == 1:
            self.client.submit(request, on_post_start=on_post_start)
            self.accepted.set()
            assert self.release.wait(timeout=10)
            raise StudyTransportError("simulated accepted response loss")
        raise StudyTransportError("simulated unresolved replay")

    def read(self, job_id):
        self.read_calls += 1
        raise StudyTransportError("simulated unresolved immediate read")


class _PauseAfterResponse(SignedStudyClient):
    def __init__(self, client: SignedStudyClient, *, lose_response: bool):
        self.client = client
        self.endpoint = client.endpoint
        self.lose_response = lose_response
        self.accepted = threading.Event()
        self.release = threading.Event()

    def submit(self, request, *, on_post_start=None):
        response = self.client.submit(request, on_post_start=on_post_start)
        self.accepted.set()
        assert self.release.wait(timeout=10)
        if self.lose_response:
            raise StudyTransportError("simulated accepted response loss")
        return response

    def read(self, job_id):
        return self.client.read(job_id)


class _GateBeforeAcceptanceStore(WorkerJobStore):
    def __init__(self, root: Path, worker_image: str):
        super().__init__(root, worker_image)
        self.request_in_handler = threading.Event()
        self.release_acceptance = threading.Event()

    def submit(self, request):
        self.request_in_handler.set()
        assert self.release_acceptance.wait(timeout=10)
        return super().submit(request)


def _start_server(store: WorkerJobStore, public_key: Path, port: int = 0):
    server = StudyWorkerServer(("127.0.0.1", port), store, public_key)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _stop_accepting(server: StudyWorkerServer, thread: threading.Thread) -> None:
    server.shutdown()
    thread.join(timeout=2)
    assert not thread.is_alive()
    server.socket.close()


def _wait_terminal(store: WorkerJobStore, study_id: str) -> dict[str, object]:
    deadline = time.monotonic() + 10
    while True:
        state = store.status(study_id)
        if state is not None and state["status"] in {"SUCCEEDED", "FAILED"}:
            return state
        assert time.monotonic() < deadline
        time.sleep(0.01)


def _request(*, seed: int):
    return freeze_synthetic_request(
        iterations=100_000,
        seed=seed,
        checkpoint_count=5,
        source_commit=COMMIT,
        source_tree=TREE,
        worker_image=IMAGE,
    )


@pytest.mark.parametrize("lose_response", [False, True])
def test_expired_post_claim_refusal_preserves_accepted_feng_work(
    tmp_path: Path, lose_response: bool
):
    store = StudyPostgresStore.from_environment()
    store.initialize()
    private_key = _keypair(tmp_path)
    worker_store = WorkerJobStore(tmp_path / "worker-state", IMAGE)
    server, server_thread = _start_server(worker_store, private_key.with_suffix(".pub.pem"))
    endpoint = f"http://127.0.0.1:{server.server_port}"
    paused = _PauseAfterResponse(
        SignedStudyClient(endpoint, private_key, timeout_seconds=2),
        lose_response=lose_response,
    )
    first = StudyDispatcher(store, paused)
    first.DISPATCH_LEASE_SECONDS = 1
    competing = StudyDispatcher(
        store, SignedStudyClient(endpoint, private_key, timeout_seconds=0.2)
    )
    request = _request(seed=int.from_bytes(os.urandom(4), "big"))

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(first.submit, request)
        assert paused.accepted.wait(timeout=10)
        after_acceptance = store.get(request["job_id"])
        time.sleep(1.1)
        _stop_accepting(server, server_thread)
        refused = competing.submit(request)
        after_refusal = store.get(request["job_id"])
        paused.release.set()
        first_result = first_future.result(timeout=10)

    remote_terminal = _wait_terminal(worker_store, request["job_id"])
    server.server_close()
    rebound, rebound_thread = _start_server(
        worker_store, private_key.with_suffix(".pub.pem"), int(endpoint.rsplit(":", 1)[1])
    )
    try:
        converged = StudyDispatcher(
            store, SignedStudyClient(endpoint, private_key, timeout_seconds=0.5)
        ).read(request["job_id"])
    finally:
        rebound.shutdown()
        rebound.server_close()
        rebound_thread.join(timeout=2)

    assert after_acceptance is not None
    assert after_acceptance["status"] == "ACCEPTED"
    assert after_acceptance["dispatch_post_started"] is True
    assert after_refusal is not None
    assert after_refusal["status"] == "ACCEPTED"
    assert after_refusal["failure"] is None
    assert after_refusal["acceptance_ambiguous"] is True
    assert refused["authoritative"]["status"] == "ACCEPTED"
    assert first_result["authoritative"]["status"] in {"ACCEPTED", "DISPATCHED", "SUCCEEDED"}
    assert remote_terminal["status"] == "SUCCEEDED"
    assert converged["authoritative"]["status"] == "SUCCEEDED"
    assert converged["authoritative"]["final_result"] == remote_terminal["result"]
    assert len(list(worker_store.root.iterdir())) == 1
    assert converged["local_compute_attempted"] is False
    storage_shape = store.storage_shape(request["job_id"])
    assert storage_shape["study_rows"] == 1
    assert storage_shape["attempt_rows"] == 0
    assert storage_shape["experiment_rows"] == 0
    assert storage_shape["per_iteration_event_rows"] == 0
    print(
        canonical_json_bytes(
            {
                "scenario": (
                    "accepted_response_loss_after_expiry"
                    if lose_response
                    else "accepted_delayed_response_after_expiry"
                ),
                "after_acceptance": after_acceptance,
                "after_reclaimed_refusal": after_refusal,
                "first_result": first_result["authoritative"],
                "feng_terminal": remote_terminal,
                "converged_postgresql": converged["authoritative"],
                "worker_document_count": len(list(worker_store.root.iterdir())),
                "storage_shape": storage_shape,
                "local_compute_attempted": converged["local_compute_attempted"],
            }
        ).decode("utf-8")
    )


def test_inflight_post_accepted_after_expiry_survives_reclaimed_refusal(tmp_path: Path):
    store = StudyPostgresStore.from_environment()
    store.initialize()
    private_key = _keypair(tmp_path)
    worker_store = _GateBeforeAcceptanceStore(tmp_path / "worker-state", IMAGE)
    server, server_thread = _start_server(worker_store, private_key.with_suffix(".pub.pem"))
    endpoint = f"http://127.0.0.1:{server.server_port}"
    first = StudyDispatcher(store, SignedStudyClient(endpoint, private_key, timeout_seconds=5))
    first.DISPATCH_LEASE_SECONDS = 1
    competing = StudyDispatcher(
        store, SignedStudyClient(endpoint, private_key, timeout_seconds=0.2)
    )
    request = _request(seed=int.from_bytes(os.urandom(4), "big"))

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(first.submit, request)
        assert worker_store.request_in_handler.wait(timeout=10)
        in_flight = store.get(request["job_id"])
        time.sleep(1.1)
        _stop_accepting(server, server_thread)
        refused = competing.submit(request)
        after_refusal = store.get(request["job_id"])
        worker_store.release_acceptance.set()
        first_result = first_future.result(timeout=10)

    remote_terminal = _wait_terminal(worker_store, request["job_id"])
    server.server_close()
    rebound, rebound_thread = _start_server(
        worker_store, private_key.with_suffix(".pub.pem"), int(endpoint.rsplit(":", 1)[1])
    )
    try:
        converged = StudyDispatcher(
            store, SignedStudyClient(endpoint, private_key, timeout_seconds=0.5)
        ).read(request["job_id"])
    finally:
        rebound.shutdown()
        rebound.server_close()
        rebound_thread.join(timeout=2)

    assert in_flight is not None
    assert in_flight["status"] == "ACCEPTED"
    assert in_flight["dispatch_post_started"] is True
    assert after_refusal is not None
    assert after_refusal["status"] == "ACCEPTED"
    assert after_refusal["failure"] is None
    assert after_refusal["acceptance_ambiguous"] is True
    assert refused["authoritative"]["status"] == "ACCEPTED"
    assert first_result["authoritative"]["status"] in {"DISPATCHED", "SUCCEEDED"}
    assert remote_terminal["status"] == "SUCCEEDED"
    assert converged["authoritative"]["status"] == "SUCCEEDED"
    assert converged["authoritative"]["final_result"] == remote_terminal["result"]
    assert len(list(worker_store.root.iterdir())) == 1
    assert converged["local_compute_attempted"] is False
    storage_shape = store.storage_shape(request["job_id"])
    assert storage_shape["study_rows"] == 1
    assert storage_shape["attempt_rows"] == 0
    assert storage_shape["experiment_rows"] == 0
    assert storage_shape["per_iteration_event_rows"] == 0
    print(
        canonical_json_bytes(
            {
                "scenario": "signed_post_in_flight_acceptance_after_expiry",
                "in_flight_before_expiry": in_flight,
                "after_reclaimed_refusal": after_refusal,
                "first_result": first_result["authoritative"],
                "feng_terminal": remote_terminal,
                "converged_postgresql": converged["authoritative"],
                "worker_document_count": len(list(worker_store.root.iterdir())),
                "storage_shape": storage_shape,
                "local_compute_attempted": converged["local_compute_attempted"],
            }
        ).decode("utf-8")
    )


def test_owned_claim_protects_success_from_stale_refusal(tmp_path: Path):
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
    creator.DISPATCH_LEASE_SECONDS = 1
    later_client = _PauseAfterAcceptedResponse(
        SignedStudyClient(endpoint, private_key, timeout_seconds=0.2)
    )
    later = StudyDispatcher(store, later_client)
    request = _request(seed=seed)

    with ThreadPoolExecutor(max_workers=2) as pool:
        creator_result = pool.submit(creator.submit, request)
        assert creator_client.entered.wait(timeout=10)
        time.sleep(1.1)
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
    assert authoritative_after_acceptance["acceptance_ambiguous"] is False
    assert authoritative_after_acceptance["dispatch_claim_id"] is not None
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
    assert authoritative_after_refusal["acceptance_ambiguous"] is False
    assert authoritative_after_refusal["dispatch_claim_id"] == authoritative_after_acceptance[
        "dispatch_claim_id"
    ]
    assert converged["authoritative"]["status"] == "SUCCEEDED"
    assert converged["authoritative"]["final_result"] == remote["result"]
    assert converged["authoritative"]["acceptance_ambiguous"] is False
    assert converged["authoritative"]["dispatch_claim_id"] is None
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
    print(
        canonical_json_bytes(
            {
                "scenario": "successful_b_stale_refused_a",
                "after_valid_feng_response_before_record": authoritative_after_acceptance,
                "after_stale_refusal": authoritative_after_refusal,
                "ambiguous_submit_result": later_result,
                "feng_terminal_read": remote,
                "converged_postgresql": converged["authoritative"],
                "terminal_replay_immutable": terminal_replay["authoritative"]
                == terminal_before_replay,
                "storage_shape": storage_shape,
                "worker_documents": [
                    path.name for path in (tmp_path / "worker-state").iterdir()
                ],
                "created_admissions": sum(created for _, created in admissions),
                "local_compute_attempted": converged["local_compute_attempted"],
            }
        ).decode("utf-8")
    )


def test_refusal_and_abandoned_pre_acceptance_claims_resolve(tmp_path: Path):
    store = StudyPostgresStore.from_environment()
    store.initialize()
    seed = int.from_bytes(os.urandom(4), "big")
    private_key = _keypair(tmp_path)

    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        closed_port = reservation.getsockname()[1]
    dispatcher = StudyDispatcher(
        store,
        SignedStudyClient(
            f"http://127.0.0.1:{closed_port}", private_key, timeout_seconds=0.1
        ),
    )

    fresh_request = _request(seed=seed)
    fresh = dispatcher.submit(fresh_request)
    assert fresh["authoritative"]["status"] == "FAILED"
    assert fresh["authoritative"]["failure"]["code"] == "FENG_UNAVAILABLE"
    assert fresh["authoritative"]["acceptance_ambiguous"] is False
    assert fresh["authoritative"]["dispatch_claim_id"] is None

    duplicate = dispatcher.submit(fresh_request)
    assert duplicate["authoritative"] == fresh["authoritative"]

    before_claim = _request(seed=seed ^ 1)
    store.admit(before_claim, dispatcher.client.endpoint)
    recovered_before = dispatcher.submit(before_claim)
    assert recovered_before["authoritative"]["status"] == "FAILED"
    assert recovered_before["authoritative"]["acceptance_ambiguous"] is False

    after_claim = _request(seed=seed ^ 2)
    store.admit(after_claim, dispatcher.client.endpoint)
    abandoned_claim = "a" * 32
    claimed, acquired = store.begin_dispatch(
        after_claim["job_id"], abandoned_claim, lease_seconds=1
    )
    assert acquired is True
    assert claimed["dispatch_claim_id"] == abandoned_claim

    still_owned = dispatcher.submit(after_claim)
    assert still_owned["authoritative"]["status"] == "ACCEPTED"
    assert still_owned["authoritative"]["dispatch_claim_id"] == abandoned_claim
    assert still_owned["authoritative"]["acceptance_ambiguous"] is False

    time.sleep(1.1)
    recovered_after = dispatcher.submit(after_claim)
    assert recovered_after["authoritative"]["status"] == "FAILED"
    assert recovered_after["authoritative"]["failure"]["code"] == "FENG_UNAVAILABLE"
    assert recovered_after["authoritative"]["dispatch_claim_id"] is None
    assert recovered_after["authoritative"]["acceptance_ambiguous"] is False

    for request in (fresh_request, before_claim, after_claim):
        shape = store.storage_shape(request["job_id"])
        assert shape["study_rows"] == 1
        assert shape["attempt_rows"] == 0
        assert shape["experiment_rows"] == 0
        assert shape["per_iteration_event_rows"] == 0
        assert shape["tables"] == ["jobs", "schema_identity"]
    assert fresh["local_compute_attempted"] is False
    print(
        canonical_json_bytes(
            {
                "scenario": "definite_refusal_and_abandoned_claim_recovery",
                "fresh_refusal": fresh["authoritative"],
                "terminal_duplicate": duplicate["authoritative"],
                "interrupted_before_claim": recovered_before["authoritative"],
                "interrupted_after_claim_before_expiry": still_owned["authoritative"],
                "interrupted_after_claim_recovered": recovered_after["authoritative"],
                "storage_shapes": [
                    store.storage_shape(request["job_id"])
                    for request in (fresh_request, before_claim, after_claim)
                ],
                "local_compute_attempted": fresh["local_compute_attempted"],
            }
        ).decode("utf-8")
    )


def test_v2_schema_migration_replay_and_foreign_identity_rejection():
    store = StudyPostgresStore.from_environment()
    store.initialize()
    with store.config.connect() as connection:
        connection.execute(
            "ALTER TABLE qr_study.jobs DROP CONSTRAINT valid_study_dispatch_post_started"
        )
        connection.execute("ALTER TABLE qr_study.jobs DROP COLUMN dispatch_post_started")
        connection.execute(
            "UPDATE qr_study.schema_identity SET identity=%s WHERE singleton",
            ("quantresearch-lightweight-study-postgresql-v2",),
        )

    store.initialize()
    store.initialize()
    with store.config.connect() as connection:
        identity = connection.execute(
            "SELECT identity FROM qr_study.schema_identity WHERE singleton"
        ).fetchone()["identity"]
        column = connection.execute(
            """
            SELECT is_nullable, column_default FROM information_schema.columns
            WHERE table_schema='qr_study' AND table_name='jobs'
              AND column_name='dispatch_post_started'
            """
        ).fetchone()
        connection.execute(
            "UPDATE qr_study.schema_identity SET identity='foreign-study-schema' WHERE singleton"
        )
    try:
        with pytest.raises(StudyRemoteError, match="schema identity conflicts"):
            store.initialize()
    finally:
        with store.config.connect() as connection:
            connection.execute(
                "UPDATE qr_study.schema_identity SET identity=%s WHERE singleton",
                ("quantresearch-lightweight-study-postgresql-v2",),
            )
        store.initialize()

    assert identity == SCHEMA_IDENTITY
    assert column == {"is_nullable": "NO", "column_default": "false"}
    print(
        canonical_json_bytes(
            {
                "scenario": "v2_schema_migration_replay_and_identity_mismatch",
                "migrated_identity": identity,
                "dispatch_post_started_column": column,
                "foreign_identity_rejected": True,
            }
        ).decode("utf-8")
    )