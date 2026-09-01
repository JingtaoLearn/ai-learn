from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest
import requests

from quant_platform.catalog import initialize_catalog
from quant_platform.dataset_service import (
    MAX_PROVIDER_RESPONSE_BYTES,
    SSE_CURRENT_ENDPOINT,
    SSE_CURRENT_PARAMS,
    DatasetCatalogItem,
    DatasetResolutionError,
    DatasetService,
    FetchedDailyBars,
    SseCurrentDailySource,
    XSHGCalendar,
    YahooChartSource,
)
from quant_platform.datasets import publish_snapshot, snapshot_status
from quant_platform.experiment_service import ExperimentService

from test_experiment_service import _task


METADATA = {
    "instrument": "601328.SS",
    "provider": "yahoo-chart-api",
    "market": "XSHG",
    "currency": "CNY",
    "adjustment": "unadjusted",
}
ITEM = DatasetCatalogItem(
    dataset_id="601328.SS",
    name="Bank of Communications (601328.SS)",
    instrument="601328.SS",
    provider="yahoo-chart-api",
    market="XSHG",
    currency="CNY",
    adjustment="unadjusted",
    calendar="XSHG",
    default_start="2024-01-02",
)
SESSIONS = ["2026-08-18", "2026-08-19", "2026-08-20"]


def _bars(dates: list[str], closes: list[float] | None = None) -> pd.DataFrame:
    closes = closes or [6.1 + index / 10 for index in range(len(dates))]
    return pd.DataFrame(
        {
            "Date": pd.to_datetime(dates),
            "Open": [value - 0.02 for value in closes],
            "High": [value + 0.04 for value in closes],
            "Low": [value - 0.05 for value in closes],
            "Close": closes,
            "Volume": [1000.0 + index for index in range(len(dates))],
        }
    )


class FixedCalendar:
    source_identity = {
        "calendar": "XSHG",
        "library": "test-calendar",
        "version": "2026",
    }

    def __init__(self, sessions: list[str] | None = None):
        self.expected = pd.DatetimeIndex(pd.to_datetime(sessions or SESSIONS))
        self.calls: list[tuple[str, str]] = []

    def sessions(self, start: str, end: str) -> list[str]:
        self.calls.append((start, end))
        start_at = pd.Timestamp(start)
        end_at = pd.Timestamp(end)
        return [
            str(value.date())
            for value in self.expected
            if start_at <= value <= end_at
        ]


class FixedSource:
    provider = "yahoo-chart-api"

    def __init__(
        self,
        bars: pd.DataFrame,
        *,
        latest_close: str = "2026-08-20",
        revision: str = "response-v1",
        provider: str = "yahoo-chart-api",
    ):
        self.provider = provider
        self.bars = bars
        self.latest_close = latest_close
        self.revision = revision
        self.fetch_calls: list[tuple[str, str, str]] = []
        self.latest_calls: list[str] = []

    def latest_available_close(self, instrument: str) -> str:
        self.latest_calls.append(instrument)
        return self.latest_close

    def fetch(self, instrument: str, start: str, end: str) -> FetchedDailyBars:
        self.fetch_calls.append((instrument, start, end))
        return FetchedDailyBars(
            bars=self.bars.copy(),
            source_identity={
                "provider": self.provider,
                "instrument": instrument,
                "request_url": (
                    f"https://query1.finance.yahoo.com/v8/finance/chart/{instrument}"
                ),
                "response_sha256": hashlib.sha256(self.revision.encode()).hexdigest(),
            },
        )


def _dataset_service(
    tmp_path: Path,
    source: FixedSource,
    calendar: FixedCalendar | None = None,
) -> DatasetService:
    catalog = initialize_catalog(tmp_path / "state")
    service = DatasetService(
        catalog,
        sources={source.provider: source},
        calendars={"XSHG": calendar or FixedCalendar()},
    )
    service.register(ITEM)
    return service


def _catalog_task(start: str, end: str) -> dict:
    task = _task("0" * 64)
    task["dataset"] = {
        "dataset_id": ITEM.dataset_id,
        "start": start,
        "end": end,
    }
    task["template"]["parameters"]["evaluation_start"] = start
    task["template"]["parameters"]["evaluation_end"] = end
    return task


