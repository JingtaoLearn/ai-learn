import base64
import contextlib
import csv
import hashlib
import importlib.metadata
import importlib.util
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import types
from pathlib import Path
from typing import Mapping

import pandas as pd
import pytest

from gold_research import _round4_bootstrap as bootstrap
from gold_research import run as run_module
from gold_research.round4 import _run_with_resource_audit_suspended
from gold_research.run import (
    ProvenanceError,
    _canonical_source_identity,
    _enumerate_release_files,
    _package_source_capture,
    _release_source_capture,
    _validate_member_name,
    _validate_source_provenance,
)
from gold_research.round4 import run_round4_research


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _record_digest(data: bytes) -> str:
    encoded = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
    return f"sha256={encoded}"


def _make_matching_package_and_release(tmp_path: Path):
    installed = tmp_path / "installed"
    release = tmp_path / "release"
    payloads = {
        "gold_research/__init__.py": b"VALUE = 1\n",
        "gold_research/_round4_bootstrap.py": b"BOOTSTRAP = True\n",
        "quant_platform/__init__.py": b"",
    }
    for relative, data in payloads.items():
        package_path = installed / relative
        package_path.parent.mkdir(parents=True, exist_ok=True)
        package_path.write_bytes(data)
        release_path = release / "src" / relative
        release_path.parent.mkdir(parents=True, exist_ok=True)
        release_path.write_bytes(data)
    pyproject = (
        '[project]\nname = "gold-quant-research"\nversion = "0.1.0"\n'
        'requires-python = ">=3.12,<3.14"\ndependencies = []\n'
    )
    (release / "pyproject.toml").write_text(pyproject)
    dist_info = installed / "gold_quant_research-0.1.0.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: gold-quant-research\nVersion: 0.1.0\n"
        "Requires-Python: <3.14,>=3.12\n"
    )
    rows = [
        (relative, _record_digest(data), str(len(data))) for relative, data in payloads.items()
    ]
    rows.append(("gold_quant_research-0.1.0.dist-info/RECORD", "", ""))
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="\n").writerows(rows)
    (dist_info / "RECORD").write_text(buffer.getvalue())
    return importlib.metadata.PathDistribution(dist_info), release, payloads


def test_package_and_release_routes_compute_the_same_payload_identity(tmp_path, monkeypatch):
    distribution, release, payloads = _make_matching_package_and_release(tmp_path)
    monkeypatch.setattr(importlib.metadata, "distributions", lambda: iter([distribution]))
    package = _package_source_capture(
        _validate_source_provenance(
            {"mode": "package", "distribution": "gold-quant-research"}
        )
    )
    expected = _canonical_source_identity(
        {f"src/{relative}": data for relative, data in payloads.items()}
    )
    source = _release_source_capture(
        _validate_source_provenance(
            {
                "mode": "release",
                "source_root": str(release),
                "expected_source_sha256": str(expected["sha256"]),
            }
        )
    )
    assert package.source_identity == source.source_identity == expected
    assert package.root_requirement_contract == source.root_requirement_contract
    assert package.observation["git"]["commit"] is None


def test_package_record_mismatch_fails_closed(tmp_path, monkeypatch):
    distribution, _, _ = _make_matching_package_and_release(tmp_path)
    monkeypatch.setattr(importlib.metadata, "distributions", lambda: iter([distribution]))
    (tmp_path / "installed" / "gold_research" / "__init__.py").write_bytes(b"changed\n")
    with pytest.raises(ProvenanceError, match="SOURCE_CONTENT_MISMATCH"):
        _package_source_capture(
            _validate_source_provenance(
                {"mode": "package", "distribution": "gold-quant-research"}
            )
        )


def test_package_extra_file_not_in_record_fails_closed(tmp_path, monkeypatch):
    distribution, _, _ = _make_matching_package_and_release(tmp_path)
    monkeypatch.setattr(importlib.metadata, "distributions", lambda: iter([distribution]))
    (tmp_path / "installed" / "gold_research" / "extra.py").write_text("EXTRA = True\n")
    with pytest.raises(ProvenanceError, match="SOURCE_SET_MISMATCH"):
        _package_source_capture(
            _validate_source_provenance(
                {"mode": "package", "distribution": "gold-quant-research"}
            )
        )


