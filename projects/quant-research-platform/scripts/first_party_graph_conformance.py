from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any

from first_party_graph_offline import (
    MAX_ACCEPTED_HANDLES,
    MAX_RESPONSE_BODY_BYTES,
    MAX_TOTAL_RECEIVED_BODY_BYTES,
    PROTOCOL_ID,
    ROOT_PATH,
    AdapterOutput,
    AtomicClaim,
    BodyDelivery,
    FirstPartyGraphController,
    TransportResponse,
    _Node,
    _empty_counts,
    project_public_result,
)

CANARY_IDS = (
    "ORDER_AND_DEDUP",
    "FORBIDDEN_FIELD_NONLEAKAGE",
    "REFERENCE_REJECTION",
    "HTTP_STATUS_GATE",
    "REDIRECT_NORMALIZATION",
    "REDIRECT_HOP_CAP",
    "MAX_RESPONSE_BODY_BYTES_BOUNDARY",
    "MAX_TOTAL_RECEIVED_BODY_BYTES_BOUNDARY",
    "MAX_DEPTH_BOUNDARY_AND_SCHEMA_CODE",
    "MAX_REQUESTED_PAGES_BOUNDARY",
    "MAX_ACCEPTED_HANDLES_BOUNDARY",
    "MAX_PER_PAGE_HANDLES_BOUNDARY",
    "CLOSED_SCHEMA",
    "ONE_SHOT_CLAIM",
)


def _canonical_digest(value: object) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _response(
    body: bytes = b"",
    *,
    status: int = 200,
    content_type: str | None = "text/html; charset=utf-8",
    locations: tuple[str, ...] = (),
    force_eof: bool | None = None,
    receive_counter: list[int] | None = None,
    opaque_headers: tuple[tuple[str, str], ...] = (),
    opaque_cookies: tuple[str, ...] = (),
) -> TransportResponse:
    def receive(allowance: int) -> BodyDelivery:
        if receive_counter is not None:
            receive_counter[0] += 1
        delivered = body[:allowance]
        end_of_stream = len(body) <= allowance if force_eof is None else force_eof
        return BodyDelivery(delivered, end_of_stream)

    return TransportResponse(
        status=status,
        content_type=content_type,
        location_values=locations,
        receive=receive,
        opaque_headers=opaque_headers,
        opaque_cookies=opaque_cookies,
    )


class _MemoryTransport:
    def __init__(self, responses: dict[str, TransportResponse | list[TransportResponse]]) -> None:
        self._responses = {
            path: value if isinstance(value, list) else [value]
            for path, value in responses.items()
        }
        self.calls: list[str] = []

    def __call__(self, path: str) -> TransportResponse:
        self.calls.append(path)
        responses = self._responses[path]
        if len(responses) == 1:
            return responses[0]
        return responses.pop(0)


def _run(
    responses: dict[str, TransportResponse | list[TransportResponse]],
    *,
    claim: AtomicClaim | None = None,
    initial_total_received_body_bytes: int = 0,
    parser: Callable[[bytes], list[str]] | None = None,
) -> tuple[FirstPartyGraphController, _MemoryTransport, AdapterOutput]:
    transport = _MemoryTransport(responses)
    options: dict[str, Any] = {
        "claim": claim,
        "initial_total_received_body_bytes": initial_total_received_body_bytes,
    }
    if parser is not None:
        options["parser"] = parser
    controller = FirstPartyGraphController(transport, **options)
    return controller, transport, controller.run()


def _result(output: AdapterOutput) -> dict[str, Any]:
    assert output.public_result is not None
    return output.public_result