def test_catalog_migration_derives_production_item_without_rewriting_snapshot(
    tmp_path: Path,
):
    root = tmp_path / "state"
    published = publish_snapshot(_bars(SESSIONS), root, METADATA)
    snapshot_dir = Path(published["path"])
    before = {path.name: path.read_bytes() for path in snapshot_dir.iterdir()}

    catalog = initialize_catalog(root)

    assert catalog.dataset_detail("601328.SS") == {
        "dataset_id": "601328.SS",
        "name": "Bank of Communications (601328.SS)",
        "instrument": "601328.SS",
        "provider": "yahoo-chart-api",
        "market": "XSHG",
        "currency": "CNY",
        "adjustment": "unadjusted",
        "calendar": "XSHG",
        "default_start": "2026-08-18",
        "created_at": "2026-08-27T00:00:00Z",
    }
    assert snapshot_status(root, "601328.SS")["snapshot_id"] == published["snapshot_id"]
    assert {path.name: path.read_bytes() for path in snapshot_dir.iterdir()} == before


def test_catalog_seed_survives_an_earlier_backfill_without_mutating_identity(
    tmp_path: Path,
):
    root = tmp_path / "state"
    publish_snapshot(_bars(SESSIONS), root, METADATA)
    first_catalog = initialize_catalog(root)
    seeded = first_catalog.dataset_detail("601328.SS")
    publish_snapshot(_bars(["2026-08-17", *SESSIONS]), root, METADATA)

    restarted = initialize_catalog(root)

    assert restarted.dataset_detail("601328.SS") == seeded
    assert seeded["default_start"] == "2026-08-18"


def test_xshg_calendar_uses_pinned_authoritative_holidays_not_weekdays():
    calendar = XSHGCalendar()

    assert calendar.sessions("2026-02-13", "2026-02-24") == [
        "2026-02-13",
        "2026-02-24",
    ]
    assert calendar.source_identity == {
        "calendar": "XSHG",
        "library": "exchange_calendars",
        "version": "4.13.2",
    }
    with pytest.raises(DatasetResolutionError, match="authoritative.*2027"):
        calendar.sessions("2027-01-04", "2027-01-04")


def test_catalog_options_use_latest_authoritative_close_as_default_end(tmp_path: Path):
    source = FixedSource(_bars(SESSIONS), latest_close="2026-08-26")
    service = _dataset_service(
        tmp_path, source, FixedCalendar(["2026-08-26"])
    )

    options = service.list_available()

    assert options == [
        {
            "dataset_id": "601328.SS",
            "name": "Bank of Communications (601328.SS)",
            "instrument": "601328.SS",
            "default_start": "2024-01-02",
            "latest_available_close": "2026-08-26",
            "latest_snapshot_id": None,
        }
    ]
    assert source.latest_calls == ["601328.SS"]


def test_complete_range_resolves_existing_snapshot_without_fetch(tmp_path: Path):
    source = FixedSource(_bars(SESSIONS))
    service = _dataset_service(tmp_path, source)
    published = publish_snapshot(_bars(SESSIONS), service.catalog.state_root, METADATA)

    resolved = service.resolve("601328.SS", "2026-08-18", "2026-08-20")

    assert source.fetch_calls == []
    assert resolved["dataset_id"] == "601328.SS"
    assert resolved["name"] == "Bank of Communications (601328.SS)"
    assert resolved["requested_start"] == "2026-08-18"
    assert resolved["requested_end"] == "2026-08-20"
    assert resolved["snapshot_id"] == published["snapshot_id"]
    assert resolved["update_id"] is None
    assert resolved["lineage"] == {"kind": "legacy_snapshot"}


def test_incomplete_range_fetches_one_generation_and_publishes_provenance(
    tmp_path: Path,
):
    source = FixedSource(_bars(SESSIONS))
    calendar = FixedCalendar()
    service = _dataset_service(tmp_path, source, calendar)
    first = publish_snapshot(
        _bars(["2026-08-18"]), service.catalog.state_root, METADATA
    )

    resolved = service.resolve("601328.SS", "2026-08-18", "2026-08-20")

    assert source.fetch_calls == [
        ("601328.SS", "2026-08-18", "2026-08-20")
    ]
    assert calendar.calls == [("2026-08-18", "2026-08-20")]
    assert resolved["snapshot_id"] != first["snapshot_id"]
    assert Path(first["path"]).is_dir()
    persisted = pd.read_parquet(
        service.catalog.state_root
        / "datasets"
        / "601328.SS"
        / resolved["snapshot_id"]
        / "data.parquet"
    )
    assert persisted["Date"].dt.strftime("%Y-%m-%d").tolist() == SESSIONS
    record = json.loads(Path(resolved["update_path"]).read_text(encoding="utf-8"))
    assert record["source"] == {
        "provider": "yahoo-chart-api",
        "instrument": "601328.SS",
        "request_url": (
            "https://query1.finance.yahoo.com/v8/finance/chart/601328.SS"
        ),
        "response_sha256": hashlib.sha256(b"response-v1").hexdigest(),
    }
    assert record["expected_sessions_source"] == calendar.source_identity
    assert record["result_snapshot_id"] == resolved["snapshot_id"]
    assert resolved["lineage"] == {
        "kind": "verified_update",
        "update_id": resolved["update_id"],
        "source": record["source"],
        "expected_sessions_source": calendar.source_identity,
        "expected_sessions_sha256": record["expected_sessions_sha256"],
        "expected_session_count": record["expected_session_count"],
    }
    repeated = service.resolve("601328.SS", "2026-08-18", "2026-08-20")
    assert repeated["lineage"] == resolved["lineage"]