def test_package_unhashed_runtime_member_fails_closed(tmp_path, monkeypatch):
    distribution, _, _ = _make_matching_package_and_release(tmp_path)
    monkeypatch.setattr(importlib.metadata, "distributions", lambda: iter([distribution]))
    record = tmp_path / "installed" / "gold_quant_research-0.1.0.dist-info" / "RECORD"
    rows = list(csv.reader(io.StringIO(record.read_text())))
    rows[0][1:] = ["", ""]
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="\n").writerows(rows)
    record.write_text(buffer.getvalue())
    with pytest.raises(ProvenanceError, match="SOURCE_SET_MISMATCH"):
        _package_source_capture(
            _validate_source_provenance(
                {"mode": "package", "distribution": "gold-quant-research"}
            )
        )


def test_duplicate_package_authority_fails_closed(tmp_path, monkeypatch):
    distribution, _, _ = _make_matching_package_and_release(tmp_path)
    monkeypatch.setattr(
        importlib.metadata,
        "distributions",
        lambda: iter([distribution, distribution]),
    )
    with pytest.raises(ProvenanceError, match="PROVENANCE_AUTHORITY_AMBIGUOUS"):
        _package_source_capture(
            _validate_source_provenance(
                {"mode": "package", "distribution": "gold-quant-research"}
            )
        )


@pytest.mark.parametrize("name", ["/absolute.py", "../escape.py", "a/./b.py", "a\\b.py", "a//b.py"])
def test_canonical_member_names_reject_aliases_and_traversal(name):
    with pytest.raises(ProvenanceError, match="SOURCE_ROOT_INVALID"):
        _validate_member_name(name)


def test_memory_loader_executes_captured_bytes_not_replaced_disk_bytes(tmp_path):
    disk = tmp_path / "probe.py"
    disk.write_text("VALUE = 'A'\n")
    captured = {"src/gold_research/probe.py": b"VALUE = 'B'\n"}
    disk.write_text("VALUE = 'C'\n")
    finder = bootstrap._MemoryFinder(captured)
    spec = finder.find_spec("gold_research.probe")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.VALUE == "B"
    assert module.__provenance_member__ == "src/gold_research/probe.py"


def test_memory_loader_rejects_sourceless_first_party_module():
    finder = bootstrap._MemoryFinder({"src/gold_research/__init__.py": b""})
    with pytest.raises(bootstrap.BootstrapError, match="SOURCE_SET_MISMATCH"):
        finder.find_spec("gold_research.only_pyc")


def test_expected_git_identity_fails_when_release_has_no_owned_repository(tmp_path):
    _, release, payloads = _make_matching_package_and_release(tmp_path)
    expected = _canonical_source_identity(
        {f"src/{relative}": data for relative, data in payloads.items()}
    )
    provenance = _validate_source_provenance(
        {
            "mode": "release",
            "source_root": str(release),
            "expected_source_sha256": str(expected["sha256"]),
            "expected_git_commit": "0" * 40,
        }
    )
    with pytest.raises(ProvenanceError, match="GIT_IDENTITY_MISMATCH"):
        _release_source_capture(provenance)


