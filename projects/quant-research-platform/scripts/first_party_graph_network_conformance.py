from __future__ import annotations

import hashlib
import json
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from first_party_graph_network import (
    ALLOWED_HOST,
    ALLOWED_PORT,
    ALLOWED_PREFIX,
    AUTHORITY_SCHEMA,
    CandidateIdentity,
    CONNECT_TIMEOUT_SECONDS,
    PROTOCOL_SHA256,
    ROOT_PATH,
    TOTAL_TIMEOUT_SECONDS,
    ExecutionRefused,
    ExecutionOutcome,
    LockedHTTPSTransport,
    RestrictedBodyReceiptStore,
    _execute_once,
    _private_directory,
    candidate_identity,
)
from first_party_graph_offline import BodyDelivery, FirstPartyGraphController, TransportResponse

CANARY_IDS = (
    "EXACT_IDENTITY_GATE",
    "DURABLE_CLAIM_BEFORE_TRANSPORT",
    "CLAIM_REUSE_REFUSED",
    "RESTRICTED_ATOMIC_BODY_RECEIPT",
    "EXACT_HTTPS_TRANSPORT",
    "TARGET_AND_RESPONSE_GATES",
    "CLOSED_EXECUTION_RECEIPT",
    "STATE_ROOT_OUTSIDE_GIT",
)


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "ascii"
    )


def _authority(state_root: Path) -> tuple[dict[str, object], CandidateIdentity]:
    identity = candidate_identity()
    value: dict[str, object] = {
        "schema": AUTHORITY_SCHEMA,
        "decision": "AUTHORIZE_ONE_SHOT_NETWORK_EXECUTION",
        "protocol_sha256": PROTOCOL_SHA256,
        "offline_controller_sha256": identity.offline_controller_sha256,
        "network_adapter_sha256": identity.network_adapter_sha256,
        "launcher_sha256": identity.launcher_sha256,
        "candidate_sha256": identity.candidate_sha256,
        "execution_handoff_sha256": "a" * 64,
        "execution_authority_context_sha256": "b" * 64,
        "claim_key": "gold-first-party-graph-fed-board-monetary-policy-v2",
        "state_root": str(state_root.resolve(strict=False)),
        "network_capability": {
            "scheme": "https",
            "host": ALLOWED_HOST,
            "port": ALLOWED_PORT,
            "start_path": ROOT_PATH,
            "exact_paths": [ROOT_PATH],
            "path_prefixes": [ALLOWED_PREFIX],
            "method": "GET",
            "connect_timeout_seconds": CONNECT_TIMEOUT_SECONDS,
            "total_timeout_seconds_per_handle": TOTAL_TIMEOUT_SECONDS,
            "redirect_mode": "MANUAL_ONE_HOP_EXACT_NORMALIZATION_ONLY",
            "retry_count": 0,
            "authentication": "NONE",
            "cookies": "DISABLED",
            "environment_proxies": "DISABLED",
            "fallback": "PROHIBITED",
        },
        "next_stage_authorization": "NOT_AUTHORIZED",
    }
    return value, identity


def _write_authority(root: Path) -> tuple[Path, str, CandidateIdentity]:
    authority, identity = _authority(root / "state")
    payload = _canonical(authority) + b"\n"
    path = root / "authority.json"
    path.write_bytes(payload)
    path.chmod(0o400)
    return path, hashlib.sha256(payload).hexdigest(), identity


class _SyntheticControllerTransport:
    instances: list[_SyntheticControllerTransport] = []

    def __init__(self, store: RestrictedBodyReceiptStore, state_root: Path) -> None:
        self.store = store
        self.state_root = state_root
        self.request_count = 0
        self.calls: list[str] = []
        self.__class__.instances.append(self)

    def __call__(self, path: str) -> TransportResponse:
        assert (self.state_root / "claim.json").is_file()
        self.request_count += 1
        self.calls.append(path)
        body = (
            b'<html>NETWORK_ADAPTER_PRIVATE_CANARY<a href="/monetarypolicy/safe.htm">'
            b"PRIVATE_LABEL_CANARY</a></html>"
            if path == ROOT_PATH
            else b""
        )

        def receive(allowance: int) -> BodyDelivery:
            delivered = body[:allowance]
            receipt_id = self.store.commit(delivered)
            return BodyDelivery(self.store.read_for_parser(receipt_id), len(body) <= allowance)

        return TransportResponse(200, "text/html", (), receive)


class _Headers:
    def __init__(self, values: dict[str, list[str]]) -> None:
        self.values = values

    def get_all(self, name: str, failobj: list[str]) -> list[str]:
        return self.values.get(name, failobj)


