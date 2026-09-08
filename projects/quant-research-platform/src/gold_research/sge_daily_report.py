"""Strict offline parser and append-only capture primitives for the SGE Daily Report."""

from __future__ import annotations

import hashlib
import os
from dataclasses import asdict
from datetime import date, datetime, time, timedelta
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from gold_research.focus_contract import (
    COLLECTION_CLASS,
    FIELD,
    INSTRUMENT,
    TIMEZONE,
    ContractViolation,
    ParsedVersion,
    ReceiptMetadata,
    RequestIdentity,
    canonical_json_bytes,
    require_positive_decimal,
)

BASE_URL = "https://en.sge.com.cn/data/data_daily_international_new"
CONTENT_TYPE = "text/html;charset=UTF-8"
EXPECTED_HEADERS = (
    "Date",
    "Contract",
    "Open",
    "Highest",
    "Lowest",
    "Close",
    "Up/Down (yuan)",
    "Up/Down (%)",
    "Weighted Average Price",
    "Volume (Kg)",
    "Amount (yuan)",
    "Open Interest (Lot)",
    "Direction",
    "Delivery Volume (Lot)",
)


def _iso_date(value: str) -> str:
    if not isinstance(value, str):
        raise ContractViolation("expected date must be a canonical ISO string")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ContractViolation("expected date must be a canonical ISO string") from exc
    if parsed.isoformat() != value:
        raise ContractViolation("expected date must be a canonical ISO string")
    return value


def build_request(expected_date: str) -> RequestIdentity:
    """Build the sole admitted one-date request identity without performing I/O."""

    expected_date = _iso_date(expected_date)
    query = (
        ("start_date", expected_date),
        ("end_date", expected_date),
        ("inst_ids", INSTRUMENT),
    )
    return RequestIdentity(
        method="GET",
        url=f"{BASE_URL}?{urlencode(query)}",
        ordered_query=query,
        expected_date=expected_date,
    )


def request_identity_sha256(identity: RequestIdentity) -> str:
    return hashlib.sha256(canonical_json_bytes(asdict(identity))).hexdigest()


def _clean(text: str) -> str:
    return " ".join(text.split())


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self._depth = 0
        self._rows: list[list[str]] | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._cell_tag: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag == "br" and self._cell is not None:
            self._cell.append(" ")
        elif tag == "table":
            if self._depth == 0:
                self._rows = []
            self._depth += 1
        elif self._depth == 1 and tag == "tr":
            if self._row is not None:
                raise ContractViolation("nested or overlapping rows are forbidden")
            self._row = []
        elif self._depth == 1 and tag in {"th", "td"}:
            if self._row is None or self._cell is not None:
                raise ContractViolation("cell occurs outside one unambiguous row")
            self._cell = []
            self._cell_tag = tag

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._depth == 1 and tag in {"th", "td"} and self._cell is not None:
            if tag != self._cell_tag:
                raise ContractViolation("cell closing tag does not match its opening tag")
            assert self._row is not None
            self._row.append(_clean("".join(self._cell)))
            self._cell = None
            self._cell_tag = None
        elif self._depth == 1 and tag == "tr" and self._row is not None:
            if self._cell is not None:
                raise ContractViolation("row ended before its cell")
            if self._row:
                assert self._rows is not None
                self._rows.append(self._row)
            self._row = None
        elif tag == "table" and self._depth:
            self._depth -= 1
            if self._depth == 0:
                if self._cell is not None or self._row is not None:
                    raise ContractViolation("table ended with an incomplete row")
                assert self._rows is not None
                self.tables.append(self._rows)
                self._rows = None

    def close(self) -> None:
        super().close()
        if self._depth or self._cell is not None or self._cell_tag is not None or self._row is not None:
            raise ContractViolation("HTML contains an unclosed table structure")


