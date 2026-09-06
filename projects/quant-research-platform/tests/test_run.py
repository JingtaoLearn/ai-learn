import hashlib

import pandas as pd
import pytest

from gold_research.run import (
    ProvenanceError,
    _canonical_source_identity,
    _data_hash,
    _mlflow_payloads,
    _redacted_tracking_uri,
    _release_source_capture,
    _source_tree_hash,
    _validate_source_provenance,
    stable_run_id,
    run_research,
)


def _write_minimal_pyproject(project):
    (project / "pyproject.toml").write_text(
        '[project]\nname = "gold-quant-research"\nversion = "0.1.0"\n'
        'requires-python = ">=3.12,<3.14"\ndependencies = []\n'
    )


def test_run_id_is_stable_and_sensitive_to_inputs():
    a = stable_run_id({"cost_bps": 5}, "abc", "deadbeef")
    assert a == stable_run_id({"cost_bps": 5}, "abc", "deadbeef")
    assert a != stable_run_id({"cost_bps": 10}, "abc", "deadbeef")


def test_data_hash_covers_all_present_canonical_input_columns():
    idx = pd.bdate_range("2024-01-01", periods=3)
    left = {"GLD": pd.DataFrame({"Close": [100.0, 101.0, 102.0], "Volume": [1, 2, 3]}, index=idx)}
    right = {"GLD": pd.DataFrame({"Close": [100.0, 101.0, 102.0], "Volume": [1, 9, 3]}, index=idx)}
    assert _data_hash(left) != _data_hash(right)


def test_data_hash_distinguishes_every_behaviorally_relevant_float64_value():
    idx = pd.bdate_range("2024-01-01", periods=2)
    left = {"ABC": pd.DataFrame({"Open": [100.0, 101.0], "Close": [100.0, 101.0]}, index=idx)}
    right = {
        "ABC": pd.DataFrame(
            {"Open": [100.0, 101.0], "Close": [100.000000001, 101.0]},
            index=idx,
        )
    }
    assert _data_hash(left) != _data_hash(right)


def test_source_tree_hash_changes_with_effective_source(tmp_path):
    (tmp_path / "src").mkdir()
    source = tmp_path / "src" / "strategy.py"
    source.write_text("VALUE = 1\n")
    first = _source_tree_hash(tmp_path)
    cache = tmp_path / "src" / "__pycache__"
    cache.mkdir()
    (cache / "strategy.pyc").write_bytes(b"runtime-cache")
    assert first == _source_tree_hash(tmp_path)
    source.write_text("VALUE = 2\n")
    assert first != _source_tree_hash(tmp_path)


def test_canonical_source_identity_uses_path_nul_bytes_nul_framing():
    files = {"src/gold_research/z.py": b"z\n", "src/gold_research/a.py": b"a\n"}
    digest = hashlib.sha256()
    for name in sorted(files, key=lambda item: item.encode("utf-8")):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(files[name])
        digest.update(b"\0")

    assert _canonical_source_identity(files) == {
        "schema": "gold-first-party-runtime-v1",
        "algorithm": "sha256",
        "sha256": digest.hexdigest(),
        "file_count": 2,
    }


@pytest.mark.parametrize(
    "value",
    [
        {"mode": "unknown"},
        {"mode": "package", "source_root": "/tmp/release"},
        {"mode": "release", "source_root": "/tmp/release"},
        {
            "mode": "release",
            "source_root": "/tmp/release",
            "expected_source_sha256": "A" * 64,
        },
    ],
)
def test_source_provenance_is_one_exact_tagged_authority(value):
    with pytest.raises(ProvenanceError, match="PROVENANCE_AUTHORITY_AMBIGUOUS"):
        _validate_source_provenance(value)


def test_release_capture_is_route_independent_and_rejects_content_changes(tmp_path):
    project = tmp_path / "release"
    (project / "src" / "gold_research").mkdir(parents=True)
    (project / "src" / "quant_platform").mkdir(parents=True)
    (project / "src" / "gold_research" / "__init__.py").write_bytes(b"VALUE = 1\n")
    (project / "src" / "quant_platform" / "__init__.py").write_bytes(b"")
    (project / "src" / "gold_research" / "ignored.pyc").write_bytes(b"cache")
    _write_minimal_pyproject(project)
    files = {
        "src/gold_research/__init__.py": b"VALUE = 1\n",
        "src/quant_platform/__init__.py": b"",
    }
    expected = _canonical_source_identity(files)["sha256"]
    provenance = {
        "mode": "release",
        "source_root": str(project),
        "expected_source_sha256": expected,
    }

    capture = _release_source_capture(_validate_source_provenance(provenance))
    assert capture.files == files
    assert capture.source_identity["sha256"] == expected
    assert capture.observation["mode"] == "release"

    (project / "src" / "gold_research" / "__init__.py").write_bytes(b"VALUE = 2\n")
    with pytest.raises(ProvenanceError, match="SOURCE_CONTENT_MISMATCH"):
        _release_source_capture(_validate_source_provenance(provenance))