def test_complete_snapshot_resolution_uses_immutable_claim_after_update_mutation(
    tmp_path: Path,
):
    source = FixedSource(_bars(SESSIONS))
    service = _dataset_service(tmp_path, source)
    resolved = service.resolve("601328.SS", "2026-08-18", "2026-08-20")
    record = Path(resolved["update_path"])
    record.chmod(0o644)

    repeated = service.resolve("601328.SS", "2026-08-18", "2026-08-20")

    assert repeated["lineage"] == resolved["lineage"]


def test_provider_gap_or_unverified_suspension_fails_closed(tmp_path: Path):
    source = FixedSource(_bars(["2026-08-18", "2026-08-20"]))
    service = _dataset_service(tmp_path, source)
    first = publish_snapshot(
        _bars(["2026-08-18"]), service.catalog.state_root, METADATA
    )
    pointer = service.catalog.state_root / "datasets" / "601328.SS" / "latest.json"
    before = pointer.read_bytes()

    with pytest.raises(
        DatasetResolutionError,
        match="provider response is missing expected XSHG sessions.*2026-08-19",
    ):
        service.resolve("601328.SS", "2026-08-18", "2026-08-20")

    assert pointer.read_bytes() == before
    assert snapshot_status(service.catalog.state_root, "601328.SS")["snapshot_id"] == (
        first["snapshot_id"]
    )
    assert not (service.catalog.state_root / "updates").exists()


def test_experiment_identity_binds_catalog_range_and_snapshot_and_rerun_is_frozen(
    tmp_path: Path,
):
    source = FixedSource(_bars(SESSIONS))
    datasets = _dataset_service(tmp_path, source)
    experiment_service = ExperimentService(
        datasets.catalog,
        execution_identity={
            "runner": "quant-platform",
            "source_digest": "e" * 64,
            "runtime_digest": "f" * 64,
        },
        datasets=datasets,
    )

    first = experiment_service.submit(
        _catalog_task("2026-08-18", "2026-08-20"), action_id="create"
    )
    duplicate = experiment_service.submit(
        _catalog_task("2026-08-18", "2026-08-20"), action_id="duplicate"
    )
    narrower = experiment_service.submit(
        _catalog_task("2026-08-18", "2026-08-19"), action_id="narrower"
    )
    frozen = experiment_service.rerun(first["experiment_id"], action_id="rerun")

    detail = experiment_service.experiment_detail(first["experiment_id"])
    rerun = experiment_service.attempt_detail(frozen["attempt_id"])
    assert duplicate["status"] == "DUPLICATE"
    assert narrower["experiment_id"] != first["experiment_id"]
    assert detail["dataset"] == {
        "dataset_id": "601328.SS",
        "name": "Bank of Communications (601328.SS)",
        "instrument": "601328.SS",
        "provider": "yahoo-chart-api",
        "market": "XSHG",
        "currency": "CNY",
        "adjustment": "unadjusted",
        "requested_start": "2026-08-18",
        "requested_end": "2026-08-20",
        "effective_start": "2026-08-18",
        "effective_end": "2026-08-20",
        "snapshot_id": detail["dataset"]["snapshot_id"],
        "canonical_sha256": detail["dataset"]["canonical_sha256"],
        "lineage": detail["dataset"]["lineage"],
    }
    assert detail["dataset"]["lineage"]["kind"] == "verified_update"
    assert detail["dataset"]["lineage"]["source"]["provider"] == "yahoo-chart-api"
    assert (
        detail["dataset"]["lineage"]["expected_sessions_source"]["calendar"]
        == "XSHG"
    )
    lineage = detail["dataset"]["lineage"]
    update_target = (
        datasets.catalog.state_root
        / "updates"
        / ITEM.instrument
        / lineage["update_id"]
    )
    update_target.chmod(0o755)
    (update_target / "update.json").chmod(0o644)
    shutil.rmtree(update_target)
    after_delete = experiment_service.experiment_detail(first["experiment_id"])
    after_delete_rerun = experiment_service.rerun(
        first["experiment_id"], action_id="after-lineage-delete"
    )
    assert after_delete["dataset"]["lineage"] == lineage
    assert (
        experiment_service.attempt_detail(after_delete_rerun["attempt_id"])[
            "resolved"
        ]["dataset"]["lineage"]
        == lineage
    )
    assert rerun["resolved"]["dataset"] == detail["dataset"]
    assert len(experiment_service.list_attempts(first["experiment_id"])) == 3
    assert source.fetch_calls == [
        ("601328.SS", "2026-08-18", "2026-08-20")
    ]


