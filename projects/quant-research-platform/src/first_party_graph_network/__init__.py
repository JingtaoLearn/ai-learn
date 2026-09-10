"""One-shot HTTPS adapter for the reviewed first-party navigation graph."""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import ssl
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from first_party_graph_offline import (
    ALLOWED_HOST,
    ALLOWED_PREFIX,
    PROTOCOL_ID,
    REDIRECT_STATUSES,
    ROOT_PATH,
    AdapterOutput,
    AtomicClaim,
    BodyDelivery,
    FirstPartyGraphController,
    TransportResponse,
)

PROTOCOL_SHA256 = "b11fd8a4936c2c0b0c760629210dd46c7c6e8da50d7c13ffa97783b190ed0248"
OFFLINE_CONTROLLER_SHA256 = "5a99f83065806e4697776e424dc21cf8ad752a78013b89592f4796022536d7ab"
AUTHORITY_SCHEMA = "quantresearch-first-party-graph-execution-authority/v1"
RECEIPT_SCHEMA = "quantresearch-first-party-graph-execution-receipt/v1"
CANDIDATE_DOMAIN = b"quantresearch-first-party-graph-network-candidate/v1\0"
CLAIM_KEY = PROTOCOL_ID
ALLOWED_PORT = 443
CONNECT_TIMEOUT_SECONDS = 10
TOTAL_TIMEOUT_SECONDS = 30
MAX_AUTHORITY_BYTES = 65_536
READ_CHUNK_BYTES = 65_536
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")

_AUTHORITY_FIELDS = {
    "schema",
    "decision",
    "protocol_sha256",
    "offline_controller_sha256",
    "network_adapter_sha256",
    "launcher_sha256",
    "candidate_sha256",
    "execution_handoff_sha256",
    "execution_authority_context_sha256",
    "claim_key",
    "state_root",
    "network_capability",
    "next_stage_authorization",
}
_NETWORK_FIELDS = {
    "scheme",
    "host",
    "port",
    "start_path",
    "exact_paths",
    "path_prefixes",
    "method",
    "connect_timeout_seconds",
    "total_timeout_seconds_per_handle",
    "redirect_mode",
    "retry_count",
    "authentication",
    "cookies",
    "environment_proxies",
    "fallback",
}