class _Socket:
    def __init__(self) -> None:
        self.timeouts: list[float] = []

    def settimeout(self, value: float) -> None:
        assert 0 < value <= TOTAL_TIMEOUT_SECONDS
        self.timeouts.append(value)


class _HTTPResponse:
    def __init__(
        self,
        body: bytes,
        *,
        status: int = 200,
        headers: dict[str, list[str]] | None = None,
    ) -> None:
        self.body = body
        self.status = status
        self.headers = _Headers(headers or {"Content-Type": ["text/html"]})
        self.length: int | None = len(body)
        self.closed = False

    def read(self, amount: int) -> bytes:
        chunk, self.body = self.body[:amount], self.body[amount:]
        self.length = len(self.body)
        if not self.body:
            self.closed = True
        return chunk

    def isclosed(self) -> bool:
        return self.closed

    def close(self) -> None:
        self.closed = True


class _HTTPSConnection:
    def __init__(
        self, response: _HTTPResponse, capture: dict[str, object], *args: object, **kwargs: object
    ) -> None:
        capture["constructor_args"] = args
        capture["constructor_kwargs"] = kwargs
        self.response = response
        self.capture = capture
        self.sock: _Socket | None = None

    def connect(self) -> None:
        self.capture["connected"] = True
        self.sock = _Socket()

    def putrequest(self, method: str, path: str, *, skip_accept_encoding: bool) -> None:
        self.capture["request"] = (method, path, skip_accept_encoding)

    def putheader(self, name: str, value: str) -> None:
        headers = self.capture.setdefault("headers", [])
        assert isinstance(headers, list)
        headers.append((name, value))

    def endheaders(self) -> None:
        self.capture["ended"] = True

    def getresponse(self) -> _HTTPResponse:
        return self.response

    def close(self) -> None:
        self.capture["closed"] = True


@dataclass(frozen=True)
class _RunInputs:
    authority_path: Path
    authority_sha256: str
    state_root: Path


def _run_once(
    protocol_path: Path, root: Path
) -> tuple[ExecutionOutcome, _RunInputs, CandidateIdentity]:
    authority_path, authority_sha256, identity = _write_authority(root)
    state_root = root / "state"

    def factory(store: RestrictedBodyReceiptStore) -> _SyntheticControllerTransport:
        return _SyntheticControllerTransport(store, state_root)

    outcome = _execute_once(
        authority_path=authority_path,
        protocol_path=protocol_path,
        state_root=state_root,
        expected_protocol_sha256=PROTOCOL_SHA256,
        expected_candidate_sha256=identity.candidate_sha256,
        expected_authority_sha256=authority_sha256,
        transport_factory=factory,
    )
    return outcome, _RunInputs(authority_path, authority_sha256, state_root), identity


def _check_identity_gate(protocol_path: Path) -> object:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        authority_path, authority_sha256, identity = _write_authority(root)
        called = [0]

        def forbidden_factory(_: RestrictedBodyReceiptStore) -> _SyntheticControllerTransport:
            called[0] += 1
            raise AssertionError("factory reached")

        try:
            _execute_once(
                authority_path=authority_path,
                protocol_path=protocol_path,
                state_root=root / "state",
                expected_protocol_sha256=PROTOCOL_SHA256,
                expected_candidate_sha256="0" * 64,
                expected_authority_sha256=authority_sha256,
                transport_factory=forbidden_factory,
            )
        except ExecutionRefused as exc:
            assert exc.code == "CANDIDATE_IDENTITY_MISMATCH"
        else:
            raise AssertionError("wrong candidate identity accepted")
        assert called == [0]
        assert not (root / "state" / "claim.json").exists()
        return {
            "code": "CANDIDATE_IDENTITY_MISMATCH",
            "candidate_sha256": identity.candidate_sha256,
        }