def test_catalog_and_direct_selectors_bind_the_same_verified_snapshot_lineage(
    tmp_path: Path,
):
    source = FixedSource(_bars(SESSIONS))
    datasets = _dataset_service(tmp_path, source)
    experiments = ExperimentService(
        datasets.catalog,
        execution_identity={
            "runner": "quant-platform",
            "source_digest": "e" * 64,
            "runtime_digest": "f" * 64,
        },
        datasets=datasets,
    )

    catalog_resolved = experiments.resolve_task(
        _catalog_task("2026-08-18", "2026-08-20")
    )
    direct_task = _task(catalog_resolved["dataset"]["snapshot_id"])
    direct_task["dataset"]["instrument"] = ITEM.instrument
    direct_resolved = experiments.resolve_task(direct_task)
    created = experiments.submit(direct_task, action_id="direct-lineage")
    connection = datasets.catalog.connect()
    try:
        row = connection.execute(
            "SELECT identity_json FROM experiments WHERE experiment_id = ?",
            (created["experiment_id"],),
        ).fetchone()
    finally:
        connection.close()

    lineage = catalog_resolved["dataset"]["lineage"]
    assert lineage["kind"] == "verified_update"
    assert direct_resolved["dataset"]["lineage"] == lineage
    assert json.loads(row["identity_json"])["dataset"]["lineage"] == lineage


def test_weekend_and_session_bounds_share_effective_experiment_identity(
    tmp_path: Path,
):
    sessions = [
        "2026-08-17",
        "2026-08-18",
        "2026-08-19",
        "2026-08-20",
        "2026-08-21",
    ]
    source = FixedSource(_bars(sessions))
    datasets = _dataset_service(tmp_path, source, FixedCalendar(sessions))
    published = publish_snapshot(
        _bars(sessions), datasets.catalog.state_root, METADATA
    )
    experiments = ExperimentService(
        datasets.catalog,
        execution_identity={
            "runner": "quant-platform",
            "source_digest": "e" * 64,
            "runtime_digest": "f" * 64,
        },
        datasets=datasets,
    )
    weekend_task = _catalog_task("2026-08-16", "2026-08-23")
    session_task = _catalog_task("2026-08-17", "2026-08-21")

    weekend_preview = experiments.preview_task(weekend_task)
    session_preview = experiments.preview_task(session_task)

    assert weekend_preview["experiment_id"] == session_preview["experiment_id"]
    resolved = weekend_preview["resolved"]
    assert resolved["dataset"]["requested_start"] == "2026-08-16"
    assert resolved["dataset"]["requested_end"] == "2026-08-23"
    assert resolved["dataset"]["effective_start"] == "2026-08-17"
    assert resolved["dataset"]["effective_end"] == "2026-08-21"
    assert resolved["dataset"]["snapshot_id"] == published["snapshot_id"]
    assert resolved["template"]["parameters"]["evaluation_start"] == "2026-08-17"
    assert resolved["template"]["parameters"]["evaluation_end"] == "2026-08-21"
    assert resolved["requested"]["dataset"]["start"] == "2026-08-16"
    assert (
        resolved["requested"]["template"]["parameters"]["evaluation_start"]
        == "2026-08-16"
    )

    created = experiments.submit(weekend_task, action_id="weekend")
    duplicate = experiments.submit(session_task, action_id="session")
    detail = experiments.experiment_detail(created["experiment_id"])
    connection = datasets.catalog.connect()
    try:
        row = connection.execute(
            "SELECT identity_json FROM experiments WHERE experiment_id = ?",
            (created["experiment_id"],),
        ).fetchone()
    finally:
        connection.close()
    identity = json.loads(row["identity_json"])
    attempt = experiments.list_attempts(created["experiment_id"])[0]

    assert duplicate["status"] == "DUPLICATE"
    assert duplicate["experiment_id"] == created["experiment_id"]
    assert len(experiments.list_attempts(created["experiment_id"])) == 1
    assert "requested_start" not in identity["dataset"]
    assert "requested_end" not in identity["dataset"]
    assert identity["dataset"]["effective_start"] == "2026-08-17"
    assert identity["dataset"]["effective_end"] == "2026-08-21"
    assert identity["dataset"]["lineage"] == {"kind": "legacy_snapshot"}
    assert attempt["requested"]["dataset"] == {
        "dataset_id": "601328.SS",
        "start": "2026-08-16",
        "end": "2026-08-23",
    }
    assert detail["dataset"]["requested_start"] == "2026-08-16"
    assert detail["dataset"]["requested_end"] == "2026-08-23"
    assert detail["dataset"]["effective_start"] == "2026-08-17"
    assert detail["dataset"]["effective_end"] == "2026-08-21"
    assert detail["template"]["parameters"]["evaluation_start"] == "2026-08-17"


