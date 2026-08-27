import json
import hashlib
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from quant_platform.catalog import initialize_catalog
from quant_platform.isolation import build_operator_validation_command
from quant_platform.operator_service import (
    OperatorConflictError,
    OperatorService,
    OperatorSubmissionError,
)
from quant_platform.operator_worker import load_published_operator, validate_candidate
from quant_platform.schemas import canonical_json_bytes


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "operators"
FIXTURE = FIXTURE_ROOT / "custom_fit.py"
IMAGE = "registry.example/research@sha256:" + "a" * 64


def _submission(version: str = "1.0.0", slot: str = "fit") -> dict:
    cases = {
        "fit": {
            "input": {"values": [1.0, 2.0, 4.0]},
            "parameters": {"window": 2},
            "expected": 3.0,
        },
        "smoothing": {
            "input": {"values": [1.0, 2.0, 4.0]},
            "parameters": {"window": 2},
            "expected": [1.0, 2.0, 4.0],
        },
        "statistic": {
            "input": {"values": [1.0, 2.0, 4.0]},
            "parameters": {"window": 2},
            "expected": [None, 1.0, 2.0],
        },
        "decision": {
            "input": {"statistics": [None, 1.0], "initial_position": 0},
            "parameters": {"window": 2},
            "expected": [
                {"action": "HOLD", "reason": "CUSTOM_FIXTURE"},
                {"action": "HOLD", "reason": "CUSTOM_FIXTURE"},
            ],
        },
        "sizing": {
            "input": {
                "cash": 1000.0,
                "raw_price": 5.0,
                "holdings": 0,
                "side": "BUY",
            },
            "parameters": {"window": 2},
            "expected": 100,
        },
        "cost": {
            "input": {"side": "BUY", "raw_price": 5.0, "quantity": 100},
            "parameters": {"window": 2},
            "expected": {
                "commission_cny": 0.0,
                "transfer_fee_cny": 0.0,
                "stamp_tax_cny": 0.0,
                "slippage_cny": 0.0,
                "total_cost_cny": 0.0,
            },
        },
        "report": {
            "input": {"title": "Fixture", "metrics": {"return": 0.0}},
            "parameters": {"window": 2},
            "expected": "<!doctype html><html><body><h1>Fixture</h1></body></html>",
        },
    }
    return {
        "operator_id": f"fixture_{slot}",
        "slot": slot,
        "version": version,
        "source": (FIXTURE_ROOT / f"custom_{slot}.py").read_text(encoding="utf-8"),
        "parameter_schema": {
            "type": "object",
            "properties": {"window": {"type": "integer", "minimum": 1, "maximum": 3}},
            "required": ["window"],
            "additionalProperties": False,
        },
        "defaults": {"window": 2},
        "title_zh": "测试均值拟合",
        "summary_zh": "在隔离容器中计算尾部窗口均值。",
        "documentation": "# Mean fit\n\nUses only the supplied prior values.",
        "tests": [cases[slot]],
    }


def _passing_validator(candidate: Path) -> dict:
    assert (candidate / "operator.py").is_file()
    return validate_candidate(candidate, validator_image=IMAGE)


