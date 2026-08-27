import json
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
from quant_platform.operator_worker import validate_candidate


FIXTURE = Path(__file__).parent / "fixtures" / "operators" / "custom_fit.py"
IMAGE = "registry.example/research@sha256:" + "a" * 64


def _submission(version: str = "1.0.0") -> dict:
    return {
        "operator_id": "fixture_mean_fit",
        "slot": "fit",
        "version": version,
        "source": FIXTURE.read_text(encoding="utf-8"),
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
        "tests": [
            {
                "values": [1.0, 2.0, 4.0],
                "parameters": {"window": 2},
                "expected": 3.0,
            }
        ],
    }


def _passing_validator(candidate: Path) -> dict:
    assert (candidate / "operator.py").is_file()
    return {
        "passed": True,
        "compile": True,
        "contract": True,
        "fixtures": 1,
        "worker": "test",
    }


def test_safe_custom_fit_worker_validates_compile_contract_and_fixture(tmp_path: Path):
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    payload = _submission()
    (candidate / "operator.py").write_text(payload["source"], encoding="utf-8")
    (candidate / "manifest.json").write_text(
        json.dumps(
            {
                key: payload[key]
                for key in (
                    "operator_id",
                    "slot",
                    "version",
                    "parameter_schema",
                    "defaults",
                )
            }
        ),
        encoding="utf-8",
    )
    (candidate / "tests.json").write_text(json.dumps(payload["tests"]), encoding="utf-8")

    evidence = validate_candidate(candidate)

    assert evidence == {
        "api_version": 1,
        "compile": True,
        "contract": True,
        "fixtures": 1,
        "passed": True,
        "slot": "fit",
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
    service = OperatorService(catalog, validator=_passing_validator)

    result = service.submit(_submission())
    detail = service.detail("fixture_mean_fit", "1.0.0")

    assert result["status"] == "CREATED"
    assert detail["content_digest"] == result["content_digest"]
    assert service.detail("fixture_mean_fit")["version"] == "1.0.0"
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
    service = OperatorService(initialize_catalog(tmp_path / "state"), validator=_passing_validator)
    first = service.submit(_submission())
    second = service.submit(_submission())

    assert first["content_digest"] == second["content_digest"]
    assert second["status"] == "NO_CHANGE"

    changed = _submission()
    changed["documentation"] += "\nChanged."
    with pytest.raises(OperatorConflictError, match="different content"):
        service.submit(changed)


def test_concurrent_identical_submission_converges(tmp_path: Path):
    service = OperatorService(initialize_catalog(tmp_path / "state"), validator=_passing_validator)

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(service.submit, [_submission()] * 4))

    assert [result["status"] for result in results].count("CREATED") == 1
    assert [result["status"] for result in results].count("NO_CHANGE") == 3
    assert len({result["content_digest"] for result in results}) == 1


def test_higher_semantic_version_becomes_latest_and_history_remains_addressable(tmp_path: Path):
    service = OperatorService(initialize_catalog(tmp_path / "state"), validator=_passing_validator)
    service.submit(_submission("1.9.0"))
    service.submit(_submission("1.10.0"))

    assert service.detail("fixture_mean_fit")["version"] == "1.10.0"
    assert service.detail("fixture_mean_fit", "1.9.0")["version"] == "1.9.0"
    versions = service.list_versions("fixture_mean_fit")
    assert [item["version"] for item in versions] == ["1.10.0", "1.9.0"]


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value.update(slot="decision"), "unsupported"),
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

    service = OperatorService(catalog, validator=worker)
    payload = _submission()
    mutation(payload)

    with pytest.raises((OperatorSubmissionError, ValueError), match=message):
        service.submit(payload)
    with pytest.raises(ValueError, match="unknown"):
        service.detail("fixture_mean_fit")


def test_worker_rejects_forbidden_import_and_wrong_contract(tmp_path: Path):
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    payload = _submission()
    payload["source"] = "import os\nOPERATOR_API_VERSION=1\nSLOT='fit'\ndef apply(v,p): return 1\n"
    (candidate / "operator.py").write_text(payload["source"], encoding="utf-8")
    (candidate / "manifest.json").write_text(
        json.dumps(
            {
                key: payload[key]
                for key in (
                    "operator_id",
                    "slot",
                    "version",
                    "parameter_schema",
                    "defaults",
                )
            }
        ),
        encoding="utf-8",
    )
    (candidate / "tests.json").write_text(json.dumps(payload["tests"]), encoding="utf-8")

    with pytest.raises(ValueError, match="import"):
        validate_candidate(candidate)
