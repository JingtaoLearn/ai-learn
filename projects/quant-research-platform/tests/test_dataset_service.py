from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest

from quant_platform.catalog import initialize_catalog
from quant_platform.dataset_service import (
    DatasetCatalogItem,
    DatasetResolutionError,
    DatasetService,
    FetchedDailyBars,
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


def _yahoo_payload(symbol: str = "601328.SS") -> bytes:
    timestamps = [
        int(pd.Timestamp("2026-08-25", tz="UTC").timestamp()),
        int(pd.Timestamp("2026-08-26", tz="UTC").timestamp()),
    ]
    return json.dumps(
        {
            "chart": {
                "error": None,
                "result": [
                    {
                        "meta": {"symbol": symbol, "dataGranularity": "1d"},
                        "timestamp": timestamps,
                        "indicators": {
                            "quote": [
                                {
                                    "open": [6.1, 6.2],
                                    "high": [6.2, 6.3],
                                    "low": [6.0, 6.1],
                                    "close": [6.15, 6.25],
                                    "volume": [1000, 1100],
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


class FakeResponse:
    def __init__(self, payload: bytes):
        self.content = payload
        self.headers = {"content-type": "application/json"}

    def raise_for_status(self) -> None:
        return None


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


def test_catalog_registration_is_immutable(tmp_path: Path):
    source = FixedSource(_bars(SESSIONS))
    service = _dataset_service(tmp_path, source)

    service.register(ITEM)
    with pytest.raises(DatasetResolutionError, match="immutable.*conflict"):
        service.register(replace(ITEM, name="Renamed dataset"))
