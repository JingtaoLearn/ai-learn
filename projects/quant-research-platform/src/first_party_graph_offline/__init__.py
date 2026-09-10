"""Offline-only first-party graph protocol with mandatory injected transport."""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
from threading import Lock
from typing import Callable, Protocol
from urllib.parse import urljoin, urlsplit

PROTOCOL_ID = "gold-first-party-graph-fed-board-monetary-policy-v2"
RESULT_SCHEMA = "quantresearch-fixed-first-party-navigation-graph-result/v2"
ROOT_PATH = "/monetarypolicy.htm"
ALLOWED_PREFIX = "/monetarypolicy/"
ALLOWED_HOST = "www.federalreserve.gov"
MAX_DEPTH = 2
MAX_REQUESTED_PAGES = 32
MAX_RESPONSE_BODY_BYTES = 2_097_152
MAX_TOTAL_RECEIVED_BODY_BYTES = 16_777_216
MAX_ACCEPTED_HANDLES = 256
MAX_PAGE_HANDLES = 64

COUNT_CODES = (
    "DUPLICATE_HANDLE",
    "HANDLE_CAP_REJECTED",
    "PER_PAGE_HANDLE_CAP_REJECTED",
    "REJECT_EMPTY_HREF",
    "REJECT_NON_ASCII_HREF",
    "REJECT_BACKSLASH",
    "REJECT_PERCENT_ESCAPE",
    "REJECT_PROTOCOL_RELATIVE_REFERENCE",
    "REJECT_SCHEME",
    "REJECT_HOST",
    "REJECT_PORT",
    "REJECT_USERINFO",
    "REJECT_QUERY",
    "REJECT_FRAGMENT",
    "REJECT_PATH",
)
NODE_CODES = frozenset(
    {
        "ACCEPTED_HANDLE",
        "FETCHED_PARSED",
        "FETCHED_NOT_EXPANDED_DEPTH",
        "FETCH_TRANSPORT_FAILURE",
        "REJECT_HTTP_STATUS",
        "REJECT_CONTENT_TYPE",
        "REJECT_REDIRECT_CHAIN",
        "REJECT_REDIRECT_OFF_ALLOWLIST",
        "RECEIPT_TOO_LARGE",
        "PARSE_FAILURE",
        "PAGE_CAP_NOT_REQUESTED",
        "TOTAL_BYTE_CAP_REACHED",
    }
)
TERMINAL_CODES = frozenset(
    {
        "COMPLETED",
        "COMPLETED_WITH_BOUNDED_NODE_FAILURES",
        "TERMINAL_ROOT_FAILURE",
        "TERMINAL_CAP_REACHED",
        "CLAIM_ALREADY_CONSUMED",
        "CONTAMINATED_OR_SCOPE_VIOLATION",
    }
)
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


@dataclass(frozen=True)
class BodyDelivery:
    data: bytes
    end_of_stream: bool


class BodyReceiver(Protocol):
    def __call__(self, allowance: int) -> BodyDelivery: ...


@dataclass(frozen=True)
class TransportResponse:
    status: int
    content_type: str | None
    location_values: tuple[str, ...]
    receive: BodyReceiver
    opaque_headers: tuple[tuple[str, str], ...] = ()
    opaque_cookies: tuple[str, ...] = ()


class Transport(Protocol):
    def __call__(self, path: str) -> TransportResponse: ...


@dataclass(frozen=True)
class AdapterOutput:
    terminal_code: str
    public_result: dict[str, object] | None


@dataclass
class _Node:
    path: str
    depth: int
    parent_path: str | None
    disposition_code: str = "ACCEPTED_HANDLE"


@dataclass(frozen=True)
class _FetchResult:
    disposition_code: str
    receipt_id: str | None = None
    terminal_after_receipt: bool = False
    contaminated: bool = False