def parse_version(
    raw: bytes,
    expected_date: str,
    *,
    http_status: int = 200,
    content_type: str = CONTENT_TYPE,
) -> ParsedVersion:
    """Parse exactly one matching table containing exactly one 14-cell body row."""

    expected_date = _iso_date(expected_date)
    if not isinstance(raw, bytes) or not raw:
        raise ContractViolation("response must contain immutable non-empty bytes")
    if http_status != 200:
        raise ContractViolation("HTTP status must be exactly 200")
    if content_type != CONTENT_TYPE:
        raise ContractViolation("content type must be exactly text/html;charset=UTF-8")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ContractViolation("response is not strict UTF-8") from exc

    parser = _TableParser()
    try:
        parser.feed(text)
        parser.close()
    except ContractViolation:
        raise
    except Exception as exc:
        raise ContractViolation("response is not closed HTML table syntax") from exc

    matching = [rows for rows in parser.tables if rows and tuple(rows[0]) == EXPECTED_HEADERS]
    if len(matching) != 1:
        raise ContractViolation(f"expected exactly one matching table, got {len(matching)}")
    body = matching[0][1:]
    if len(body) != 1:
        raise ContractViolation(f"expected exactly one body row, got {len(body)}")
    row = body[0]
    if len(row) != len(EXPECTED_HEADERS):
        raise ContractViolation("body row must contain exactly 14 cells")
    if row[0] != expected_date:
        raise ContractViolation("body row date does not match the expected date")
    if row[1] != INSTRUMENT:
        raise ContractViolation("body row instrument is not exactly Au99.99")
    close = require_positive_decimal(row[5], FIELD)
    return ParsedVersion(
        trading_date=expected_date,
        instrument=INSTRUMENT,
        field=FIELD,
        value_decimal=str(close),
        unit="CNY/g",
        raw_sha256=hashlib.sha256(raw).hexdigest(),
        raw_size_bytes=len(raw),
    )


def _write_create_only(path: Path, payload: bytes, mode: int = 0o444) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)
    os.chmod(path, mode, follow_symlinks=False)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _verify_regular_single_link(path: Path, expected: bytes) -> None:
    info = path.lstat()
    if path.is_symlink() or not path.is_file() or info.st_nlink != 1:
        raise ContractViolation("capture target must be a regular single-link file")
    if path.read_bytes() != expected:
        raise ContractViolation("existing capture bytes conflict with immutable identity")


def capture_response(
    identity: RequestIdentity,
    raw: bytes,
    receipt: ReceiptMetadata,
    data_root: Path,
) -> ParsedVersion:
    """Seal raw bytes and sanitized receipt before attempting the strict parse."""

    if identity != build_request(identity.expected_date):
        raise ContractViolation("request identity is not the admitted request")
    if receipt.request_sha256 != request_identity_sha256(identity):
        raise ContractViolation("receipt does not bind the request identity")
    if receipt.receipt_sequence < 1:
        raise ContractViolation("receipt sequence must be positive")

    raw_sha256 = hashlib.sha256(raw).hexdigest()
    raw_path = data_root / "raw" / raw_sha256
    if raw_path.exists():
        _verify_regular_single_link(raw_path, raw)
    else:
        _write_create_only(raw_path, raw)

    receipt_payload = canonical_json_bytes(
        {
            "schema": "quant-research/sge-retrieval-receipt/v1",
            "collection_class": COLLECTION_CLASS,
            "expected_date": identity.expected_date,
            "raw_sha256": raw_sha256,
            "raw_size_bytes": len(raw),
            **asdict(receipt),
        }
    )
    receipt_path = data_root / "receipts" / f"{receipt.receipt_sequence:06d}.json"
    _write_create_only(receipt_path, receipt_payload + b"\n")

    return parse_version(
        raw,
        identity.expected_date,
        http_status=receipt.http_status,
        content_type=receipt.content_type,
    )


def select_historical_dates(
    eligible_dates: list[str], lock_time: datetime, *, count: int = 758
) -> tuple[str, ...]:
    """Choose the latest mature contiguous official identities without reading outcomes."""

    if count != 758:
        raise ContractViolation("historical interval cardinality is frozen at 758")
    if lock_time.tzinfo is None or lock_time.utcoffset() is None:
        raise ContractViolation("capture lock must be timezone-aware")
    parsed = [date.fromisoformat(_iso_date(item)) for item in eligible_dates]
    if parsed != sorted(set(parsed)):
        raise ContractViolation("official eligible dates must be unique and ordered")
    mature = []
    for item in parsed:
        cutoff_day = item + timedelta(days=10)
        cutoff = datetime.combine(
            cutoff_day,
            time(23, 59, 59),
            tzinfo=ZoneInfo(TIMEZONE),
        )
        if cutoff < lock_time.astimezone(ZoneInfo(TIMEZONE)):
            mature.append(item.isoformat())
    if len(mature) < count:
        raise ContractViolation("fewer than 758 mature official eligible dates")
    return tuple(mature[-count:])


__all__ = [
    "BASE_URL",
    "CONTENT_TYPE",
    "EXPECTED_HEADERS",
    "build_request",
    "capture_response",
    "parse_version",
    "request_identity_sha256",
    "select_historical_dates",
    "TIMEZONE",
]