def _write_candidate(candidate: Path, payload: dict) -> str:
    digest = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    manifest = {
        key: payload[key]
        for key in (
            "operator_id",
            "slot",
            "version",
            "parameter_schema",
            "defaults",
            "title_zh",
            "summary_zh",
            "documentation",
        )
    } | {"content_digest": digest}
    (candidate / "operator.py").write_text(payload["source"], encoding="utf-8")
    (candidate / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (candidate / "tests.json").write_text(
        json.dumps(payload["tests"]), encoding="utf-8"
    )
    (candidate / "documentation.md").write_text(
        payload["documentation"], encoding="utf-8"
    )
    return digest


def test_safe_custom_fit_worker_validates_compile_contract_and_fixture(tmp_path: Path):
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    payload = _submission()
    digest = _write_candidate(candidate, payload)

    evidence = validate_candidate(candidate, validator_image=IMAGE)

    assert evidence["passed"] is True
    assert evidence["slot"] == "fit"
    assert evidence["candidate_digest"] == digest
    assert evidence["validator_image"] == IMAGE
    assert evidence["execution_envelope"]["network"] == "none"
    assert evidence["observations"] == {
        "api_version": 1,
        "compile": True,
        "contract": True,
        "fixtures": 1,
    }


def test_validation_command_reuses_hardened_docker_boundary(tmp_path: Path):
    candidate = tmp_path / "candidate"
    evidence = tmp_path / "evidence"
    candidate.mkdir()
    evidence.mkdir()

    command = build_operator_validation_command(candidate, evidence, IMAGE)

    assert command[:3] == ["docker", "run", "--rm"]
    for pair in (
        ("--pull", "never"),
        ("--network", "none"),
        ("--cpus", "1.0"),
        ("--memory", "512m"),
        ("--pids-limit", "256"),
        ("--cap-drop", "ALL"),
        ("--security-opt", "no-new-privileges:true"),
        ("--user", "1000:1000"),
    ):
        index = command.index(pair[0])
        assert command[index + 1] == pair[1]
    assert "--read-only" in command
    assert f"type=bind,src={candidate.resolve()},dst=/operator,readonly" in command
    assert f"type=bind,src={evidence.resolve()},dst=/evidence" in command
    assert command[-4:] == [
        "-m",
        "quant_platform.operator_worker",
        "validate",
        "/operator",
    ]


def test_submission_publishes_immutable_bundle_and_latest(tmp_path: Path):
    catalog = initialize_catalog(tmp_path / "state")
    service = OperatorService(catalog, validator=_passing_validator, runner_image=IMAGE)

    result = service.submit(_submission())
    detail = service.detail("fixture_fit", "1.0.0")

    assert result["status"] == "CREATED"
    assert detail["content_digest"] == result["content_digest"]
    assert service.detail("fixture_fit")["version"] == "1.0.0"
    bundle = catalog.state_root / detail["bundle_path"]
    assert stat.S_IMODE(bundle.stat().st_mode) == 0o555
    assert {path.name for path in bundle.iterdir()} == {
        "documentation.md",
        "evidence.json",
        "manifest.json",
        "operator.py",
        "tests.json",
    }
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o444 for path in bundle.iterdir())


def test_same_identity_and_content_is_no_change_but_changed_content_conflicts(tmp_path: Path):
    service = OperatorService(
        initialize_catalog(tmp_path / "state"),
        validator=_passing_validator,
        runner_image=IMAGE,
    )
    first = service.submit(_submission())
    second = service.submit(_submission())

    assert first["content_digest"] == second["content_digest"]
    assert second["status"] == "NO_CHANGE"

    changed = _submission()
    changed["documentation"] += "\nChanged."
    with pytest.raises(OperatorConflictError, match="different content"):
        service.submit(changed)


def test_concurrent_identical_submission_converges(tmp_path: Path):
    service = OperatorService(
        initialize_catalog(tmp_path / "state"),
        validator=_passing_validator,
        runner_image=IMAGE,
    )

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(service.submit, [_submission()] * 4))

    assert [result["status"] for result in results].count("CREATED") == 1
    assert [result["status"] for result in results].count("NO_CHANGE") == 3
    assert len({result["content_digest"] for result in results}) == 1