class AtomicClaim:
    """One in-memory claim with no reset, retry, resume, or replacement operation."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._claimed = False

    def consume(self) -> bool:
        with self._lock:
            if self._claimed:
                return False
            self._claimed = True
            return True


class _RestrictedReceiptStore:
    def __init__(self) -> None:
        self._receipts: dict[str, bytes] = {}
        self._next_id = 1

    def commit(self, body: bytes) -> str:
        receipt_id = f"r{self._next_id:06d}"
        self._next_id += 1
        self._receipts[receipt_id] = bytes(body)
        return receipt_id

    def read_for_parser(self, receipt_id: str) -> bytes:
        return self._receipts[receipt_id]


class _HrefParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        for name, value in attrs:
            if name.lower() == "href":
                self.hrefs.append("" if value is None else value)
                return


def _parse_committed_html(body: bytes) -> list[str]:
    parser = _HrefParser()
    parser.feed(body.decode("utf-8", errors="replace"))
    parser.close()
    return parser.hrefs


def _allowed_path(path: str) -> bool:
    return path == ROOT_PATH or path.startswith(ALLOWED_PREFIX)


def _normalize_reference(raw: str, current_path: str) -> tuple[str | None, str | None]:
    if raw == "":
        return None, "REJECT_EMPTY_HREF"
    try:
        raw.encode("ascii")
    except UnicodeEncodeError:
        return None, "REJECT_NON_ASCII_HREF"
    if "\\" in raw:
        return None, "REJECT_BACKSLASH"
    if "%" in raw:
        return None, "REJECT_PERCENT_ESCAPE"
    if raw.startswith("//"):
        return None, "REJECT_PROTOCOL_RELATIVE_REFERENCE"

    try:
        parsed = urlsplit(raw)
    except ValueError:
        return None, "REJECT_PATH"

    if parsed.scheme:
        if parsed.scheme.lower() != "https":
            return None, "REJECT_SCHEME"
        if parsed.username is not None or parsed.password is not None:
            return None, "REJECT_USERINFO"
        if (parsed.hostname or "").lower() != ALLOWED_HOST:
            return None, "REJECT_HOST"
        try:
            port = parsed.port
        except ValueError:
            return None, "REJECT_PORT"
        if port not in (None, 443):
            return None, "REJECT_PORT"
    elif parsed.netloc:
        return None, "REJECT_PROTOCOL_RELATIVE_REFERENCE"

    if "?" in raw:
        return None, "REJECT_QUERY"
    if "#" in raw:
        return None, "REJECT_FRAGMENT"

    try:
        resolved = urlsplit(urljoin(f"https://{ALLOWED_HOST}{current_path}", raw))
        port = resolved.port
    except ValueError:
        return None, "REJECT_PORT"
    if resolved.scheme.lower() != "https":
        return None, "REJECT_SCHEME"
    if resolved.username is not None or resolved.password is not None:
        return None, "REJECT_USERINFO"
    if (resolved.hostname or "").lower() != ALLOWED_HOST:
        return None, "REJECT_HOST"
    if port not in (None, 443):
        return None, "REJECT_PORT"
    if not _allowed_path(resolved.path):
        return None, "REJECT_PATH"
    return resolved.path, None


def _normalize_redirect(values: tuple[str, ...], current_path: str) -> str | None:
    if len(values) != 1 or not isinstance(values[0], str) or values[0] == "":
        return None
    path, disposition = _normalize_reference(values[0], current_path)
    if disposition is not None:
        return None
    return path


def _content_type_is_html(value: str | None) -> bool:
    if not isinstance(value, str):
        return False
    try:
        value.encode("ascii")
    except UnicodeEncodeError:
        return False
    return value.split(";", 1)[0].strip().lower() == "text/html"


def _empty_counts() -> dict[str, int]:
    return {code: 0 for code in COUNT_CODES}


def _closed_result(
    terminal_code: str,
    nodes: list[dict[str, object]],
    counts: dict[str, int],
) -> dict[str, object]:
    return {
        "schema": RESULT_SCHEMA,
        "protocol_id": PROTOCOL_ID,
        "root_path": ROOT_PATH,
        "terminal_code": terminal_code,
        "nodes": nodes,
        "disposition_counts": counts,
        "next_stage_authorization": "NOT_AUTHORIZED",
    }


def project_public_result(candidate: object) -> AdapterOutput:
    """Fail closed if any field, type, path, or disposition is outside protocol v2."""
    if not isinstance(candidate, dict):
        return AdapterOutput("CONTAMINATED_OR_SCOPE_VIOLATION", None)
    if set(candidate) != {
        "schema",
        "protocol_id",
        "root_path",
        "terminal_code",
        "nodes",
        "disposition_counts",
        "next_stage_authorization",
    }:
        return AdapterOutput("CONTAMINATED_OR_SCOPE_VIOLATION", None)
    if (
        candidate.get("schema") != RESULT_SCHEMA
        or candidate.get("protocol_id") != PROTOCOL_ID
        or candidate.get("root_path") != ROOT_PATH
        or candidate.get("terminal_code") not in TERMINAL_CODES
        or candidate.get("next_stage_authorization") != "NOT_AUTHORIZED"
    ):
        return AdapterOutput("CONTAMINATED_OR_SCOPE_VIOLATION", None)

    nodes = candidate.get("nodes")
    counts = candidate.get("disposition_counts")
    if not isinstance(nodes, list) or len(nodes) > MAX_ACCEPTED_HANDLES:
        return AdapterOutput("CONTAMINATED_OR_SCOPE_VIOLATION", None)
    if not isinstance(counts, dict) or set(counts) != set(COUNT_CODES):
        return AdapterOutput("CONTAMINATED_OR_SCOPE_VIOLATION", None)
    if any(type(counts[code]) is not int or counts[code] < 0 for code in COUNT_CODES):
        return AdapterOutput("CONTAMINATED_OR_SCOPE_VIOLATION", None)

    clean_nodes: list[dict[str, object]] = []
    for node in nodes:
        if not isinstance(node, dict) or set(node) != {
            "path",
            "depth",
            "parent_path",
            "disposition_code",
        }:
            return AdapterOutput("CONTAMINATED_OR_SCOPE_VIOLATION", None)
        path = node.get("path")
        depth = node.get("depth")
        parent = node.get("parent_path")
        disposition = node.get("disposition_code")
        if not isinstance(path, str) or not _allowed_path(path):
            return AdapterOutput("CONTAMINATED_OR_SCOPE_VIOLATION", None)
        if type(depth) is not int or not 0 <= depth <= MAX_DEPTH:
            return AdapterOutput("CONTAMINATED_OR_SCOPE_VIOLATION", None)
        if parent is not None and (not isinstance(parent, str) or not _allowed_path(parent)):
            return AdapterOutput("CONTAMINATED_OR_SCOPE_VIOLATION", None)
        if disposition not in NODE_CODES:
            return AdapterOutput("CONTAMINATED_OR_SCOPE_VIOLATION", None)
        clean_nodes.append(
            {
                "path": path,
                "depth": depth,
                "parent_path": parent,
                "disposition_code": disposition,
            }
        )

    clean = _closed_result(
        str(candidate["terminal_code"]),
        clean_nodes,
        {code: int(counts[code]) for code in COUNT_CODES},
    )
    return AdapterOutput(str(candidate["terminal_code"]), clean)


class FirstPartyGraphController:
    """Offline-capable protocol controller; transport is mandatory and has no default."""

    def __init__(
        self,
        transport: Transport,
        *,
        claim: AtomicClaim | None = None,
        initial_total_received_body_bytes: int = 0,
        parser: Callable[[bytes], list[str]] = _parse_committed_html,
    ) -> None:
        if not 0 <= initial_total_received_body_bytes <= MAX_TOTAL_RECEIVED_BODY_BYTES:
            raise ValueError("invalid initial byte count")
        self._transport = transport
        self._claim = claim or AtomicClaim()
        self._initial_total = initial_total_received_body_bytes
        self._parser = parser
        self.transport_attempts = 0
        self.requested_pages = 0
        self.total_received_body_bytes = initial_total_received_body_bytes
        self._receipts = _RestrictedReceiptStore()

    def run(self) -> AdapterOutput:
        if not self._claim.consume():
            return project_public_result(
                _closed_result("CLAIM_ALREADY_CONSUMED", [], _empty_counts())
            )

        counts = _empty_counts()
        nodes: dict[str, _Node] = {ROOT_PATH: _Node(ROOT_PATH, 0, None)}
        bounded_failure = False
        cap_reached = False
        terminal_code: str | None = None

        for depth in range(MAX_DEPTH + 1):
            for path in sorted(
                (node.path for node in nodes.values() if node.depth == depth),
                key=lambda value: value.encode("ascii"),
            ):
                node = nodes[path]
                if self.requested_pages >= MAX_REQUESTED_PAGES:
                    node.disposition_code = "PAGE_CAP_NOT_REQUESTED"
                    cap_reached = True
                    continue
                if self.total_received_body_bytes == MAX_TOTAL_RECEIVED_BODY_BYTES:
                    node.disposition_code = "TOTAL_BYTE_CAP_REACHED"
                    terminal_code = "TERMINAL_CAP_REACHED"
                    break

                self.requested_pages += 1
                fetched = self._fetch(path)
                if fetched.contaminated:
                    return AdapterOutput("CONTAMINATED_OR_SCOPE_VIOLATION", None)
                node.disposition_code = fetched.disposition_code

                if fetched.disposition_code == "TOTAL_BYTE_CAP_REACHED":
                    terminal_code = "TERMINAL_CAP_REACHED"
                    break
                if fetched.receipt_id is None:
                    if path == ROOT_PATH:
                        terminal_code = "TERMINAL_ROOT_FAILURE"
                        break
                    bounded_failure = True
                    continue

                if depth == MAX_DEPTH:
                    node.disposition_code = "FETCHED_NOT_EXPANDED_DEPTH"
                else:
                    try:
                        hrefs = self._parser(
                            self._receipts.read_for_parser(fetched.receipt_id)
                        )
                    except Exception:
                        node.disposition_code = "PARSE_FAILURE"
                        if fetched.terminal_after_receipt:
                            terminal_code = "TERMINAL_CAP_REACHED"
                            break
                        if path == ROOT_PATH:
                            terminal_code = "TERMINAL_ROOT_FAILURE"
                            break
                        bounded_failure = True
                        continue
                    if not isinstance(hrefs, list) or any(
                        not isinstance(href, str) for href in hrefs
                    ):
                        return AdapterOutput("CONTAMINATED_OR_SCOPE_VIOLATION", None)
                    node.disposition_code = "FETCHED_PARSED"
                    self._accept_hrefs(hrefs, node, nodes, counts)
                    if counts["HANDLE_CAP_REJECTED"]:
                        cap_reached = True

                if fetched.terminal_after_receipt:
                    terminal_code = "TERMINAL_CAP_REACHED"
                    break
            if terminal_code is not None:
                break

        if terminal_code is None:
            terminal_code = (
                "TERMINAL_CAP_REACHED"
                if cap_reached
                else (
                    "COMPLETED_WITH_BOUNDED_NODE_FAILURES"
                    if bounded_failure
                    else "COMPLETED"
                )
            )
        public_nodes = [
            {
                "path": node.path,
                "depth": node.depth,
                "parent_path": node.parent_path,
                "disposition_code": node.disposition_code,
            }
            for node in sorted(
                nodes.values(), key=lambda item: (item.depth, item.path.encode("ascii"))
            )
        ]
        return project_public_result(_closed_result(terminal_code, public_nodes, counts))

    def _fetch(self, path: str) -> _FetchResult:
        response = self._attempt_transport(path)
        if isinstance(response, _FetchResult):
            return response
        if response.status in REDIRECT_STATUSES:
            redirect_path = _normalize_redirect(response.location_values, path)
            if redirect_path is None:
                return _FetchResult("REJECT_REDIRECT_OFF_ALLOWLIST")
            response = self._attempt_transport(redirect_path)
            if isinstance(response, _FetchResult):
                return response
            if response.status in REDIRECT_STATUSES:
                return _FetchResult("REJECT_REDIRECT_CHAIN")

        if not 200 <= response.status <= 299:
            return _FetchResult("REJECT_HTTP_STATUS")
        if not _content_type_is_html(response.content_type):
            return _FetchResult("REJECT_CONTENT_TYPE")

        remaining = MAX_TOTAL_RECEIVED_BODY_BYTES - self.total_received_body_bytes
        if remaining == 0:
            return _FetchResult("TOTAL_BYTE_CAP_REACHED")
        allowance = min(MAX_RESPONSE_BODY_BYTES, remaining)
        try:
            delivery = response.receive(allowance)
        except Exception:
            return _FetchResult("FETCH_TRANSPORT_FAILURE")
        if (
            not isinstance(delivery, BodyDelivery)
            or not isinstance(delivery.data, bytes)
            or type(delivery.end_of_stream) is not bool
            or len(delivery.data) > allowance
            or (not delivery.end_of_stream and len(delivery.data) != allowance)
        ):
            return _FetchResult("CONTAMINATED_OR_SCOPE_VIOLATION", contaminated=True)

        self.total_received_body_bytes += len(delivery.data)
        receipt_id = self._receipts.commit(delivery.data)
        if not delivery.end_of_stream:
            if allowance == remaining:
                return _FetchResult("TOTAL_BYTE_CAP_REACHED")
            return _FetchResult("RECEIPT_TOO_LARGE")
        return _FetchResult(
            "FETCHED_PARSED",
            receipt_id,
            terminal_after_receipt=(
                self.total_received_body_bytes == MAX_TOTAL_RECEIVED_BODY_BYTES
            ),
        )

    def _attempt_transport(self, path: str) -> TransportResponse | _FetchResult:
        self.transport_attempts += 1
        try:
            response = self._transport(path)
        except Exception:
            return _FetchResult("FETCH_TRANSPORT_FAILURE")
        if (
            not isinstance(response, TransportResponse)
            or type(response.status) is not int
            or not isinstance(response.location_values, tuple)
            or any(not isinstance(value, str) for value in response.location_values)
        ):
            return _FetchResult("CONTAMINATED_OR_SCOPE_VIOLATION", contaminated=True)
        return response

    @staticmethod
    def _accept_hrefs(
        hrefs: list[str],
        parent: _Node,
        nodes: dict[str, _Node],
        counts: dict[str, int],
    ) -> None:
        normalized: list[str] = []
        for href in hrefs:
            path, rejection = _normalize_reference(href, parent.path)
            if rejection is not None:
                counts[rejection] += 1
            elif path is not None:
                normalized.append(path)

        page_unique: list[str] = []
        for path in sorted(normalized, key=lambda value: value.encode("ascii")):
            if page_unique and path == page_unique[-1]:
                counts["DUPLICATE_HANDLE"] += 1
            else:
                page_unique.append(path)
        retained = page_unique[:MAX_PAGE_HANDLES]
        counts["PER_PAGE_HANDLE_CAP_REJECTED"] += max(
            0, len(page_unique) - MAX_PAGE_HANDLES
        )

        for path in retained:
            if path in nodes:
                counts["DUPLICATE_HANDLE"] += 1
                continue
            if len(nodes) >= MAX_ACCEPTED_HANDLES:
                counts["HANDLE_CAP_REJECTED"] += 1
                continue
            nodes[path] = _Node(path, parent.depth + 1, parent.path)