class ExecutionRefused(RuntimeError):
    """Fail-closed refusal carrying only a stable code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class CandidateIdentity:
    offline_controller_sha256: str
    network_adapter_sha256: str
    launcher_sha256: str
    candidate_sha256: str


@dataclass(frozen=True)
class ExecutionOutcome:
    graph: AdapterOutput
    receipt: dict[str, object] | None


class _CountedTransport(Protocol):
    request_count: int

    def __call__(self, path: str) -> TransportResponse: ...


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def candidate_identity() -> CandidateIdentity:
    adapter_path = Path(__file__)
    offline_path = adapter_path.parent.parent / "first_party_graph_offline" / "__init__.py"
    launcher_path = (
        adapter_path.parent.parent.parent / "scripts" / "run_first_party_graph_network.py"
    )
    offline_sha256 = _digest(_read_regular(offline_path, 1_000_000))
    adapter_sha256 = _digest(_read_regular(adapter_path, 1_000_000))
    launcher_sha256 = _digest(_read_regular(launcher_path, 1_000_000))
    aggregate = _digest(
        CANDIDATE_DOMAIN
        + offline_sha256.encode("ascii")
        + b"\0"
        + adapter_sha256.encode("ascii")
        + b"\0"
        + launcher_sha256.encode("ascii")
    )
    return CandidateIdentity(offline_sha256, adapter_sha256, launcher_sha256, aggregate)


def _read_regular(path: Path, limit: int) -> bytes:
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > limit:
            raise ExecutionRefused("IDENTITY_FILE_INVALID")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise ExecutionRefused("IDENTITY_FILE_CHANGED")
            chunks: list[bytes] = []
            remaining = limit + 1
            while remaining:
                chunk = os.read(descriptor, min(READ_CHUNK_BYTES, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            payload = b"".join(chunks)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
    except ExecutionRefused:
        raise
    except OSError:
        raise ExecutionRefused("IDENTITY_FILE_INVALID") from None
    if len(payload) > limit:
        raise ExecutionRefused("IDENTITY_FILE_INVALID")
    if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise ExecutionRefused("IDENTITY_FILE_CHANGED")
    return payload


def _strict_object(payload: bytes) -> dict[str, object]:
    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ExecutionRefused("AUTHORITY_INVALID")
            result[key] = value
        return result

    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=unique,
            parse_constant=lambda _: (_ for _ in ()).throw(ExecutionRefused("AUTHORITY_INVALID")),
        )
    except ExecutionRefused:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ExecutionRefused("AUTHORITY_INVALID") from None
    if not isinstance(value, dict):
        raise ExecutionRefused("AUTHORITY_INVALID")
    return value


def _require_sha256(value: object) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ExecutionRefused("IDENTITY_INVALID")
    return value


def _validate_authority(
    authority_path: Path,
    protocol_path: Path,
    state_root: Path,
    *,
    expected_protocol_sha256: str,
    expected_candidate_sha256: str,
    expected_authority_sha256: str,
) -> tuple[dict[str, object], CandidateIdentity]:
    if _require_sha256(expected_protocol_sha256) != PROTOCOL_SHA256:
        raise ExecutionRefused("PROTOCOL_IDENTITY_MISMATCH")
    if _digest(_read_regular(protocol_path, 1_000_000)) != expected_protocol_sha256:
        raise ExecutionRefused("PROTOCOL_IDENTITY_MISMATCH")

    identity = candidate_identity()
    if identity.offline_controller_sha256 != OFFLINE_CONTROLLER_SHA256:
        raise ExecutionRefused("OFFLINE_CONTROLLER_IDENTITY_MISMATCH")
    if _require_sha256(expected_candidate_sha256) != identity.candidate_sha256:
        raise ExecutionRefused("CANDIDATE_IDENTITY_MISMATCH")

    authority_bytes = _read_regular(authority_path, MAX_AUTHORITY_BYTES)
    if _digest(authority_bytes) != _require_sha256(expected_authority_sha256):
        raise ExecutionRefused("AUTHORITY_IDENTITY_MISMATCH")
    authority = _strict_object(authority_bytes)
    if set(authority) != _AUTHORITY_FIELDS:
        raise ExecutionRefused("AUTHORITY_INVALID")
    network = authority.get("network_capability")
    if not isinstance(network, dict) or set(network) != _NETWORK_FIELDS:
        raise ExecutionRefused("AUTHORITY_INVALID")
    for field in ("execution_handoff_sha256", "execution_authority_context_sha256"):
        _require_sha256(authority.get(field))
    if (
        authority.get("schema") != AUTHORITY_SCHEMA
        or authority.get("decision") != "AUTHORIZE_ONE_SHOT_NETWORK_EXECUTION"
        or authority.get("protocol_sha256") != PROTOCOL_SHA256
        or authority.get("offline_controller_sha256") != identity.offline_controller_sha256
        or authority.get("network_adapter_sha256") != identity.network_adapter_sha256
        or authority.get("launcher_sha256") != identity.launcher_sha256
        or authority.get("candidate_sha256") != identity.candidate_sha256
        or authority.get("claim_key") != CLAIM_KEY
        or authority.get("state_root") != str(state_root.resolve(strict=False))
        or authority.get("next_stage_authorization") != "NOT_AUTHORIZED"
        or network
        != {
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
        }
    ):
        raise ExecutionRefused("AUTHORITY_INVALID")
    return authority, identity


def _path_is_in_git(path: Path) -> bool:
    return any((ancestor / ".git").exists() for ancestor in (path, *path.parents))


def _path_has_symlink_component(path: Path) -> bool:
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            if stat.S_ISLNK(current.lstat().st_mode):
                return True
        except FileNotFoundError:
            continue
    return False


def _private_directory(path: Path) -> Path:
    if not path.is_absolute() or _path_has_symlink_component(path):
        raise ExecutionRefused("STATE_ROOT_INVALID")
    try:
        resolved = path.resolve(strict=False)
        if _path_is_in_git(resolved):
            raise ExecutionRefused("STATE_ROOT_IN_GIT")
        resolved.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = resolved.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise ExecutionRefused("STATE_ROOT_INVALID")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise ExecutionRefused("STATE_ROOT_PERMISSIONS")
    except ExecutionRefused:
        raise
    except OSError:
        raise ExecutionRefused("STATE_ROOT_INVALID") from None
    return resolved


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_create_only(path: Path, payload: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700, follow_symlinks=False)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
    except Exception:
        os.close(descriptor)
        try:
            path.unlink()
        except OSError:
            pass
        raise
    else:
        os.close(descriptor)
    _fsync_directory(path.parent)


class DurableClaim(AtomicClaim):
    """A create-only filesystem claim consumed before the controller calls transport."""

    def __init__(self, state_root: Path, claim_payload: dict[str, object]) -> None:
        self._path = state_root / "claim.json"
        self._payload = _canonical_json(claim_payload) + b"\n"

    def consume(self) -> bool:
        try:
            _write_create_only(self._path, self._payload, 0o400)
        except FileExistsError:
            return False
        except OSError:
            raise ExecutionRefused("CLAIM_PERSISTENCE_FAILURE") from None
        return True


class RestrictedBodyReceiptStore:
    """Atomic create-only body receipts readable only by the protocol parser path."""

    def __init__(self, state_root: Path) -> None:
        self._root = state_root / "body-receipts"
        self._root.mkdir(mode=0o700, exist_ok=True)
        os.chmod(self._root, 0o700, follow_symlinks=False)
        self._next_id = 1

    def commit(self, body: bytes) -> str:
        receipt_id = f"r{self._next_id:06d}"
        self._next_id += 1
        target = self._root / f"{receipt_id}.bin"
        temporary = self._root / f".{receipt_id}.{os.getpid()}.tmp"
        try:
            _write_create_only(temporary, bytes(body), 0o400)
            os.link(temporary, target, follow_symlinks=False)
            temporary.unlink()
            _fsync_directory(self._root)
        except Exception:
            try:
                temporary.unlink()
            except OSError:
                pass
            raise ExecutionRefused("BODY_RECEIPT_PERSISTENCE_FAILURE") from None
        return receipt_id

    def read_for_parser(self, receipt_id: str) -> bytes:
        if re.fullmatch(r"r[0-9]{6}", receipt_id) is None:
            raise ExecutionRefused("BODY_RECEIPT_INVALID")
        return _read_regular(self._root / f"{receipt_id}.bin", 2_097_152)


class LockedHTTPSTransport:
    """Exact-host, direct, no-retry stdlib HTTPS transport."""

    def __init__(
        self,
        receipt_store: RestrictedBodyReceiptStore,
        *,
        _connection_factory: Callable[..., Any] = http.client.HTTPSConnection,
        _clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._receipt_store = receipt_store
        self._connection_factory = _connection_factory
        self._clock = _clock
        self.request_count = 0
        context = ssl.create_default_context()
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        self._tls_context = context

    def __call__(self, path: str) -> TransportResponse:
        if not isinstance(path, str) or not _allowed_request_path(path):
            raise ExecutionRefused("REQUEST_TARGET_REJECTED")
        deadline = self._clock() + TOTAL_TIMEOUT_SECONDS
        connection: Any = None
        response: Any = None
        self.request_count += 1
        try:
            connection = self._connection_factory(
                ALLOWED_HOST,
                ALLOWED_PORT,
                timeout=CONNECT_TIMEOUT_SECONDS,
                context=self._tls_context,
            )
            connection.connect()
            self._set_timeout(connection, deadline)
            connection.putrequest("GET", path, skip_accept_encoding=True)
            connection.putheader("Accept", "text/html")
            connection.putheader("User-Agent", "QuantResearch-FirstPartyGraph/1.0")
            connection.endheaders()
            self._set_timeout(connection, deadline)
            response = connection.getresponse()
            status = response.status
            locations = tuple(response.headers.get_all("Location", failobj=[]))
            content_types = response.headers.get_all("Content-Type", failobj=[])
            content_type = content_types[0] if len(content_types) == 1 else None
            if status in REDIRECT_STATUSES or not 200 <= status <= 299 or not _html(content_type):
                response.close()
                connection.close()
                return TransportResponse(status, content_type, locations, _closed_receive)

            consumed = False

            def receive(allowance: int) -> BodyDelivery:
                nonlocal consumed
                if consumed or type(allowance) is not int or allowance <= 0:
                    raise ExecutionRefused("BODY_RECEIVE_INVALID")
                consumed = True
                chunks: list[bytes] = []
                remaining = allowance
                try:
                    while remaining:
                        self._set_timeout(connection, deadline)
                        chunk = response.read(min(READ_CHUNK_BYTES, remaining))
                        if not chunk:
                            break
                        chunks.append(chunk)
                        remaining -= len(chunk)
                    body = b"".join(chunks)
                    end_of_stream = response.isclosed() or response.length == 0
                    receipt_id = self._receipt_store.commit(body)
                    committed = self._receipt_store.read_for_parser(receipt_id)
                    return BodyDelivery(committed, end_of_stream)
                except ExecutionRefused:
                    raise
                except (OSError, TimeoutError, ssl.SSLError, http.client.HTTPException):
                    raise ExecutionRefused("FETCH_TRANSPORT_FAILURE") from None
                finally:
                    response.close()
                    connection.close()

            return TransportResponse(status, content_type, locations, receive)
        except ExecutionRefused:
            if response is not None:
                response.close()
            if connection is not None:
                connection.close()
            raise
        except (OSError, TimeoutError, ssl.SSLError, http.client.HTTPException):
            if response is not None:
                response.close()
            if connection is not None:
                connection.close()
            raise ExecutionRefused("FETCH_TRANSPORT_FAILURE") from None

    def _set_timeout(self, connection: Any, deadline: float) -> None:
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise ExecutionRefused("FETCH_TRANSPORT_FAILURE")
        if connection.sock is None:
            raise ExecutionRefused("FETCH_TRANSPORT_FAILURE")
        connection.sock.settimeout(remaining)


def _allowed_request_path(path: str) -> bool:
    try:
        path.encode("ascii")
    except UnicodeEncodeError:
        return False
    return (
        (path == ROOT_PATH or path.startswith(ALLOWED_PREFIX))
        and not any(character in path for character in ("?", "#", "%", "\\"))
        and all(0x21 <= ord(character) < 0x7F for character in path)
        and path.startswith("/")
        and "/../" not in f"{path}/"
        and "/./" not in f"{path}/"
    )


def _html(content_type: str | None) -> bool:
    if not isinstance(content_type, str):
        return False
    try:
        content_type.encode("ascii")
    except UnicodeEncodeError:
        return False
    return content_type.split(";", 1)[0].strip().lower() == "text/html"


def _closed_receive(allowance: int) -> BodyDelivery:
    del allowance
    raise ExecutionRefused("BODY_RECEIVE_PROHIBITED")


def _persist_execution_receipt(
    state_root: Path,
    *,
    authority: dict[str, object],
    authority_sha256: str,
    identity: CandidateIdentity,
    request_count: int,
    graph: AdapterOutput,
) -> dict[str, object]:
    output_sha256 = (
        _digest(_canonical_json(graph.public_result)) if graph.public_result is not None else None
    )
    receipt: dict[str, object] = {
        "schema": RECEIPT_SCHEMA,
        "protocol_id": PROTOCOL_ID,
        "protocol_sha256": PROTOCOL_SHA256,
        "offline_controller_sha256": identity.offline_controller_sha256,
        "network_adapter_sha256": identity.network_adapter_sha256,
        "launcher_sha256": identity.launcher_sha256,
        "candidate_sha256": identity.candidate_sha256,
        "authority_sha256": authority_sha256,
        "execution_handoff_sha256": authority["execution_handoff_sha256"],
        "execution_authority_context_sha256": authority["execution_authority_context_sha256"],
        "claim_key_sha256": _digest(CLAIM_KEY.encode("ascii")),
        "request_count": request_count,
        "terminal_state": graph.terminal_code,
        "output_sha256": output_sha256,
        "next_stage_authorization": "NOT_AUTHORIZED",
    }
    try:
        _write_create_only(
            state_root / "execution-receipt.json",
            _canonical_json(receipt) + b"\n",
            0o400,
        )
    except OSError:
        raise ExecutionRefused("EXECUTION_RECEIPT_PERSISTENCE_FAILURE") from None
    return receipt


def execute_once(
    *,
    authority_path: Path,
    protocol_path: Path,
    state_root: Path,
    expected_protocol_sha256: str,
    expected_candidate_sha256: str,
    expected_authority_sha256: str,
) -> ExecutionOutcome:
    return _execute_once(
        authority_path=authority_path,
        protocol_path=protocol_path,
        state_root=state_root,
        expected_protocol_sha256=expected_protocol_sha256,
        expected_candidate_sha256=expected_candidate_sha256,
        expected_authority_sha256=expected_authority_sha256,
        transport_factory=LockedHTTPSTransport,
    )


def _execute_once(
    *,
    authority_path: Path,
    protocol_path: Path,
    state_root: Path,
    expected_protocol_sha256: str,
    expected_candidate_sha256: str,
    expected_authority_sha256: str,
    transport_factory: Callable[[RestrictedBodyReceiptStore], _CountedTransport],
) -> ExecutionOutcome:
    authority, identity = _validate_authority(
        authority_path,
        protocol_path,
        state_root,
        expected_protocol_sha256=expected_protocol_sha256,
        expected_candidate_sha256=expected_candidate_sha256,
        expected_authority_sha256=expected_authority_sha256,
    )
    root = _private_directory(state_root)
    claim_path = root / "claim.json"
    body_root = root / "body-receipts"
    if not claim_path.exists() and (
        (root / "execution-receipt.json").exists()
        or (body_root.exists() and any(body_root.iterdir()))
    ):
        raise ExecutionRefused("STATE_CONFLICT")
    claim = DurableClaim(
        root,
        {
            "schema": "quantresearch-first-party-graph-durable-claim/v1",
            "claim_key": CLAIM_KEY,
            "protocol_sha256": PROTOCOL_SHA256,
            "candidate_sha256": identity.candidate_sha256,
            "authority_sha256": expected_authority_sha256,
            "state": "CLAIMED",
        },
    )
    body_receipts = RestrictedBodyReceiptStore(root)
    transport = transport_factory(body_receipts)
    controller = FirstPartyGraphController(transport, claim=claim)
    graph = controller.run()
    if graph.terminal_code == "CLAIM_ALREADY_CONSUMED":
        return ExecutionOutcome(graph, None)
    receipt = _persist_execution_receipt(
        root,
        authority=authority,
        authority_sha256=expected_authority_sha256,
        identity=identity,
        request_count=transport.request_count,
        graph=graph,
    )
    return ExecutionOutcome(graph, receipt)


__all__ = [
    "AUTHORITY_SCHEMA",
    "CandidateIdentity",
    "ExecutionOutcome",
    "ExecutionRefused",
    "LockedHTTPSTransport",
    "PROTOCOL_SHA256",
    "RestrictedBodyReceiptStore",
    "candidate_identity",
    "execute_once",
]