def test_higher_semantic_version_becomes_latest_and_history_remains_addressable(tmp_path: Path):
    service = OperatorService(
        initialize_catalog(tmp_path / "state"),
        validator=_passing_validator,
        runner_image=IMAGE,
    )
    service.submit(_submission("1.9.0"))
    service.submit(_submission("1.10.0"))

    assert service.detail("fixture_fit")["version"] == "1.10.0"
    assert service.detail("fixture_fit", "1.9.0")["version"] == "1.9.0"
    versions = service.list_versions("fixture_fit")
    assert [item["version"] for item in versions] == ["1.10.0", "1.9.0"]


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value.update(operator_id="../escape"), "operator_id"),
        (lambda value: value.update(version="1"), "semantic version"),
        (lambda value: value.update(source=""), "source"),
        (lambda value: value.update(source="import os\n"), "validation"),
        (lambda value: value.update(extra=True), "unknown"),
        (lambda value: value["defaults"].update(extra=1), "defaults"),
        (lambda value: value.update(tests=[]), "tests"),
    ],
)
def test_invalid_submissions_fail_closed_without_catalog_entry(tmp_path: Path, mutation, message):
    catalog = initialize_catalog(tmp_path / "state")

    def worker(candidate: Path) -> dict:
        if "import os" in (candidate / "operator.py").read_text(encoding="utf-8"):
            raise OperatorSubmissionError("validation rejected forbidden import")
        return _passing_validator(candidate)

    service = OperatorService(catalog, validator=worker, runner_image=IMAGE)
    payload = _submission()
    mutation(payload)

    with pytest.raises((OperatorSubmissionError, ValueError), match=message):
        service.submit(payload)
    with pytest.raises(ValueError, match="unknown"):
        service.detail("fixture_fit")


def test_worker_rejects_forbidden_import_and_wrong_contract(tmp_path: Path):
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    payload = _submission()
    payload["source"] = "import os\nOPERATOR_API_VERSION=1\nSLOT='fit'\ndef apply(v,p): return 1\n"
    _write_candidate(candidate, payload)

    with pytest.raises(ValueError, match="import"):
        validate_candidate(candidate, validator_image=IMAGE)


@pytest.mark.parametrize(
    "slot",
    ["fit", "smoothing", "statistic", "decision", "sizing", "cost", "report"],
)
def test_all_seven_custom_slot_contracts_publish_with_runner_owned_evidence(
    tmp_path: Path, slot: str
):
    catalog = initialize_catalog(tmp_path / slot)
    service = OperatorService(
        catalog, validator=_passing_validator, runner_image=IMAGE
    )

    result = service.submit(_submission(slot=slot))
    detail = service.detail(f"fixture_{slot}", "1.0.0")

    assert result["status"] == "CREATED"
    evidence = detail["validation_evidence"]
    assert evidence["slot"] == slot
    assert evidence["candidate_digest"] == result["content_digest"]
    assert evidence["validator_image"] == IMAGE
    assert evidence["fixture_digest"]


def test_submission_rejects_unbound_or_submitter_supplied_evidence(tmp_path: Path):
    catalog = initialize_catalog(tmp_path / "state")
    payload = _submission()
    payload["validation_evidence"] = {"passed": True}
    service = OperatorService(
        catalog, validator=_passing_validator, runner_image=IMAGE
    )
    with pytest.raises(OperatorSubmissionError, match="unknown"):
        service.submit(payload)

    def unbound(candidate: Path) -> dict:
        evidence = _passing_validator(candidate)
        return evidence | {"candidate_digest": "0" * 64}

    with pytest.raises(OperatorSubmissionError, match="evidence"):
        OperatorService(
            catalog, validator=unbound, runner_image=IMAGE
        ).submit(_submission())


def test_runtime_loader_requires_catalog_bound_content_and_evidence_digests(
    tmp_path: Path,
):
    catalog = initialize_catalog(tmp_path / "state")
    service = OperatorService(
        catalog, validator=_passing_validator, runner_image=IMAGE
    )
    service.submit(_submission())
    detail = service.detail("fixture_fit", "1.0.0")
    bundle = catalog.state_root / detail["bundle_path"]

    with pytest.raises(ValueError, match="resolved content digest"):
        load_published_operator(
            bundle,
            expected_content_digest="0" * 64,
            expected_evidence_digest=hashlib.sha256(
                canonical_json_bytes(detail["validation_evidence"])
            ).hexdigest(),
        )
    with pytest.raises(ValueError, match="resolved evidence digest"):
        load_published_operator(
            bundle,
            expected_content_digest=detail["content_digest"],
            expected_evidence_digest="0" * 64,
        )