def _yahoo_payload(
    symbol: str = "601328.SS",
    *,
    metadata: dict | None = None,
    timestamps: list[int] | None = None,
    volume: list | None = None,
) -> bytes:
    timestamps = timestamps or [
        int(pd.Timestamp("2026-08-25", tz="UTC").timestamp()),
        int(pd.Timestamp("2026-08-26", tz="UTC").timestamp()),
    ]
    yahoo_metadata = {
        "symbol": symbol,
        "dataGranularity": "1d",
        "currency": "CNY",
        "exchangeName": "SHH",
        "instrumentType": "EQUITY",
        "exchangeTimezoneName": "Asia/Shanghai",
        "gmtoffset": 28800,
    }
    if metadata:
        yahoo_metadata.update(metadata)
    return json.dumps(
        {
            "chart": {
                "error": None,
                "result": [
                    {
                        "meta": yahoo_metadata,
                        "timestamp": timestamps,
                        "indicators": {
                            "quote": [
                                {
                                    "open": [6.1, 6.2],
                                    "high": [6.2, 6.3],
                                    "low": [6.0, 6.1],
                                    "close": [6.15, 6.25],
                                    "volume": volume or [1000, 1100],
                                }
                            ],
                            "adjclose": [{"adjclose": [6.05, 6.15]}],
                        },
                    }
                ],
            }
        },
        separators=(",", ":"),
    ).encode()


SSE_CANONICAL_URL = requests.Request(
    "GET", SSE_CURRENT_ENDPOINT, params=SSE_CURRENT_PARAMS
).prepare().url


class FakeResponse:
    def __init__(
        self,
        payload: bytes,
        *,
        content_length: str | None = None,
        chunks: list[bytes] | None = None,
        url: str | None = SSE_CANONICAL_URL,
        status_code: int = 200,
        history: list[object] | None = None,
    ):
        self.payload = payload
        self.url = url
        self.status_code = status_code
        self.history = [] if history is None else history
        self.headers = {"content-type": "application/json"}
        if content_length is not None:
            self.headers["content-length"] = content_length
        self.chunks = chunks
        self.iterated = False
        self.closed = False

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, chunk_size: int):
        self.iterated = True
        yield from self.chunks if self.chunks is not None else [self.payload]

    def close(self) -> None:
        self.closed = True

    @property
    def content(self):
        raise AssertionError("Yahoo responses must be consumed as bounded streams")


def test_yahoo_source_reuses_canonical_endpoint_and_binds_exact_response():
    calls = []
    payload = _yahoo_payload()

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return FakeResponse(payload)

    result = YahooChartSource(http_get=get).fetch(
        "601328.SS", "2026-08-25", "2026-08-26"
    )

    assert calls[0][0].startswith(
        "https://query1.finance.yahoo.com/v8/finance/chart/601328.SS?"
    )
    assert calls[0][1]["timeout"] == 30
    assert calls[0][1]["stream"] is True
    assert result.bars["Date"].dt.strftime("%Y-%m-%d").tolist() == [
        "2026-08-25",
        "2026-08-26",
    ]
    assert list(result.bars) == [
        "Date",
        "Open",
        "High",
        "Low",
        "Close",
        "Volume",
        "AdjustedClose",
    ]
    assert result.source_identity["response_sha256"] == hashlib.sha256(
        payload
    ).hexdigest()
    assert result.source_identity["provider"] == "yahoo-chart-api"