def test_release_git_identity_verifies_owned_project_tree(tmp_path):
    _, release, payloads = _make_matching_package_and_release(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=release, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=release, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=release, check=True)
    subprocess.run(["git", "add", "."], cwd=release, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=release, check=True)
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=release, check=True, text=True, capture_output=True
    ).stdout.strip()
    tree = subprocess.run(
        ["git", "rev-parse", "HEAD^{tree}"],
        cwd=release,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    expected = _canonical_source_identity(
        {f"src/{relative}": data for relative, data in payloads.items()}
    )
    capture = _release_source_capture(
        _validate_source_provenance(
            {
                "mode": "release",
                "source_root": str(release),
                "expected_source_sha256": str(expected["sha256"]),
                "expected_git_commit": commit,
                "expected_project_tree_oid": tree,
            }
        )
    )
    git_observation = capture.observation["git"]
    assert isinstance(git_observation, dict)
    assert git_observation["commit"] == commit
    assert git_observation["project_tree_oid"] == tree


def test_native_change_after_seal_fails_closed(monkeypatch):
    sealed = {
        "dependency_identity": {"sha256": "dependency"},
        "runtime_identity": {"sha256": "runtime"},
        "process_identity": {"sha256": "process"},
        "native_identity": {"sha256": "native-a"},
        "loaded_module_identity": {"sha256": "modules"},
        "render_identity": {"sha256": "render"},
    }
    current = {**sealed, "native_identity": {"sha256": "native-b"}}
    monkeypatch.setattr(run_module, "seal_execution_identity", lambda context: current)
    with pytest.raises(ProvenanceError, match="NATIVE_IDENTITY_INVALID"):
        run_module.revalidate_execution_identity({}, sealed)


def test_process_identity_rejects_unknown_inherited_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/untrusted")
    with pytest.raises(bootstrap.BootstrapError, match="EXECUTION_RESOURCE_UNBOUND"):
        bootstrap._process_identity(tmp_path)


@pytest.mark.parametrize(
    "member",
    ["pkg/../escape.py", "pkg/./alias.py", "pkg//alias.py", "pkg\\alias.py", "/pkg/a.py"],
)
def test_dependency_record_rejects_noncanonical_member_aliases(tmp_path, member):
    installed = tmp_path / "installed"
    dist_info = installed / "sample-1.0.dist-info"
    dist_info.mkdir(parents=True)
    (dist_info / "METADATA").write_text("Name: sample\nVersion: 1.0\n")
    (dist_info / "RECORD").write_text(f"{member},sha256=unused,1\n")
    distribution = importlib.metadata.PathDistribution(dist_info)

    with pytest.raises(bootstrap.BootstrapError, match="DEPENDENCY_IDENTITY_INVALID"):
        bootstrap._record_rows(distribution)


def _dependency_distribution(installed: Path, name: str, shared: Path):
    dist_info = installed / f"{name}-1.0.dist-info"
    dist_info.mkdir()
    metadata = dist_info / "METADATA"
    metadata.write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n")
    record = dist_info / "RECORD"
    record.write_text(
        f"shared.py,{_record_digest(shared.read_bytes())},{shared.stat().st_size}\n"
        f"{dist_info.name}/METADATA,{_record_digest(metadata.read_bytes())},"
        f"{metadata.stat().st_size}\n"
        f"{dist_info.name}/RECORD,,\n"
    )
    return importlib.metadata.PathDistribution(dist_info)


def test_dependency_closure_records_complete_sorted_owner_set(tmp_path, monkeypatch):
    installed = tmp_path / "installed"
    installed.mkdir()
    monkeypatch.setattr(sys, "prefix", str(installed))
    shared = installed / "shared.py"
    shared.write_bytes(b"VALUE = 1\n")
    distributions = [
        _dependency_distribution(installed, name, shared) for name in ("alpha", "beta")
    ]
    monkeypatch.setattr(
        bootstrap,
        "_distribution_map",
        lambda roots: {"alpha": [distributions[0]], "beta": [distributions[1]]},
    )
    monkeypatch.setattr(
        bootstrap,
        "_root_fields",
        lambda provenance, source_root, distribution_map: (
            "gold-quant-research",
            "0.1.0",
            ">=3.11",
            ["alpha==1.0", "beta==1.0"],
        ),
    )

    environment = bootstrap._dependency_environment(
        {"mode": "release"}, tmp_path, (installed.resolve(),)
    )
    payload = next(
        item
        for item in environment["dependency_identity"]["payload_ownership"]
        if item["environment_member"] == "shared.py"
    )
    assert list(payload) == [
        "environment_member",
        "observed_sha256",
        "observed_size",
        "owners",
    ]
    assert [owner["distribution_name"] for owner in payload["owners"]] == ["alpha", "beta"]
    assert all(
        set(owner)
        == {
            "distribution_name",
            "distribution_version",
            "record_hash_algorithm",
            "record_hash_digest",
            "record_member",
            "record_size",
        }
        for owner in payload["owners"]
    )


def test_inactive_installed_claimant_of_active_payload_fails_closed(tmp_path, monkeypatch):
    installed = tmp_path / "installed"
    installed.mkdir()
    shared = installed / "shared.py"
    shared.write_bytes(b"VALUE = 1\n")
    active = _dependency_distribution(installed, "alpha", shared)
    _dependency_distribution(installed, "inactive", shared)
    monkeypatch.setattr(
        bootstrap,
        "_distribution_map",
        lambda roots: {"alpha": [active]},
    )
    monkeypatch.setattr(
        bootstrap,
        "_root_fields",
        lambda provenance, source_root, distribution_map: (
            "gold-quant-research",
            "0.1.0",
            ">=3.11",
            ["alpha==1.0"],
        ),
    )

    with pytest.raises(bootstrap.BootstrapError, match="claimant set differs"):
        bootstrap._dependency_environment(
            {"mode": "release"}, tmp_path, (installed.resolve(),)
        )


def test_record_target_accepts_only_exact_canonical_leading_parent_spelling(tmp_path):
    environment = tmp_path / "venv"
    site = environment / "lib" / "python3.12" / "site-packages"
    site.mkdir(parents=True)
    member, target = bootstrap._canonical_record_target(site, environment, "../../../bin/tool")
    assert member == "bin/tool"
    assert target == environment / "bin" / "tool"

    with pytest.raises(bootstrap.BootstrapError, match="invalid RECORD path"):
        bootstrap._canonical_record_target(site, environment, "../../python3.12/../bin/tool")


def test_installed_inventory_rejects_duplicate_normalized_distribution_names(tmp_path):
    installed = tmp_path / "installed"
    installed.mkdir()
    shared = installed / "shared.py"
    shared.write_bytes(b"VALUE = 1\n")
    _dependency_distribution(installed, "alpha_one", shared)
    _dependency_distribution(installed, "alpha-one", shared)

    with pytest.raises(bootstrap.BootstrapError, match="duplicate installed distribution"):
        bootstrap._installed_inventory((installed.resolve(),))


def test_installed_inventory_rejects_inactive_claimant_alias(tmp_path):
    installed = tmp_path / "installed"
    installed.mkdir()
    shared = installed / "shared.py"
    shared.write_bytes(b"VALUE = 1\n")
    distribution = _dependency_distribution(installed, "inactive", shared)
    record = Path(distribution._path) / "RECORD"
    record.write_text(record.read_text().replace("shared.py,", "pkg/../shared.py,"))

    with pytest.raises(bootstrap.BootstrapError, match="invalid RECORD path"):
        bootstrap._installed_inventory((installed.resolve(),))


def test_canonical_owner_example_matches_accepted_sha256():
    owner = {
        "distribution_name": "mlflow",
        "distribution_version": "3.15.1",
        "record_hash_algorithm": "sha256",
        "record_hash_digest": "C7l2ZTMk-ADFqlBI4OQSVKa8ywZtxJIfYFUHj1KiXlY",
        "record_member": "mlflow/__init__.py",
        "record_size": 14048,
    }
    assert hashlib.sha256(bootstrap._canonical_json(owner)).hexdigest() == (
        "8fdecd3736c92fe26e1c426fede88c354702d16bf186346ebe2b0ec552aa43b2"
    )


def test_preloaded_first_party_module_is_rejected_not_removed(monkeypatch):
    contaminated = types.ModuleType("gold_research.contaminated")
    monkeypatch.setitem(sys.modules, "gold_research.contaminated", contaminated)

    with pytest.raises(bootstrap.BootstrapError, match="LOADED_CODE_UNBOUND"):
        bootstrap._install_import_policy({"files": {}, "allowed_files": {}, "site_roots": []})
    assert sys.modules["gold_research.contaminated"] is contaminated


def test_arbitrary_pathless_module_is_not_a_bootstrap_module(monkeypatch):
    rogue = types.ModuleType("rogue_pathless")
    rogue.__spec__ = None
    monkeypatch.setattr(sys, "modules", {"rogue_pathless": rogue})
    context = {
        "allowed_files": {},
        "site_roots": [],
        "files": {},
        "bootstrap_pathless_modules": frozenset(),
        "runtime_identity": {"executable": {"sha256": "0" * 64, "size": 1}},
    }

    with pytest.raises(ProvenanceError, match="UNVERIFIED_LOADED_MODULE"):
        run_module._loaded_module_identity(context)


def test_final_revalidation_recaptures_dependency_runtime_and_process(monkeypatch):
    environment = {
        "root_requirement_contract": {"name": "gold"},
        "dependency_identity": {"sha256": "dependency-a"},
        "runtime_identity": {"sha256": "runtime-a"},
        "process_identity": {"sha256": "process-a"},
    }
    context = {
        **environment,
        "source_identity": {"sha256": "source"},
        "recapture_environment": lambda: dict(environment),
    }
    monkeypatch.setattr(run_module, "_loaded_module_identity", lambda value: {"sha256": "modules"})
    monkeypatch.setattr(run_module, "_native_identity", lambda value: {"sha256": "native"})
    monkeypatch.setattr(run_module, "_render_identity", lambda value: {"sha256": "render"})
    sealed = run_module.seal_execution_identity(context)

    environment["process_identity"] = {"sha256": "process-b"}
    with pytest.raises(ProvenanceError, match="RUNTIME_CHANGED_DURING_RUN"):
        run_module.revalidate_execution_identity(context, sealed)

    environment["process_identity"] = {"sha256": "process-a"}
    environment["dependency_identity"] = {"sha256": "dependency-b"}
    with pytest.raises(ProvenanceError, match="DEPENDENCY_CHANGED_DURING_RUN"):
        run_module.revalidate_execution_identity(context, sealed)


@pytest.mark.parametrize(
    ("member", "code"),
    [
        ("dependency_identity", "DEPENDENCY_CHANGED_DURING_RUN"),
        ("runtime_identity", "RUNTIME_CHANGED_DURING_RUN"),
        ("process_identity", "RUNTIME_CHANGED_DURING_RUN"),
    ],
)
def test_initial_seal_rejects_bootstrap_to_current_environment_drift(
    monkeypatch, member, code
):
    bootstrap_environment = {
        "root_requirement_contract": {"name": "gold"},
        "dependency_identity": {"sha256": "dependency-a"},
        "runtime_identity": {"sha256": "runtime-a"},
        "process_identity": {"sha256": "process-a"},
    }
    current_environment = dict(bootstrap_environment)
    current_environment[member] = {"sha256": f"{member}-b"}
    context = {
        **bootstrap_environment,
        "source_identity": {"sha256": "source"},
        "recapture_environment": lambda: current_environment,
    }
    monkeypatch.setattr(run_module, "_loaded_module_identity", lambda value: {"sha256": "modules"})
    monkeypatch.setattr(run_module, "_native_identity", lambda value: {"sha256": "native"})
    monkeypatch.setattr(run_module, "_render_identity", lambda value: {"sha256": "render"})

    with pytest.raises(ProvenanceError, match=code):
        run_module.seal_execution_identity(context)


def test_resource_tracker_rejects_external_and_post_seal_resources(tmp_path):
    private_root = tmp_path / "private"
    private_root.mkdir()
    first = tmp_path / "matplotlib" / "fonts" / "first.ttf"
    second = tmp_path / "matplotlib" / "fonts" / "second.ttf"
    external_font = tmp_path / "system" / "external.ttf"
    external_generic = tmp_path / "system" / "external.json"
    post_seal_generic = tmp_path / "system" / "after.dat"
    for path in (first, second, external_font, external_generic, post_seal_generic):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(path.name.encode())
    allowed = {
        str(first): {
            "environment_member": "fonts/first.ttf",
            "observed_sha256": "a",
            "observed_size": 9,
            "owners": [{"distribution_name": "matplotlib"}],
        },
        str(second): {
            "environment_member": "fonts/second.ttf",
            "observed_sha256": "b",
            "observed_size": 10,
            "owners": [{"distribution_name": "matplotlib"}],
        },
    }
    tracker = bootstrap._ResourceTracker(allowed, (tmp_path,), private_root)
    output_root = tmp_path / "output"
    output_root.mkdir()
    staged_output = output_root / "staged.json"
    staged_output.write_text("private output")
    tracker.authorize_output_root(output_root)
    tracker("open", (str(first), "rb", 0))
    tracker("open", (str(staged_output), "rb", 0))
    with pytest.raises(bootstrap.BootstrapError, match="EXECUTION_RESOURCE_UNBOUND"):
        tracker("open", (str(external_generic), "rb", 0))
    tracker.seal()

    with pytest.raises(bootstrap.BootstrapError, match="new resource opened after seal"):
        tracker("open", (str(second), "rb", 0))
    with pytest.raises(bootstrap.BootstrapError, match="resource is not RECORD-bound"):
        tracker("open", (str(external_font), "rb", 0))
    with pytest.raises(bootstrap.BootstrapError, match="EXECUTION_RESOURCE_UNBOUND"):
        tracker("open", (str(post_seal_generic), "rb", 0))


def test_provenance_metadata_operations_suspend_only_the_resource_audit():
    class Tracker:
        active = False

        @contextlib.contextmanager
        def suspended(self):
            self.active = True
            try:
                yield
            finally:
                self.active = False

    tracker = Tracker()

    def operation():
        assert tracker.active
        return "captured"

    assert (
        _run_with_resource_audit_suspended({"resource_tracker": tracker}, operation)
        == "captured"
    )
    assert not tracker.active


def _release_fixture(tmp_path: Path, *, anchor: str | None = None, injection: str = ""):
    release = tmp_path / "release"
    shutil.copytree(PROJECT_ROOT / "src", release / "src", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy2(PROJECT_ROOT / "pyproject.toml", release / "pyproject.toml")
    if anchor is not None:
        round4_path = release / "src" / "gold_research" / "round4.py"
        source = round4_path.read_text()
        assert source.count(anchor) == 1
        round4_path.write_text(source.replace(anchor, injection, 1))
    files, _, _ = _enumerate_release_files(release)
    provenance = {
        "mode": "release",
        "source_root": str(release),
        "expected_source_sha256": str(_canonical_source_identity(files)["sha256"]),
    }
    return release, provenance


def _exact_worker_data() -> dict[str, pd.DataFrame]:
    index = pd.bdate_range("2021-01-04", periods=1200)
    prices = pd.Series([100.0 + index_value * 0.02 for index_value in range(1200)], index=index)
    return {
        "GC=F": pd.DataFrame({"Open": prices * 0.999, "Close": prices}, index=index),
        "GLD": pd.DataFrame({"Open": prices * 1.001, "Close": prices * 1.002}, index=index),
    }


def _assert_exact_worker_failure(
    tmp_path: Path, provenance: Mapping[str, object], expected_code: str
) -> None:
    output_root = tmp_path / "published"
    with pytest.raises(ProvenanceError, match=expected_code):
        run_round4_research(
            _exact_worker_data(),
            output_root,
            analysis_date="2025-08-08",
            source_provenance=dict(provenance),
        )
    assert not output_root.exists() or not any(output_root.iterdir())


@pytest.mark.parametrize("phase", ["before-seal", "after-seal"])
def test_exact_worker_rejects_generic_external_resource_with_zero_publication(tmp_path, phase):
    external = tmp_path / "external" / ("before.json" if phase == "before-seal" else "after.dat")
    external.parent.mkdir()
    external.write_text("unbound")
    seal = "    execution_identity = seal_execution_identity(_provenance_context)\n"
    external_read = f"    Path({str(external)!r}).read_bytes()\n"
    injection = external_read + seal if phase == "before-seal" else seal + external_read
    _, provenance = _release_fixture(tmp_path, anchor=seal, injection=injection)

    _assert_exact_worker_failure(tmp_path, provenance, "EXECUTION_RESOURCE_UNBOUND")


def test_exact_worker_rejects_post_capture_source_replacement_with_zero_publication(tmp_path):
    release = tmp_path / "release"
    target = release / "src" / "gold_research" / "__init__.py"
    anchor = (
        "    if dict(source_capture.source_identity) != _provenance_context[\"source_identity\"]:\n"
        "        raise RuntimeError(\"SOURCE_CHANGED_DURING_RUN: worker source differs from bootstrap capture\")\n"
    )
    injection = anchor + f"    Path({str(target)!r}).write_bytes(b\"# replaced after capture\\n\")\n"
    _, provenance = _release_fixture(tmp_path, anchor=anchor, injection=injection)

    _assert_exact_worker_failure(tmp_path, provenance, "SOURCE_CHANGED_DURING_RUN")


def test_exact_worker_rejects_sourceless_first_party_bytecode_with_zero_publication(tmp_path):
    release, _ = _release_fixture(tmp_path)
    source = release / "src" / "gold_research" / "strategies.py"
    source.unlink()
    cache = source.parent / "__pycache__"
    cache.mkdir()
    (cache / "strategies.cpython-312.pyc").write_bytes(b"not executable source")
    files, _, _ = _enumerate_release_files(release)
    provenance = {
        "mode": "release",
        "source_root": str(release),
        "expected_source_sha256": str(_canonical_source_identity(files)["sha256"]),
    }

    _assert_exact_worker_failure(tmp_path, provenance, "SOURCE_SET_MISMATCH")


def test_exact_worker_rejects_changed_direct_pin_with_zero_publication(tmp_path):
    release, _ = _release_fixture(tmp_path)
    pyproject = release / "pyproject.toml"
    text = pyproject.read_text()
    assert "pandas==2.3.1" in text
    pyproject.write_text(text.replace("pandas==2.3.1", "pandas==0.0.1", 1))
    files, _, _ = _enumerate_release_files(release)
    provenance = {
        "mode": "release",
        "source_root": str(release),
        "expected_source_sha256": str(_canonical_source_identity(files)["sha256"]),
    }

    _assert_exact_worker_failure(tmp_path, provenance, "DEPENDENCY_IDENTITY_INVALID")


def test_exact_worker_rejects_undeclared_optional_import_with_zero_publication(tmp_path):
    packaging_spec = importlib.util.find_spec("packaging")
    assert packaging_spec is not None and packaging_spec.origin is not None
    rogue = Path(packaging_spec.origin).resolve().parent.parent / "qr_undeclared_optional.py"
    assert not rogue.exists()
    rogue.write_text("VALUE = 'unverified'\n")
    anchor = "    costs = _validate_costs(cost_grid_bps)\n"
    injection = "    import qr_undeclared_optional\n" + anchor
    try:
        _, provenance = _release_fixture(tmp_path, anchor=anchor, injection=injection)
        _assert_exact_worker_failure(tmp_path, provenance, "UNVERIFIED_LOADED_MODULE")
    finally:
        rogue.unlink(missing_ok=True)


def _coherent_record_replacement(
    distribution: importlib.metadata.PathDistribution, payload_path: Path
) -> tuple[bytes, bytes, bytes, bytes]:
    record_path = Path(str(distribution._path)) / "RECORD"
    original_payload = payload_path.read_bytes()
    original_record = record_path.read_bytes()
    changed_payload = original_payload + b"\n# coherent provenance replacement\n"
    rows = list(csv.reader(io.StringIO(original_record.decode("utf-8"))))
    matching = [
        row
        for row in rows
        if len(row) == 3 and Path(str(distribution.locate_file(row[0]))).resolve() == payload_path
    ]
    assert len(matching) == 1
    matching[0][1] = _record_digest(changed_payload)
    matching[0][2] = str(len(changed_payload))
    output = io.StringIO()
    csv.writer(output, lineterminator="\n").writerows(rows)
    return original_payload, original_record, changed_payload, output.getvalue().encode("utf-8")


def test_exact_worker_rejects_executed_a_but_coherently_attested_b_with_zero_publication(tmp_path):
    distribution = importlib.metadata.distribution("packaging")
    assert isinstance(distribution, importlib.metadata.PathDistribution)
    packaging_spec = importlib.util.find_spec("packaging")
    assert packaging_spec is not None and packaging_spec.origin is not None
    payload_path = Path(packaging_spec.origin).resolve()
    record_path = Path(str(distribution._path)) / "RECORD"
    original_payload, original_record, changed_payload, changed_record = _coherent_record_replacement(
        distribution, payload_path
    )
    seal = "    execution_identity = seal_execution_identity(_provenance_context)\n"
    injection = (
        f"    Path({str(payload_path)!r}).write_bytes(base64.b64decode({base64.b64encode(changed_payload)!r}))\n"
        f"    Path({str(record_path)!r}).write_bytes(base64.b64decode({base64.b64encode(changed_record)!r}))\n"
        + seal
    )
    try:
        _, provenance = _release_fixture(tmp_path, anchor=seal, injection=injection)
        _assert_exact_worker_failure(tmp_path, provenance, "DEPENDENCY_CHANGED_DURING_RUN")
    finally:
        payload_path.write_bytes(original_payload)
        record_path.write_bytes(original_record)


@pytest.mark.parametrize("drift", ["payload-stat", "record-stat"])
def test_exact_worker_rejects_post_seal_dependency_stat_drift_with_zero_publication(
    tmp_path, drift
):
    distribution = importlib.metadata.distribution("packaging")
    assert isinstance(distribution, importlib.metadata.PathDistribution)
    packaging_spec = importlib.util.find_spec("packaging")
    assert packaging_spec is not None and packaging_spec.origin is not None
    payload_path = Path(packaging_spec.origin).resolve()
    record_path = Path(str(distribution._path)) / "RECORD"
    target = payload_path if drift == "payload-stat" else record_path
    original = target.read_bytes()
    original_mode = target.stat().st_mode
    replacement = target.with_name(target.name + ".provenance-replacement")
    seal = "    execution_identity = seal_execution_identity(_provenance_context)\n"
    injection = (
        seal
        + "    import os as _provenance_os\n"
        + f"    _provenance_replacement = Path({str(replacement)!r})\n"
        + f"    _provenance_replacement.write_bytes(base64.b64decode({base64.b64encode(original)!r}))\n"
        + f"    _provenance_os.replace(_provenance_replacement, Path({str(target)!r}))\n"
    )
    try:
        _, provenance = _release_fixture(tmp_path, anchor=seal, injection=injection)
        _assert_exact_worker_failure(tmp_path, provenance, "DEPENDENCY_CHANGED_DURING_RUN")
    finally:
        replacement.unlink(missing_ok=True)
        target.write_bytes(original)
        target.chmod(original_mode)


def test_exact_worker_executes_authority_b_and_ignores_stale_a_bytecode(tmp_path):
    release, _ = _release_fixture(tmp_path)
    init_path = release / "src" / "gold_research" / "__init__.py"
    init_path.write_bytes(init_path.read_bytes() + b"\nAUTHORITY_MARKER = 'B'\n")
    cache = init_path.parent / "__pycache__"
    cache.mkdir()
    (cache / "__init__.cpython-312.pyc").write_bytes(b"stale authority A bytecode")
    files, _, _ = _enumerate_release_files(release)
    expected_source = _canonical_source_identity(files)
    provenance = {
        "mode": "release",
        "source_root": str(release),
        "expected_source_sha256": str(expected_source["sha256"]),
    }
    output_root = tmp_path / "published"

    result = run_round4_research(
        _exact_worker_data(),
        output_root,
        analysis_date="2025-08-08",
        source_provenance=provenance,
    )

    manifest = json.loads((output_root / result["run_id"] / "run_manifest.json").read_text())
    assert manifest["source_identity"] == expected_source
    assert len(list(output_root.iterdir())) == 1


@pytest.mark.parametrize(
    "kind",
    [
        "missing",
        "directory",
        "target-symlink",
        "intermediate-symlink",
        "root-symlink",
        "hardlink",
        "fifo",
        "socket",
        "device",
    ],
)
def test_secure_environment_read_rejects_complete_non_regular_and_link_matrix(tmp_path, kind):
    environment = tmp_path / "environment"
    environment.mkdir()
    member = "payload"
    target = environment / member
    open_socket = None
    if kind == "missing":
        pass
    elif kind == "directory":
        target.mkdir()
    elif kind == "target-symlink":
        real = environment / "real"
        real.write_bytes(b"payload")
        target.symlink_to(real)
    elif kind == "intermediate-symlink":
        real = environment / "real"
        real.mkdir()
        (real / "payload").write_bytes(b"payload")
        (environment / "alias").symlink_to(real, target_is_directory=True)
        member = "alias/payload"
    elif kind == "root-symlink":
        real = tmp_path / "real-environment"
        real.mkdir()
        (real / member).write_bytes(b"payload")
        linked = tmp_path / "linked-environment"
        linked.symlink_to(real, target_is_directory=True)
        environment = linked
    elif kind == "hardlink":
        real = environment / "real"
        real.write_bytes(b"payload")
        os.link(real, target)
    elif kind == "fifo":
        os.mkfifo(target)
    elif kind == "socket":
        open_socket = socket.socket(socket.AF_UNIX)
        open_socket.bind(str(target))
    else:
        environment = Path("/")
        member = "dev/null"
    try:
        with pytest.raises(bootstrap.BootstrapError, match="DEPENDENCY_IDENTITY_INVALID"):
            bootstrap._secure_environment_read(environment, member)
    finally:
        if open_socket is not None:
            open_socket.close()