def _check_one_shot(protocol_path: Path) -> tuple[object, object, object]:
    _SyntheticControllerTransport.instances.clear()
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        outcome, inputs, identity = _run_once(protocol_path, root)
        state_root = inputs.state_root
        result = outcome.graph.public_result
        assert result is not None
        assert outcome.graph.terminal_code == "COMPLETED"
        nodes = result["nodes"]
        assert isinstance(nodes, list)
        assert [node["path"] for node in nodes if isinstance(node, dict)] == [
            ROOT_PATH,
            "/monetarypolicy/safe.htm",
        ]
        assert _SyntheticControllerTransport.instances[0].calls == [
            ROOT_PATH,
            "/monetarypolicy/safe.htm",
        ]
        assert len(list((state_root / "body-receipts").glob("*.bin"))) == 2
        claim = state_root / "claim.json"
        receipt_path = state_root / "execution-receipt.json"
        assert stat.S_IMODE(claim.stat().st_mode) == 0o400
        assert stat.S_IMODE(receipt_path.stat().st_mode) == 0o400
        assert stat.S_IMODE(state_root.stat().st_mode) == 0o700
        receipt = json.loads(receipt_path.read_text())
        assert receipt == outcome.receipt
        assert receipt["request_count"] == 2
        assert receipt["candidate_sha256"] == identity.candidate_sha256
        assert receipt["terminal_state"] == "COMPLETED"
        assert receipt["output_sha256"] == hashlib.sha256(_canonical(result)).hexdigest()
        visible = json.dumps([result, receipt], sort_keys=True)
        assert "PRIVATE_CANARY" not in visible and "PRIVATE_LABEL" not in visible

        def factory(store: RestrictedBodyReceiptStore) -> _SyntheticControllerTransport:
            return _SyntheticControllerTransport(store, state_root)

        second = _execute_once(
            authority_path=inputs.authority_path,
            protocol_path=protocol_path,
            state_root=state_root,
            expected_protocol_sha256=PROTOCOL_SHA256,
            expected_candidate_sha256=identity.candidate_sha256,
            expected_authority_sha256=inputs.authority_sha256,
            transport_factory=factory,
        )
        assert second.graph.terminal_code == "CLAIM_ALREADY_CONSUMED"
        assert second.receipt is None
        assert _SyntheticControllerTransport.instances[1].request_count == 0
        assert json.loads(receipt_path.read_text()) == receipt
        return (
            {"claim_preceded_requests": True, "request_count": receipt["request_count"]},
            {"second_terminal": second.graph.terminal_code, "second_request_count": 0},
            {"receipt_fields": sorted(receipt), "closed_output": True},
        )


def _check_body_no_overwrite() -> object:
    with tempfile.TemporaryDirectory() as directory:
        root = _private_directory(Path(directory) / "state")
        first = RestrictedBodyReceiptStore(root)
        receipt_id = first.commit(b"first")
        path = root / "body-receipts" / f"{receipt_id}.bin"
        before = path.read_bytes()
        try:
            RestrictedBodyReceiptStore(root).commit(b"replacement")
        except ExecutionRefused as exc:
            assert exc.code == "BODY_RECEIPT_PERSISTENCE_FAILURE"
        else:
            raise AssertionError("body receipt overwrite accepted")
        assert path.read_bytes() == before
        assert stat.S_IMODE(path.stat().st_mode) == 0o400
        return {"create_only": True, "mode": "0400", "sha256": hashlib.sha256(before).hexdigest()}


def _check_https_transport() -> tuple[object, object]:
    with tempfile.TemporaryDirectory() as directory:
        store = RestrictedBodyReceiptStore(_private_directory(Path(directory) / "state"))
        capture: dict[str, object] = {}
        response = _HTTPResponse(b"abcde")

        def factory(*args: object, **kwargs: object) -> _HTTPSConnection:
            return _HTTPSConnection(response, capture, *args, **kwargs)

        transport = LockedHTTPSTransport(store, _connection_factory=factory)
        wrapped = transport(ROOT_PATH)
        delivery = wrapped.receive(4)
        assert delivery == BodyDelivery(b"abcd", False)
        assert transport.request_count == 1
        assert capture["constructor_args"] == (ALLOWED_HOST, ALLOWED_PORT)
        options = capture["constructor_kwargs"]
        assert isinstance(options, dict)
        assert options["timeout"] == CONNECT_TIMEOUT_SECONDS
        context = options["context"]
        assert context.check_hostname is True
        assert context.verify_mode.name == "CERT_REQUIRED"
        assert capture["request"] == ("GET", ROOT_PATH, True)
        assert capture["headers"] == [
            ("Accept", "text/html"),
            ("User-Agent", "QuantResearch-FirstPartyGraph/1.0"),
        ]
        assert capture["closed"] is True

        factories = [0]

        def forbidden(*args: object, **kwargs: object) -> _HTTPSConnection:
            del args, kwargs
            factories[0] += 1
            raise AssertionError("connection created")

        rejected = LockedHTTPSTransport(store, _connection_factory=forbidden)
        for path in (
            "http://www.federalreserve.gov/monetarypolicy.htm",
            "/monetarypolicy.htm?q=1",
            "/outside.htm",
            "/monetarypolicy/%61.htm",
        ):
            try:
                rejected(path)
            except ExecutionRefused as exc:
                assert exc.code == "REQUEST_TARGET_REJECTED"
            else:
                raise AssertionError("invalid target accepted")
        assert factories == [0]
        return (
            {
                "host": ALLOWED_HOST,
                "port": ALLOWED_PORT,
                "method": "GET",
                "connect_timeout_seconds": CONNECT_TIMEOUT_SECONDS,
                "total_timeout_seconds": TOTAL_TIMEOUT_SECONDS,
                "request_count": transport.request_count,
                "delivered_bytes": len(delivery.data),
                "end_of_stream": delivery.end_of_stream,
            },
            {"rejected_targets": 4, "connections": factories[0]},
        )