def test_yahoo_latest_close_excludes_the_current_unfinished_xshg_session():
    payload = _yahoo_payload()
    source = YahooChartSource(
        http_get=lambda *args, **kwargs: FakeResponse(payload),
        clock=lambda: datetime(2026, 8, 26, 6, 0, tzinfo=UTC),
    )

    assert source.latest_available_close("601328.SS") == "2026-08-25"


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (_yahoo_payload("OTHER.SS"), "symbol"),
        (
            _yahoo_payload().replace(
                b'"timestamp":[1787616000,1787702400]',
                b'"timestamp":[1787616000,1787616000]',
            ),
            "duplicate",
        ),
        (
            _yahoo_payload().replace(b'"close":[6.15,6.25]', b'"close":[6.15]'),
            "aligned",
        ),
    ],
)
def test_yahoo_source_rejects_mismatched_or_incoherent_responses(payload, message):
    source = YahooChartSource(http_get=lambda *args, **kwargs: FakeResponse(payload))

    with pytest.raises(DatasetResolutionError, match=message):
        source.fetch("601328.SS", "2026-08-25", "2026-08-26")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("symbol", "OTHER.SS"),
        ("dataGranularity", "1h"),
        ("currency", "USD"),
        ("exchangeName", "NYQ"),
        ("instrumentType", "ETF"),
        ("exchangeTimezoneName", "UTC"),
        ("gmtoffset", 0),
    ],
)
def test_yahoo_source_rejects_mismatched_production_metadata(field, value):
    payload = _yahoo_payload(metadata={field: value})
    source = YahooChartSource(http_get=lambda *args, **kwargs: FakeResponse(payload))

    with pytest.raises(DatasetResolutionError, match=field):
        source.fetch("601328.SS", "2026-08-25", "2026-08-26")


def test_yahoo_source_derives_sessions_in_declared_exchange_timezone():
    timestamps = [
        int(pd.Timestamp("2026-08-24 16:00:00", tz="UTC").timestamp()),
        int(pd.Timestamp("2026-08-25 16:00:00", tz="UTC").timestamp()),
    ]
    payload = _yahoo_payload(timestamps=timestamps)
    source = YahooChartSource(http_get=lambda *args, **kwargs: FakeResponse(payload))

    result = source.fetch("601328.SS", "2026-08-25", "2026-08-26")

    assert result.bars["Date"].dt.strftime("%Y-%m-%d").tolist() == [
        "2026-08-25",
        "2026-08-26",
    ]


@pytest.mark.parametrize(
    "volume",
    [
        [True, 1100],
        [-1, 1100],
        [float("nan"), 1100],
        [1000.5, 1100],
    ],
)
def test_yahoo_source_rejects_non_count_volume_values(volume):
    payload = _yahoo_payload(volume=volume)
    source = YahooChartSource(http_get=lambda *args, **kwargs: FakeResponse(payload))

    with pytest.raises(DatasetResolutionError, match="volume|Volume|non-finite"):
        source.fetch("601328.SS", "2026-08-25", "2026-08-26")


def test_yahoo_source_accepts_integral_numeric_volume_as_float64():
    payload = _yahoo_payload(volume=[1000, 1100.0])
    source = YahooChartSource(http_get=lambda *args, **kwargs: FakeResponse(payload))

    result = source.fetch("601328.SS", "2026-08-25", "2026-08-26")

    assert result.bars["Volume"].tolist() == [1000.0, 1100.0]
    assert result.bars["Volume"].dtype == "float64"


def test_yahoo_source_rejects_oversized_content_length_before_streaming():
    response = FakeResponse(
        _yahoo_payload(),
        content_length=str(MAX_PROVIDER_RESPONSE_BYTES + 1),
    )
    source = YahooChartSource(http_get=lambda *args, **kwargs: response)

    with pytest.raises(DatasetResolutionError, match="size limit"):
        source.fetch("601328.SS", "2026-08-25", "2026-08-26")

    assert response.iterated is False
    assert response.closed is True


def test_yahoo_source_rejects_oversized_stream_and_closes_response():
    response = FakeResponse(
        b"",
        chunks=[b"x" * (1024 * 1024)] * 17,
    )
    source = YahooChartSource(http_get=lambda *args, **kwargs: response)

    with pytest.raises(DatasetResolutionError, match="size limit"):
        source.fetch("601328.SS", "2026-08-25", "2026-08-26")

    assert response.closed is True


@pytest.mark.parametrize("content_length", [None, "invalid"])
def test_yahoo_source_safely_streams_without_valid_content_length(content_length):
    payload = _yahoo_payload()
    response = FakeResponse(payload, content_length=content_length)
    source = YahooChartSource(http_get=lambda *args, **kwargs: response)

    result = source.fetch("601328.SS", "2026-08-25", "2026-08-26")

    assert result.source_identity["response_sha256"] == hashlib.sha256(payload).hexdigest()
    assert response.closed is True


SSE_FIELDS = [
    "code",
    "name",
    "open",
    "high",
    "low",
    "last",
    "prev_close",
    "chg_rate",
    "volume",
    "amount",
    "tradephase",
    "change",
    "amp_rate",
    "cpxxsubtype",
    "cpxxprodusta",
]
SSE_ROW = [
    "601328",
    "交通银行",
    7.14,
    7.28,
    7.13,
    7.24,
    7.10,
    1.97,
    213407934,
    1542217037,
    "E110    ",
    0.14,
    2.11,
    "ASH",
    "   D  F  N          ",
]


