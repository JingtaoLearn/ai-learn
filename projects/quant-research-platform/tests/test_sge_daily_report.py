from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from gold_research.focus_contract import ContractViolation, ReceiptMetadata
from gold_research.sge_daily_report import (
    CONTENT_TYPE,
    EXPECTED_HEADERS,
    build_request,
    capture_response,
    parse_version,
    request_identity_sha256,
    select_historical_dates,
)


def _row(
    *,
    day: str = "2026-09-07",
    instrument: str = "Au99.99",
    close: str = "949.60",
    cells: int = 14,
) -> str:
    values = [day, instrument, "964", "968", "943", close] + [""] * 8
    values = values[:cells]
    return "<tr>" + "".join(f"<td>{value}</td>" for value in values) + "</tr>"


def _table(*rows: str, headers: tuple[str, ...] = EXPECTED_HEADERS) -> str:
    header = "<tr>" + "".join(f"<th>{value}</th>" for value in headers) + "</tr>"
    return "<table>" + header + "".join(rows) + "</table>"


def _html(*tables: str) -> bytes:
    return ("<html><body>" + "".join(tables) + "</body></html>").encode()


def test_request_is_exact_one_date_official_identity() -> None:
    request = build_request("2026-09-07")
    assert request.method == "GET"
    assert request.ordered_query == (
        ("start_date", "2026-09-07"),
        ("end_date", "2026-09-07"),
        ("inst_ids", "Au99.99"),
    )
    assert request.url == (
        "https://en.sge.com.cn/data/data_daily_international_new?"
        "start_date=2026-09-07&end_date=2026-09-07&inst_ids=Au99.99"
    )
    assert "&p=" not in request.url
    with pytest.raises(ContractViolation):
        build_request("2026-9-7")


def test_parser_accepts_exact_table_row_and_finite_positive_decimal() -> None:
    raw = _html(_table(_row()))
    parsed = parse_version(raw, "2026-09-07")
    assert parsed.trading_date == "2026-09-07"
    assert parsed.instrument == "Au99.99"
    assert parsed.field == "Close"
    assert parsed.value_decimal == "949.60"
    assert parsed.raw_sha256 == hashlib.sha256(raw).hexdigest()


@pytest.mark.parametrize(
    "raw",
    [
        _html(
            "<table><tr>"
            + "".join(f"<th>{value}</td>" for value in EXPECTED_HEADERS)
            + "</tr>"
            + _row()
            + "</table>"
        ),
        _html(
            "<table><tr>"
            + "".join(f"<th>{value}</th>" for value in EXPECTED_HEADERS)
            + "</tr>"
            + _row().replace("</td>", "</th>")
            + "</table>"
        ),
    ],
)
def test_parser_rejects_mismatched_cell_closing_tags(raw: bytes) -> None:
    with pytest.raises(ContractViolation, match="closing tag"):
        parse_version(raw, "2026-09-07")


@pytest.mark.parametrize(
    ("raw", "kwargs"),
    [
        (_html(_table(_row(), _row())), {}),
        (_html(_table(_row()), _table(_row())), {}),
        (_html(_table(_row(), _row(instrument="Other"))), {}),
        (_html(_table(_row(), headers=("date",) + EXPECTED_HEADERS[1:])), {}),
        (_html(_table(_row(day="2026-09-08"))), {}),
        (_html(_table(_row(instrument="AU99.99"))), {}),
        (_html(_table(_row(close="Infinity"))), {}),
        (_html(_table(_row(close="0"))), {}),
        (_html(_table(_row(close="-1"))), {}),
        (_html(_table(_row(close="not-decimal"))), {}),
        (_html(_table(_row(cells=13))), {}),
        (_html(_table()), {}),
        (b"\xff", {}),
        (_html(_table(_row())), {"http_status": 500}),
        (_html(_table(_row())), {"content_type": "text/html; charset=UTF-8"}),
    ],
)
def test_parser_rejects_critical_malformed_or_ambiguous_cases(raw: bytes, kwargs: dict) -> None:
    with pytest.raises(ContractViolation):
        parse_version(raw, "2026-09-07", **kwargs)


def test_accepted_sealed_official_fixture_replays_when_bound() -> None:
    fixture = os.environ.get("SGE_ACCEPTED_FIXTURE")
    if fixture is None:
        pytest.skip("formal runner supplies the sealed accepted fixture")
    raw = Path(fixture).read_bytes()
    parsed = parse_version(raw, "2026-09-07")
    assert parsed.raw_sha256 == "a82b78cc3c5e1917114538236c600bb698ee01c88159f79339c9d4cd24fdf93c"
    assert parsed.raw_size_bytes == 75_584
    assert parsed.value_decimal == "949.60"


def test_capture_seals_raw_and_receipt_before_parse_failure(tmp_path: Path) -> None:
    identity = build_request("2026-09-07")
    receipt = ReceiptMetadata(
        retrieved_at_utc="2026-09-08T00:00:00Z",
        retrieved_at_asia_shanghai="2026-09-08T08:00:00+08:00",
        receipt_sequence=1,
        http_status=200,
        content_type=CONTENT_TYPE,
        request_sha256=request_identity_sha256(identity),
    )
    raw = _html(_table(_row(close="Infinity")))
    with pytest.raises(ContractViolation):
        capture_response(identity, raw, receipt, tmp_path)
    raw_path = tmp_path / "raw" / hashlib.sha256(raw).hexdigest()
    receipt_path = tmp_path / "receipts" / "000001.json"
    assert raw_path.read_bytes() == raw
    assert receipt_path.is_file()
    assert raw_path.stat().st_mode & 0o777 == 0o444
    assert receipt_path.stat().st_mode & 0o777 == 0o444
    with pytest.raises(FileExistsError):
        capture_response(identity, raw, receipt, tmp_path)


def test_historical_selector_uses_only_ordered_mature_calendar_identities() -> None:
    dates = [f"2024-01-{day:02d}" for day in range(1, 32)]
    with pytest.raises(ContractViolation, match="frozen at 758"):
        select_historical_dates(dates, datetime(2026, 1, 1, tzinfo=timezone.utc), count=10)
    with pytest.raises(ContractViolation, match="fewer than 758"):
        select_historical_dates(dates, datetime(2026, 1, 1, tzinfo=timezone.utc))
