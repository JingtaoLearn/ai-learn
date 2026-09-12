from __future__ import annotations

import os
from pathlib import Path

import pytest

from quant_platform.bocom_admission import (
    INTERVAL,
    PARENT_SNAPSHOT_ID,
    build_bocom_action_snapshot,
    load_bocom_admission_package,
)
from quant_platform.corporate_actions import accounting_cash_dividends


SOURCE_PACKAGE = os.environ.get("QUANT_BOCOM_SOURCE_PACKAGE")
PARENT_SNAPSHOT = os.environ.get("QUANT_BOCOM_PARENT_SNAPSHOT")


@pytest.mark.skipif(SOURCE_PACKAGE is None, reason="exact BOCOM source package is not mounted")
def test_exact_source_package_adapts_to_two_action_complete_evidence():
    assert SOURCE_PACKAGE is not None
    package = load_bocom_admission_package(Path(SOURCE_PACKAGE))

    actions = accounting_cash_dividends(package.evidence)
    assert [str(action.gross_cash_per_share) for action in actions] == ["0.1563", "0.1684"]
    assert [(action.record_date.isoformat(), action.pay_date.isoformat()) for action in actions] == [
        ("2025-12-24", "2025-12-25"),
        ("2026-07-09", "2026-07-10"),
    ]
    coverage = package.evidence.document["coverage"]["payload"]
    assert (coverage["interval_start"], coverage["interval_end"]) == INTERVAL
    assert coverage["coverage_state"] == "VERIFIED_COMPLETE_INTERVAL"
    assert package.evidence.document["source_package"] == package.package_binding


@pytest.mark.skipif(
    SOURCE_PACKAGE is None or PARENT_SNAPSHOT is None,
    reason="exact BOCOM package and parent Snapshot are not mounted",
)
def test_exact_parent_produces_schema4_action_aware_snapshot(tmp_path: Path):
    assert SOURCE_PACKAGE is not None
    assert PARENT_SNAPSHOT is not None
    package = load_bocom_admission_package(Path(SOURCE_PACKAGE))

    result = build_bocom_action_snapshot(Path(PARENT_SNAPSHOT), tmp_path, package)

    assert result["parent_snapshot_id"] == PARENT_SNAPSHOT_ID
    assert result["schema_version"] == 4
    assert result["event_count"] == 2
    assert result["data_start"] == "2019-05-06"
    assert result["data_end"] == INTERVAL[1]