def _sse_payload(
    *,
    rows: list | None = None,
    date: object = 20260831,
    time: object = 162903,
    total: object = 1,
) -> bytes:
    return json.dumps(
        {
            "date": date,
            "time": time,
            "total": total,
            "list": [SSE_ROW] if rows is None else rows,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()


def test_sse_current_source_binds_exact_request_bar_and_source_identity():
    calls = []
    payload = _sse_payload()

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return FakeResponse(payload)

    result = SseCurrentDailySource(http_get=get).fetch(
        "601328.SS", "2026-08-31", "2026-08-31"
    )

    request = {
        "method": "GET",
        "url": SSE_CANONICAL_URL,
        "params": {
            "select": ",".join(SSE_FIELDS),
            "begin": 0,
            "end": 5000,
        },
        "headers": {
            "Accept": "application/json",
            "Referer": "https://www.sse.com.cn/market/price/report/",
            "User-Agent": "quant-research-platform/0.1",
        },
    }
    assert calls == [
        (
            SSE_CURRENT_ENDPOINT,
            {
                "params": request["params"],
                "headers": request["headers"],
                "timeout": 30,
                "stream": True,
                "allow_redirects": False,
            },
        )
    ]
    pd.testing.assert_frame_equal(
        result.bars,
        pd.DataFrame(
            {
                "Date": pd.to_datetime(["2026-08-31"]),
                "Open": [7.14],
                "High": [7.28],
                "Low": [7.13],
                "Close": [7.24],
                "Volume": [213407934.0],
            }
        ),
    )
    assert result.source_identity == {
        "provider": "sse-current-ashare-report",
        "instrument": "601328.SS",
        "request": request,
        "response_sha256": "53c6785296d6a11efa36160c5ec99b33e541896e56abb6c6eddb1d2e92c4a759",
        "canonical_content_sha256": "aba8bc12cd51608c28c52674eb7755d9d0392c95355cd5418a19c517bd033d52",
        "response_date": "2026-08-31",
        "response_time": "16:29:03",
        "trading_phase": "E110",
        "contract": "IS120 STEP 0.62",
        "price_report_script_url": (
            "https://www.sse.com.cn/xhtml/home/2021public/querySearch/"
            "search_price_2021.js?v=ssesite_V3.8.0_20260828"
        ),
    }


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (FakeResponse(_sse_payload(), status_code=201), "HTTP 200"),
        (FakeResponse(_sse_payload(), history=[object()]), "redirect"),
        (
            FakeResponse(
                _sse_payload(),
                url="https://example.invalid/v1/sh1/list/exchange/ashare",
            ),
            "effective URL",
        ),
        (FakeResponse(_sse_payload(), url=None), "effective URL"),
        (FakeResponse(_sse_payload(), url="not-a-url"), "effective URL"),
    ],
)
def test_sse_current_source_rejects_unverified_http_response(response, message):
    source = SseCurrentDailySource(http_get=lambda *args, **kwargs: response)

    with pytest.raises(DatasetResolutionError, match=message):
        source.fetch("601328.SS", "2026-08-31", "2026-08-31")

    assert response.closed is True


def test_sse_current_source_latest_close_is_verified_response_date():
    response = FakeResponse(_sse_payload())
    source = SseCurrentDailySource(
        http_get=lambda *args, **kwargs: response,
        clock=lambda: datetime(2026, 9, 1, tzinfo=UTC),
    )

    assert source.latest_available_close("601328.SS") == "2026-08-31"
    assert response.closed is True


@pytest.mark.parametrize(
    ("instrument", "start", "end", "message"),
    [
        ("601328", "2026-08-31", "2026-08-31", "instrument"),
        ("000001.SZ", "2026-08-31", "2026-08-31", "instrument"),
        ("601328.SS", "2026-08-30", "2026-08-30", "range"),
        ("601328.SS", "2026-08-30", "2026-08-31", "range"),
        ("601328.SS", "2026-08-31", "2026-09-01", "range"),
    ],
)
def test_sse_current_source_rejects_noncanonical_instrument_or_range(
    instrument, start, end, message
):
    source = SseCurrentDailySource(
        http_get=lambda *args, **kwargs: FakeResponse(_sse_payload())
    )

    with pytest.raises(DatasetResolutionError, match=message):
        source.fetch(instrument, start, end)


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        ([], "requested code"),
        ([["600000", *SSE_ROW[1:]]], "requested code"),
        ([SSE_ROW, SSE_ROW], "exactly one"),
        ([SSE_ROW[:-1]], "15 fields"),
    ],
)
def test_sse_current_source_requires_one_exact_requested_code_row(rows, message):
    source = SseCurrentDailySource(
        http_get=lambda *args, **kwargs: FakeResponse(_sse_payload(rows=rows))
    )

    with pytest.raises(DatasetResolutionError, match=message):
        source.fetch("601328.SS", "2026-08-31", "2026-08-31")