def test_release_capture_rejects_unsafe_roots_and_members(tmp_path):
    relative = {
        "mode": "release",
        "source_root": "relative",
        "expected_source_sha256": "0" * 64,
    }
    with pytest.raises(ProvenanceError, match="SOURCE_ROOT_INVALID"):
        _release_source_capture(_validate_source_provenance(relative))

    project = tmp_path / "release"
    (project / "src" / "gold_research").mkdir(parents=True)
    (project / "src" / "quant_platform").mkdir(parents=True)
    target = project / "target.py"
    target.write_text("VALUE = 1\n")
    (project / "src" / "gold_research" / "linked.py").symlink_to(target)
    (project / "src" / "quant_platform" / "__init__.py").write_text("")
    unsafe = dict(relative, source_root=str(project))
    with pytest.raises(ProvenanceError, match="SOURCE_SYMLINK_REJECTED"):
        _release_source_capture(_validate_source_provenance(unsafe))


def test_release_capture_revalidation_detects_replacement(tmp_path):
    project = tmp_path / "release"
    (project / "src" / "gold_research").mkdir(parents=True)
    (project / "src" / "quant_platform").mkdir(parents=True)
    source = project / "src" / "gold_research" / "__init__.py"
    source.write_text("VALUE = 1\n")
    (project / "src" / "quant_platform" / "__init__.py").write_text("")
    _write_minimal_pyproject(project)
    files = {
        "src/gold_research/__init__.py": b"VALUE = 1\n",
        "src/quant_platform/__init__.py": b"",
    }
    provenance = {
        "mode": "release",
        "source_root": str(project),
        "expected_source_sha256": _canonical_source_identity(files)["sha256"],
    }
    capture = _release_source_capture(_validate_source_provenance(provenance))
    source.write_text("VALUE = 2\n")

    with pytest.raises(ProvenanceError, match="SOURCE_CHANGED_DURING_RUN"):
        capture.revalidate()


def test_tracking_uri_credentials_and_query_are_redacted():
    value = _redacted_tracking_uri("https://user:secret@example.com:5000/path?token=private")
    assert value == "https://example.com:5000/path"


def test_mlflow_payload_aggregates_full_oos_and_stress_metrics():
    rows = [
        {"symbol": "GLD", "strategy": "sma_50_200", "scenario": "base", "segment": "full", "cagr": 0.1, "sharpe": 1.0},
        {"symbol": "GLD", "strategy": "sma_50_200", "scenario": "base", "segment": "out_of_sample", "cagr": 0.2, "sharpe": 1.5},
        {"symbol": "GLD", "strategy": "sma_50_200", "scenario": "cost_stress", "segment": "full", "cagr": 0.08, "sharpe": 0.8},
        {"symbol": "GLD", "strategy": "donchian_50_20", "scenario": "parameter_stability", "segment": "full", "cagr": 0.07, "sharpe": 0.7},
    ]
    payload = _mlflow_payloads(rows)[("GLD", "sma_50_200")]
    assert payload == {"full_cagr": 0.1, "full_sharpe": 1.0, "oos_cagr": 0.2, "oos_sharpe": 1.5, "stress_cagr": 0.08, "stress_sharpe": 0.8}


def test_complete_synthetic_run_emits_required_artifacts(tmp_path):
    idx = pd.bdate_range("2018-01-01", periods=700)
    prices = 100 + pd.Series(range(700), index=idx) * 0.05
    data = {"GLD": pd.DataFrame({"Open": prices, "Close": prices}, index=idx)}
    result = run_research(data, tmp_path, cost_bps=7, tracking_uri=None)
    run_dir = tmp_path / result["run_id"]
    for name in ["config.json", "run_manifest.json", "metrics.json", "metrics.csv", "equity.csv", "trades.csv", "report.html"]:
        assert (run_dir / name).exists(), name
    assert {"research", "out_of_sample", "cost_stress", "parameter_stability"} <= set(result["validations"])
    report = (run_dir / "report.html").read_text()
    assert "7 bps one-way" in report
    assert "14 bps one-way" in report
    with pytest.raises(FileExistsError):
        run_research(data, tmp_path, cost_bps=7, tracking_uri=None)
