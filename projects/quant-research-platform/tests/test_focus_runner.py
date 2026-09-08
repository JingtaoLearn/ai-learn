from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from gold_research.focus_contract import ContractViolation, Phase
from gold_research.focus_runner import claim_phase, main, verify_implementation
from gold_research.sge_daily_report import EXPECTED_HEADERS


def _contract(path: Path) -> Path:
    document = {
        "schema": "quant-research/sge-au9999-html-envelope-contract/v1",
        "revision": "008",
        "fail_closed_terminal": "INSUFFICIENT_EVIDENCE",
        "request": {
            "host": "en.sge.com.cn",
            "path": "/data/data_daily_international_new",
            "ordered_query": [
                ["start_date", "<D:YYYY-MM-DD>"],
                ["end_date", "<D:YYYY-MM-DD>"],
                ["inst_ids", "Au99.99"],
            ],
        },
        "response": {
            "http_status_exact": 200,
            "content_type_exact": "text/html;charset=UTF-8",
        },
        "html_envelope": {
            "matching_table_count_exact": 1,
            "body_row_count_exact": 1,
            "matching_table_first_row_exact": list(EXPECTED_HEADERS),
        },
        "version_and_chronology_binding": {
            "collection_class": "HISTORICAL_SNAPSHOT_V1",
            "mixing_with_PROSPECTIVE_D10_V1": "PROHIBITED",
        },
    }
    path.write_text(json.dumps(document, separators=(",", ":"), sort_keys=True), encoding="utf-8")
    return path


def test_verify_is_offline_immutable_and_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def reject_network(*args, **kwargs):
        del args, kwargs
        raise AssertionError("network access is forbidden")

    monkeypatch.setattr(socket, "getaddrinfo", reject_network)
    monkeypatch.setattr(socket.socket, "connect", reject_network)
    contract = _contract(tmp_path / "contract.json")
    output = tmp_path / "output"
    first = verify_implementation(contract, output)
    second = verify_implementation(contract, output)
    assert first["status"] == "CREATED"
    assert second["status"] == "NO_CHANGE"
    assert first["evidence_sha256"] == second["evidence_sha256"]
    assert first["network_requests"] == 0
    assert first["calibration_paths_executed"] == 0
    assert first["evaluation_executions"] == 0
    receipt = output / "implementation-verification" / "receipt.json"
    ledger = output / "phase-ledger" / "01-IMPLEMENTATION_VERIFIED.json"
    assert receipt.stat().st_mode & 0o777 == 0o444
    assert receipt.parent.stat().st_mode & 0o777 == 0o555
    assert ledger.stat().st_mode & 0o777 == 0o444


def test_phase_ledger_is_ordered_at_most_once_and_conflicts_fail(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger"
    with pytest.raises(ContractViolation, match="prerequisite"):
        claim_phase(ledger, Phase.IMPLEMENTATION_REVIEWED, "1" * 64)
    assert claim_phase(ledger, Phase.IMPLEMENTATION_VERIFIED, "1" * 64)
    assert not claim_phase(ledger, Phase.IMPLEMENTATION_VERIFIED, "1" * 64)
    with pytest.raises(ContractViolation, match="different evidence"):
        claim_phase(ledger, Phase.IMPLEMENTATION_VERIFIED, "2" * 64)
    assert claim_phase(ledger, Phase.IMPLEMENTATION_REVIEWED, "3" * 64)


def test_phase_ledger_rejects_forged_prerequisite(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger"
    ledger.mkdir()
    forged = ledger / "01-IMPLEMENTATION_VERIFIED.json"
    forged.write_bytes(b"not a phase claim\n")
    forged.chmod(0o444)

    with pytest.raises(ContractViolation):
        claim_phase(ledger, Phase.IMPLEMENTATION_REVIEWED, "3" * 64)
    assert not (ledger / "02-IMPLEMENTATION_REVIEWED.json").exists()


def test_unreviewed_effectful_phases_and_unknown_options_fail_closed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    contract = _contract(tmp_path / "contract.json")
    for command in ("calibrate-once", "collect", "finalize", "evaluate-once"):
        assert main(
            [
                command,
                "--contract",
                str(contract),
                "--output-root",
                str(tmp_path / command),
            ]
        ) == 2
        result = json.loads(capsys.readouterr().out)
        assert result["status"] == "REJECTED"
        assert "not admitted" in result["reason"]
    with pytest.raises(SystemExit) as caught:
        main(
            [
                "verify-implementation",
                "--contract",
                str(contract),
                "--output-root",
                str(tmp_path / "verify"),
                "--threshold",
                "1",
            ]
        )
    assert caught.value.code == 2