@pytest.mark.parametrize(
    ("index", "value", "message"),
    [
        (1, "  ", "name"),
        (2, 0, "OHLC"),
        (3, 7.12, "OHLC"),
        (4, 7.25, "OHLC"),
        (5, float("inf"), "non-finite"),
        (8, True, "volume"),
        (8, -1, "volume"),
        (8, 1.5, "volume"),
        (10, "E111    ", "phase"),
        (13, "ASHI", "subtype"),
    ],
)
def test_sse_current_source_rejects_invalid_name_phase_subtype_or_ohlcv(
    index, value, message
):
    row = SSE_ROW.copy()
    row[index] = value
    source = SseCurrentDailySource(
        http_get=lambda *args, **kwargs: FakeResponse(_sse_payload(rows=[row]))
    )

    with pytest.raises(DatasetResolutionError, match=message):
        source.fetch("601328.SS", "2026-08-31", "2026-08-31")


def test_sse_current_source_rejects_volume_not_lossless_in_float64():
    row = SSE_ROW.copy()
    row[8] = 9007199254740993
    source = SseCurrentDailySource(
        http_get=lambda *args, **kwargs: FakeResponse(_sse_payload(rows=[row]))
    )

    with pytest.raises(DatasetResolutionError, match="lossless.*float64"):
        source.fetch("601328.SS", "2026-08-31", "2026-08-31")


def test_sse_current_source_rejects_leading_whitespace_in_trading_phase():
    row = SSE_ROW.copy()
    row[10] = " E110   "
    source = SseCurrentDailySource(
        http_get=lambda *args, **kwargs: FakeResponse(_sse_payload(rows=[row]))
    )

    with pytest.raises(DatasetResolutionError, match="phase"):
        source.fetch("601328.SS", "2026-08-31", "2026-08-31")


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"date": 20260230}, "date"),
        ({"date": "20260831"}, "date"),
        ({"time": 246000}, "time"),
        ({"time": "162903"}, "time"),
        ({"total": -1}, "total"),
        ({"total": True}, "total"),
    ],
)
def test_sse_current_source_rejects_invalid_report_metadata(overrides, message):
    source = SseCurrentDailySource(
        http_get=lambda *args, **kwargs: FakeResponse(_sse_payload(**overrides))
    )

    with pytest.raises(DatasetResolutionError, match=message):
        source.fetch("601328.SS", "2026-08-31", "2026-08-31")


@pytest.mark.parametrize(
    "payload",
    [
        b'{"date":20260831,"date":20260831,"time":162903,"total":0,"list":[]}',
        _sse_payload().replace(b"7.14", b"NaN", 1),
    ],
)
def test_sse_current_source_rejects_duplicate_keys_and_nonfinite_json(payload):
    source = SseCurrentDailySource(http_get=lambda *args, **kwargs: FakeResponse(payload))

    with pytest.raises(DatasetResolutionError, match="duplicate|non-finite"):
        source.fetch("601328.SS", "2026-08-31", "2026-08-31")


def test_sse_current_source_rejects_non_json_and_oversized_responses():
    non_json = FakeResponse(_sse_payload())
    non_json.headers["content-type"] = "text/html"
    oversized = FakeResponse(
        b"", chunks=[b"x" * (1024 * 1024)] * 17
    )

    for response, message in [(non_json, "not JSON"), (oversized, "size limit")]:
        source = SseCurrentDailySource(
            http_get=lambda *args, response=response, **kwargs: response
        )
        with pytest.raises(DatasetResolutionError, match=message):
            source.fetch("601328.SS", "2026-08-31", "2026-08-31")
        assert response.closed is True


def test_sse_current_source_requires_timezone_aware_clock_for_latest_close():
    source = SseCurrentDailySource(
        http_get=lambda *args, **kwargs: FakeResponse(_sse_payload()),
        clock=lambda: datetime(2026, 9, 1),
    )

    with pytest.raises(DatasetResolutionError, match="timezone-aware"):
        source.latest_available_close("601328.SS")


def test_catalog_registration_is_immutable(tmp_path: Path):
    source = FixedSource(_bars(SESSIONS))
    service = _dataset_service(tmp_path, source)

    service.register(ITEM)
    with pytest.raises(DatasetResolutionError, match="immutable.*conflict"):
        service.register(replace(ITEM, name="Renamed dataset"))