def _check_response_gates() -> object:
    with tempfile.TemporaryDirectory() as directory:
        store = RestrictedBodyReceiptStore(_private_directory(Path(directory) / "state"))
        records = []
        for status, headers in (
            (302, {"Location": ["/monetarypolicy/safe.htm"]}),
            (404, {"Content-Type": ["text/html"]}),
            (200, {"Content-Type": ["application/json"]}),
        ):
            capture: dict[str, object] = {}
            response = _HTTPResponse(b"REJECTED_BODY_CANARY", status=status, headers=headers)

            def factory(
                *args: object, _response: _HTTPResponse = response, **kwargs: object
            ) -> _HTTPSConnection:
                return _HTTPSConnection(_response, capture, *args, **kwargs)

            transport = LockedHTTPSTransport(store, _connection_factory=factory)
            wrapped = transport(ROOT_PATH)
            assert response.closed is True
            assert transport.request_count == 1
            assert not list((store._root).glob("*.bin"))
            records.append(
                {
                    "status": wrapped.status,
                    "locations": len(wrapped.location_values),
                    "request_count": transport.request_count,
                }
            )

        failed_calls = [0]

        def failing_factory(*args: object, **kwargs: object) -> _HTTPSConnection:
            del args, kwargs
            failed_calls[0] += 1
            raise OSError("TRANSPORT_FAILURE_CANARY")

        failing = LockedHTTPSTransport(store, _connection_factory=failing_factory)
        failed_result = FirstPartyGraphController(failing).run().public_result
        assert failed_result is not None
        assert failed_result["terminal_code"] == "TERMINAL_ROOT_FAILURE"
        assert failed_calls == [1]
        assert failing.request_count == 1
        assert "TRANSPORT_FAILURE_CANARY" not in json.dumps(failed_result, sort_keys=True)
        records.append({"transport_failure": "FETCH_TRANSPORT_FAILURE", "request_count": 1})
        assert "REJECTED_BODY_CANARY" not in json.dumps(records)
        return records


def _check_state_root() -> object:
    source_directory = Path(__file__).resolve().parent
    try:
        _private_directory(source_directory)
    except ExecutionRefused as exc:
        assert exc.code == "STATE_ROOT_IN_GIT"
    else:
        raise AssertionError("Git-contained state root accepted")
    return {"code": "STATE_ROOT_IN_GIT"}


def main() -> int:
    if len(sys.argv) != 2:
        return 2
    protocol_path = Path(sys.argv[1])
    checks: dict[str, Callable[[], object]] = {
        "EXACT_IDENTITY_GATE": lambda: _check_identity_gate(protocol_path),
        "DURABLE_CLAIM_BEFORE_TRANSPORT": lambda: _check_one_shot(protocol_path)[0],
        "CLAIM_REUSE_REFUSED": lambda: _check_one_shot(protocol_path)[1],
        "RESTRICTED_ATOMIC_BODY_RECEIPT": _check_body_no_overwrite,
        "EXACT_HTTPS_TRANSPORT": lambda: _check_https_transport()[0],
        "TARGET_AND_RESPONSE_GATES": lambda: [_check_https_transport()[1], _check_response_gates()],
        "CLOSED_EXECUTION_RECEIPT": lambda: _check_one_shot(protocol_path)[2],
        "STATE_ROOT_OUTSIDE_GIT": _check_state_root,
    }
    records = []
    failed = False
    for canary_id in CANARY_IDS:
        try:
            evidence = checks[canary_id]()
        except Exception:
            records.append({"id": canary_id, "status": "FAIL"})
            failed = True
        else:
            records.append(
                {
                    "id": canary_id,
                    "status": "PASS",
                    "evidence_sha256": hashlib.sha256(_canonical(evidence)).hexdigest(),
                }
            )
    output = {
        "schema": "quantresearch-first-party-graph-network-conformance/v1",
        "fixture_origin": "LOCAL_SYNTHETIC_ONLY",
        "network_access": "PROHIBITED",
        "canaries": records,
        "terminal_code": "PASS" if not failed else "FAIL",
        "next_stage_authorization": "NOT_AUTHORIZED",
    }
    print(_canonical(output).decode("ascii"))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