def _node_map(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    nodes = result["nodes"]
    assert isinstance(nodes, list)
    return {str(node["path"]): node for node in nodes if isinstance(node, dict)}


def _order_and_dedup() -> object:
    root = (
        b'<a href="/monetarypolicy/z.htm">z</a>'
        b'<a href="/monetarypolicy/a.htm">a</a>'
        b'<a href="/monetarypolicy/z.htm">repeat</a>'
    )
    _, _, output = _run(
        {
            ROOT_PATH: _response(root),
            "/monetarypolicy/a.htm": _response(),
            "/monetarypolicy/z.htm": _response(),
        }
    )
    result = _result(output)
    assert [node["path"] for node in result["nodes"]] == [
        ROOT_PATH,
        "/monetarypolicy/a.htm",
        "/monetarypolicy/z.htm",
    ]
    assert result["disposition_counts"]["DUPLICATE_HANDLE"] == 1
    return result


def _forbidden_field_nonleakage() -> object:
    marker = "CANARY_053_FORBIDDEN"
    body = f"""<html><head><title>{marker}</title>
<meta name="description" content="{marker}"><script>{marker}</script>
<style>/*{marker}*/</style></head><body><!--{marker}-->{marker}
<a href="/monetarypolicy/safe.htm">{marker}</a></body></html>""".encode()
    _, _, successful = _run(
        {
            ROOT_PATH: _response(
                body,
                opaque_headers=(("X-Canary", marker),),
                opaque_cookies=(marker,),
            ),
            "/monetarypolicy/safe.htm": _response(),
        }
    )
    successful_result = _result(successful)
    assert marker not in json.dumps(successful_result, sort_keys=True)

    def failing_parser(_: bytes) -> list[str]:
        raise ValueError(marker)

    _, _, failed = _run({ROOT_PATH: _response(body)}, parser=failing_parser)
    failed_result = _result(failed)
    assert failed_result["terminal_code"] == "TERMINAL_ROOT_FAILURE"
    assert marker not in json.dumps(failed_result, sort_keys=True)
    return [successful_result, failed_result]


def _reference_rejection() -> object:
    cases = {
        "REJECT_QUERY": "/monetarypolicy/query.htm?q=1",
        "REJECT_FRAGMENT": "/monetarypolicy/fragment.htm#x",
        "REJECT_PERCENT_ESCAPE": "/monetarypolicy/%61.htm",
        "REJECT_BACKSLASH": "/monetarypolicy\\bad.htm",
        "REJECT_PROTOCOL_RELATIVE_REFERENCE": "//www.federalreserve.gov/monetarypolicy/x.htm",
        "REJECT_SCHEME": "http://www.federalreserve.gov/monetarypolicy/x.htm",
        "REJECT_HOST": "https://example.invalid/monetarypolicy/x.htm",
        "REJECT_PORT": "https://www.federalreserve.gov:444/monetarypolicy/x.htm",
        "REJECT_USERINFO": "https://user@www.federalreserve.gov/monetarypolicy/x.htm",
        "REJECT_PATH": "/outside/x.htm",
    }
    body = "".join(f'<a href="{href}">hidden</a>' for href in cases.values()).encode()
    _, _, output = _run({ROOT_PATH: _response(body)})
    result = _result(output)
    counts = result["disposition_counts"]
    for code in cases:
        assert counts[code] == 1
    assert len(result["nodes"]) == 1
    for href in cases.values():
        assert href not in json.dumps(result, sort_keys=True)
    return result


def _http_status_gate() -> object:
    outputs = []
    for status in (199, 304, 404, 500, 200, 299):
        received = [0]
        _, _, output = _run(
            {ROOT_PATH: _response(b"STATUS_CANARY", status=status, receive_counter=received)}
        )
        result = _result(output)
        node = _node_map(result)[ROOT_PATH]
        if status in (200, 299):
            assert received == [1]
            assert node["disposition_code"] == "FETCHED_PARSED"
        else:
            assert received == [0]
            assert node["disposition_code"] == "REJECT_HTTP_STATUS"
            assert result["terminal_code"] == "TERMINAL_ROOT_FAILURE"
        assert "STATUS_CANARY" not in json.dumps(result, sort_keys=True)
        outputs.append(result)
    return outputs


def _redirect_normalization() -> object:
    rejected = (
        (),
        ("/monetarypolicy/a.htm", "/monetarypolicy/b.htm"),
        ("https://user@www.federalreserve.gov/monetarypolicy/a.htm",),
        ("/monetarypolicy/a.htm?q=1",),
        ("/monetarypolicy/a.htm#x",),
        ("/monetarypolicy/%61.htm",),
        ("/monetarypolicy\\a.htm",),
        ("//www.federalreserve.gov/monetarypolicy/a.htm",),
        ("http://www.federalreserve.gov/monetarypolicy/a.htm",),
        ("https://example.invalid/monetarypolicy/a.htm",),
        ("https://www.federalreserve.gov:444/monetarypolicy/a.htm",),
        ("/monetarypolicy/../../outside.htm",),
    )
    outputs = []
    for locations in rejected:
        received = [0]
        controller, transport, output = _run(
            {
                ROOT_PATH: _response(
                    b"REDIRECT_CANARY",
                    status=302,
                    locations=locations,
                    receive_counter=received,
                )
            }
        )
        result = _result(output)
        assert transport.calls == [ROOT_PATH]
        assert controller.transport_attempts == 1
        assert received == [0]
        assert _node_map(result)[ROOT_PATH]["disposition_code"] == (
            "REJECT_REDIRECT_OFF_ALLOWLIST"
        )
        assert "REDIRECT_CANARY" not in json.dumps(result, sort_keys=True)
        outputs.append(result)

    safe_path = "/monetarypolicy/safe.htm"
    _, transport, safe = _run(
        {
            ROOT_PATH: _response(status=302, locations=("monetarypolicy/safe.htm",)),
            safe_path: _response(),
        }
    )
    safe_result = _result(safe)
    assert transport.calls[:2] == [ROOT_PATH, safe_path]
    assert _node_map(safe_result)[ROOT_PATH]["disposition_code"] == "FETCHED_PARSED"
    outputs.append(safe_result)
    return outputs


def _redirect_hop_cap() -> object:
    received = [0]
    target = "/monetarypolicy/one.htm"
    _, transport, output = _run(
        {
            ROOT_PATH: _response(status=301, locations=(target,)),
            target: _response(
                b"SECOND_REDIRECT_CANARY",
                status=308,
                locations=("/monetarypolicy/SECOND_REDIRECT_CANARY.htm",),
                receive_counter=received,
            ),
        }
    )
    result = _result(output)
    assert transport.calls == [ROOT_PATH, target]
    assert received == [0]
    assert _node_map(result)[ROOT_PATH]["disposition_code"] == "REJECT_REDIRECT_CHAIN"
    assert "SECOND_REDIRECT_CANARY" not in json.dumps(result, sort_keys=True)
    return result


def _max_response_body_bytes_boundary() -> object:
    outputs = []
    for size in (MAX_RESPONSE_BODY_BYTES - 1, MAX_RESPONSE_BODY_BYTES):
        controller, _, output = _run({ROOT_PATH: _response(b"x" * size)})
        result = _result(output)
        assert controller.total_received_body_bytes == size
        assert _node_map(result)[ROOT_PATH]["disposition_code"] == "FETCHED_PARSED"
        outputs.append(result)

    controller, _, truncated = _run(
        {
            ROOT_PATH: _response(
                b"x" * MAX_RESPONSE_BODY_BYTES,
                force_eof=False,
            )
        }
    )
    result = _result(truncated)
    assert controller.total_received_body_bytes == MAX_RESPONSE_BODY_BYTES
    assert _node_map(result)[ROOT_PATH]["disposition_code"] == "RECEIPT_TOO_LARGE"
    assert result["terminal_code"] == "TERMINAL_ROOT_FAILURE"
    assert controller.transport_attempts == 1
    outputs.append(result)
    return outputs


def _max_total_received_body_bytes_boundary() -> object:
    initial = 15_728_640
    allowance = 1_048_576
    exact, _, exact_output = _run(
        {ROOT_PATH: _response(b"x" * allowance)},
        initial_total_received_body_bytes=initial,
    )
    exact_result = _result(exact_output)
    assert exact.total_received_body_bytes == MAX_TOTAL_RECEIVED_BODY_BYTES
    assert exact_result["terminal_code"] == "TERMINAL_CAP_REACHED"
    assert _node_map(exact_result)[ROOT_PATH]["disposition_code"] == "FETCHED_PARSED"

    truncated, _, truncated_output = _run(
        {ROOT_PATH: _response(b"x" * allowance, force_eof=False)},
        initial_total_received_body_bytes=initial,
    )
    truncated_result = _result(truncated_output)
    assert truncated.total_received_body_bytes == MAX_TOTAL_RECEIVED_BODY_BYTES
    assert truncated_result["terminal_code"] == "TERMINAL_CAP_REACHED"
    assert _node_map(truncated_result)[ROOT_PATH]["disposition_code"] == (
        "TOTAL_BYTE_CAP_REACHED"
    )

    called = [0]

    def forbidden_transport(path: str) -> TransportResponse:
        del path
        called[0] += 1
        return _response()

    zero = FirstPartyGraphController(
        forbidden_transport,
        initial_total_received_body_bytes=MAX_TOTAL_RECEIVED_BODY_BYTES,
    )
    zero_result = _result(zero.run())
    assert called == [0]
    assert zero.transport_attempts == 0
    assert zero.requested_pages == 0
    assert zero_result["terminal_code"] == "TERMINAL_CAP_REACHED"
    return [exact_result, truncated_result, zero_result]


def _max_depth_boundary_and_schema_code() -> object:
    depth_one = "/monetarypolicy/d1.htm"
    depth_two = "/monetarypolicy/d2.htm"
    depth_three = "/monetarypolicy/d3.htm"
    _, transport, output = _run(
        {
            ROOT_PATH: _response(f'<a href="{depth_one}">one</a>'.encode()),
            depth_one: _response(f'<a href="{depth_two}">two</a>'.encode()),
            depth_two: _response(f'<a href="{depth_three}">three</a>'.encode()),
        }
    )
    result = _result(output)
    nodes = _node_map(result)
    assert nodes[depth_two]["depth"] == 2
    assert nodes[depth_two]["disposition_code"] == "FETCHED_NOT_EXPANDED_DEPTH"
    assert depth_three not in nodes
    assert depth_three not in transport.calls
    assert "FETCHED_NOT_EXPANDED_DEPTH" not in result["disposition_counts"]
    return result


def _max_requested_pages_boundary() -> object:
    paths = [f"/monetarypolicy/p{index:02d}.htm" for index in range(32)]
    body = "".join(f'<a href="{path}">x</a>' for path in paths).encode()
    responses = {ROOT_PATH: _response(body)}
    responses.update({path: _response() for path in paths})
    controller, transport, output = _run(responses)
    result = _result(output)
    assert controller.requested_pages == 32
    assert len(transport.calls) == 32
    assert transport.calls == [ROOT_PATH, *paths[:31]]
    assert _node_map(result)[paths[31]]["disposition_code"] == "PAGE_CAP_NOT_REQUESTED"
    assert result["terminal_code"] == "TERMINAL_CAP_REACHED"
    return result


def _max_accepted_handles_boundary() -> object:
    controller = FirstPartyGraphController(lambda path: _response())
    nodes = {ROOT_PATH: _Node(ROOT_PATH, 0, None)}
    counts = _empty_counts()
    for page in range(4):
        hrefs = [
            f"/monetarypolicy/h{page * 64 + index:03d}.htm" for index in range(64)
        ]
        controller._accept_hrefs(hrefs, nodes[ROOT_PATH], nodes, counts)
    assert len(nodes) == MAX_ACCEPTED_HANDLES
    assert counts["HANDLE_CAP_REJECTED"] == 1
    assert counts["PER_PAGE_HANDLE_CAP_REJECTED"] == 0
    assert "/monetarypolicy/h255.htm" not in nodes
    return {
        "accepted_paths_sha256": _canonical_digest(sorted(nodes)),
        "disposition_counts": counts,
    }


def _max_per_page_handles_boundary() -> object:
    controller = FirstPartyGraphController(lambda path: _response())
    nodes = {ROOT_PATH: _Node(ROOT_PATH, 0, None)}
    counts = _empty_counts()
    hrefs = [f"/monetarypolicy/p{index:02d}.htm" for index in range(65)]
    controller._accept_hrefs(list(reversed(hrefs)), nodes[ROOT_PATH], nodes, counts)
    assert len(nodes) == 65
    assert counts["PER_PAGE_HANDLE_CAP_REJECTED"] == 1
    assert counts["HANDLE_CAP_REJECTED"] == 0
    assert hrefs[-1] not in nodes
    return {
        "accepted_paths_sha256": _canonical_digest(sorted(nodes)),
        "disposition_counts": counts,
    }


def _closed_schema() -> object:
    base = _result(_run({ROOT_PATH: _response()})[2])
    attempts = []
    additions = {
        "title": "hidden",
        "url": "https://example.invalid/",
        "status": 200,
        "byte_count": 1,
        "timestamp": "hidden",
        "error": "hidden",
    }
    for key, value in additions.items():
        candidate = dict(base)
        candidate[key] = value
        output = project_public_result(candidate)
        assert output.terminal_code == "CONTAMINATED_OR_SCOPE_VIOLATION"
        assert output.public_result is None
        attempts.append(output.terminal_code)

    candidate = dict(base)
    candidate["nodes"] = [dict(base["nodes"][0])]
    candidate["nodes"][0]["disposition_code"] = "UNKNOWN_DISPOSITION"
    output = project_public_result(candidate)
    assert output.terminal_code == "CONTAMINATED_OR_SCOPE_VIOLATION"
    assert output.public_result is None
    attempts.append(output.terminal_code)
    return attempts


def _one_shot_claim() -> object:
    claim = AtomicClaim()
    calls = [0]

    def failing_transport(path: str) -> TransportResponse:
        del path
        calls[0] += 1
        raise RuntimeError("ONE_SHOT_CANARY")

    first = FirstPartyGraphController(failing_transport, claim=claim)
    first_result = _result(first.run())
    second = FirstPartyGraphController(failing_transport, claim=claim)
    second_result = _result(second.run())
    assert calls == [1]
    assert first_result["terminal_code"] == "TERMINAL_ROOT_FAILURE"
    assert _node_map(first_result)[ROOT_PATH]["disposition_code"] == (
        "FETCH_TRANSPORT_FAILURE"
    )
    assert second_result["terminal_code"] == "CLAIM_ALREADY_CONSUMED"
    assert second.transport_attempts == 0
    assert "ONE_SHOT_CANARY" not in json.dumps([first_result, second_result], sort_keys=True)
    return [first_result, second_result]


CANARIES: dict[str, Callable[[], object]] = {
    "ORDER_AND_DEDUP": _order_and_dedup,
    "FORBIDDEN_FIELD_NONLEAKAGE": _forbidden_field_nonleakage,
    "REFERENCE_REJECTION": _reference_rejection,
    "HTTP_STATUS_GATE": _http_status_gate,
    "REDIRECT_NORMALIZATION": _redirect_normalization,
    "REDIRECT_HOP_CAP": _redirect_hop_cap,
    "MAX_RESPONSE_BODY_BYTES_BOUNDARY": _max_response_body_bytes_boundary,
    "MAX_TOTAL_RECEIVED_BODY_BYTES_BOUNDARY": _max_total_received_body_bytes_boundary,
    "MAX_DEPTH_BOUNDARY_AND_SCHEMA_CODE": _max_depth_boundary_and_schema_code,
    "MAX_REQUESTED_PAGES_BOUNDARY": _max_requested_pages_boundary,
    "MAX_ACCEPTED_HANDLES_BOUNDARY": _max_accepted_handles_boundary,
    "MAX_PER_PAGE_HANDLES_BOUNDARY": _max_per_page_handles_boundary,
    "CLOSED_SCHEMA": _closed_schema,
    "ONE_SHOT_CLAIM": _one_shot_claim,
}


def main() -> int:
    records = []
    failed = False
    for canary_id in CANARY_IDS:
        try:
            evidence = CANARIES[canary_id]()
        except Exception:
            records.append({"id": canary_id, "status": "FAIL"})
            failed = True
        else:
            records.append(
                {
                    "id": canary_id,
                    "status": "PASS",
                    "evidence_sha256": _canonical_digest(evidence),
                }
            )
    output = {
        "schema": "quantresearch-first-party-graph-offline-conformance/v1",
        "protocol_id": PROTOCOL_ID,
        "fixture_origin": "IN_MEMORY_SYNTHETIC_ONLY",
        "network_access": "PROHIBITED",
        "canaries": records,
        "terminal_code": "PASS" if not failed else "CONTAMINATED_OR_SCOPE_VIOLATION",
        "next_stage_authorization": "NOT_AUTHORIZED",
    }
    print(json.dumps(output, sort_keys=True, separators=(",", ":")))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
