from __future__ import annotations

import base64
import csv
import hashlib
import io
import importlib.util
import importlib.metadata
import json
import math
import os
import pickle
import re
import shutil
import stat
import subprocess
import sys
import sysconfig
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Mapping
from urllib.parse import urlsplit, urlunsplit

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

from .backtest import backtest, metrics, trade_ledger
from .strategies import donchian_signal, strategy_signals


def canonical_hash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def stable_run_id(config: dict, data_hash: str, git_state: str) -> str:
    return canonical_hash({"config": config, "data_hash": data_hash, "git_state": git_state})[:16]


def _source_tree_hash(root: Path | str = Path(".")) -> str:
    root = Path(root).resolve()
    candidates = ["src", "tests", "scripts"]
    explicit = [
        ".dockerignore",
        "compose.yaml",
        "Dockerfile",
        "Makefile",
        "pyproject.toml",
        "requirements.in",
        "requirements.lock",
    ]
    files = []
    ignored_parts = {"__pycache__", ".pytest_cache", ".ruff_cache"}
    for name in candidates:
        for path in (root / name).rglob("*"):
            is_runtime_cache = bool(ignored_parts.intersection(path.parts))
            is_bytecode = path.suffix in {".pyc", ".pyo"}
            if path.is_file() and not is_runtime_cache and not is_bytecode:
                files.append(path)
    files.extend(path for name in explicit if (path := root / name).is_file())
    files = sorted(set(files), key=lambda path: str(path.relative_to(root)))
    if not files:
        raise RuntimeError(f"no effective source files found under {root}")
    digest = hashlib.sha256()
    for path in files:
        relative = str(path.relative_to(root))
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def get_git_state(root: Path | str = Path(".")) -> dict:
    root = Path(root).resolve()

    def command(*args):
        try:
            result = subprocess.run(["git", *args], cwd=root, text=True, capture_output=True, check=False)
        except FileNotFoundError:
            return None
        return result.stdout.strip() if result.returncode == 0 else None

    commit = command("rev-parse", "HEAD")
    branch = command("branch", "--show-current")
    status = command("status", "--porcelain")
    return {
        "commit": commit or "unavailable",
        "branch": branch or "unavailable",
        "dirty": bool(status) if status is not None else None,
        "source_hash": _source_tree_hash(root),
        "provenance_mode": "git+source-tree" if commit else "source-tree",
    }


class ProvenanceError(RuntimeError):
    """A fail-closed source or execution-provenance rejection."""

    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True)
class SourceCapture:
    """Immutable bytes and observations selected by one provenance authority."""

    provenance: Mapping[str, object]
    files: Mapping[str, bytes]
    source_identity: Mapping[str, object]
    observation: Mapping[str, object]
    root_requirement_contract: Mapping[str, object]
    paths: Mapping[str, Path]
    stats: Mapping[str, tuple[int, int, int, int]]

    def revalidate(self) -> "SourceCapture":
        try:
            current = (
                _release_source_capture(self.provenance)
                if self.provenance["mode"] == "release"
                else _package_source_capture(self.provenance)
            )
        except ProvenanceError as exc:
            raise ProvenanceError("SOURCE_CHANGED_DURING_RUN", exc.detail) from exc
        if (
            dict(current.files) != dict(self.files)
            or dict(current.stats) != dict(self.stats)
            or current.source_identity != self.source_identity
        ):
            raise ProvenanceError(
                "SOURCE_CHANGED_DURING_RUN", "selected source changed after preflight capture"
            )
        return current


_CANONICAL_ROOTS = ("src/gold_research", "src/quant_platform")
_SOURCE_SCHEMA = "gold-first-party-runtime-v1"
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_HEX40 = re.compile(r"[0-9a-fA-F]{40}\Z")


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _validate_member_name(name: str) -> PurePosixPath:
    if not isinstance(name, str) or not name or "\\" in name or "\0" in name:
        raise ProvenanceError("SOURCE_ROOT_INVALID", f"invalid canonical member name: {name!r}")
    if name.startswith("/") or "//" in name:
        raise ProvenanceError("SOURCE_ROOT_INVALID", f"invalid canonical member name: {name!r}")
    parts = name.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ProvenanceError("SOURCE_ROOT_INVALID", f"invalid canonical member name: {name!r}")
    return PurePosixPath(*parts)


def _canonical_source_identity(files: Mapping[str, bytes]) -> dict[str, object]:
    if not files:
        raise ProvenanceError("SOURCE_SET_EMPTY", "canonical first-party payload is empty")
    digest = hashlib.sha256()
    for name in sorted(files, key=lambda item: item.encode("utf-8")):
        _validate_member_name(name)
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(files[name])
        digest.update(b"\0")
    return {
        "schema": _SOURCE_SCHEMA,
        "algorithm": "sha256",
        "sha256": digest.hexdigest(),
        "file_count": len(files),
    }


def _validate_source_provenance(value: Mapping[str, object] | None) -> Mapping[str, object]:
    if value is None:
        return MappingProxyType({"mode": "package", "distribution": "gold-quant-research"})
    if not isinstance(value, Mapping):
        raise ProvenanceError("PROVENANCE_AUTHORITY_AMBIGUOUS", "source_provenance must be a mapping")
    supplied = dict(value)
    mode = supplied.get("mode")
    if mode == "package":
        if set(supplied) != {"mode", "distribution"} or supplied.get("distribution") != "gold-quant-research":
            raise ProvenanceError(
                "PROVENANCE_AUTHORITY_AMBIGUOUS",
                "package authority requires exactly mode and distribution=gold-quant-research",
            )
    elif mode == "release":
        required = {"mode", "source_root", "expected_source_sha256"}
        optional = {"expected_git_commit", "expected_project_tree_oid"}
        if not required <= set(supplied) or not set(supplied) <= required | optional:
            raise ProvenanceError(
                "PROVENANCE_AUTHORITY_AMBIGUOUS", "release authority fields are incomplete or mixed"
            )
        if not isinstance(supplied["source_root"], str):
            raise ProvenanceError("SOURCE_ROOT_INVALID", "release source_root must be a string")
        if not isinstance(supplied["expected_source_sha256"], str) or not _HEX64.fullmatch(
            supplied["expected_source_sha256"]
        ):
            raise ProvenanceError(
                "PROVENANCE_AUTHORITY_AMBIGUOUS",
                "expected_source_sha256 must be 64 lowercase hexadecimal characters",
            )
        for field in optional:
            if field in supplied and (
                not isinstance(supplied[field], str) or not _HEX40.fullmatch(supplied[field])
            ):
                raise ProvenanceError("GIT_IDENTITY_MISMATCH", f"invalid {field}")
    else:
        raise ProvenanceError("PROVENANCE_AUTHORITY_AMBIGUOUS", f"unknown authority mode: {mode!r}")
    return MappingProxyType(supplied)


def _assert_no_symlink_path(path: Path, *, code: str = "SOURCE_SYMLINK_REJECTED") -> None:
    absolute = path if path.is_absolute() else path.absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError as exc:
            raise ProvenanceError("SOURCE_ROOT_INVALID", f"missing path: {current}") from exc
        if stat.S_ISLNK(mode):
            raise ProvenanceError(code, f"symlink is not permitted: {current}")


def _safe_read(path: Path) -> tuple[bytes, tuple[int, int, int, int]]:
    _assert_no_symlink_path(path)
    before = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode):
        raise ProvenanceError("SOURCE_SET_MISMATCH", f"not a regular file: {path}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        data = bytearray()
        while chunk := os.read(descriptor, 1024 * 1024):
            data.extend(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    opened_identity = (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
    after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if identity != opened_identity or identity != after_identity or len(data) != before.st_size:
        raise ProvenanceError("SOURCE_CHANGED_DURING_RUN", f"file changed while reading: {path}")
    return bytes(data), identity


def _release_root(value: str) -> Path:
    if "\0" in value or "\\" in value or not value.startswith("/"):
        raise ProvenanceError("SOURCE_ROOT_INVALID", "source_root must be an absolute POSIX path")
    if "//" in value or any(part in {".", ".."} for part in value.split("/")[1:]):
        raise ProvenanceError("SOURCE_ROOT_INVALID", "source_root contains a lexical alias")
    root = Path(value)
    _assert_no_symlink_path(root)
    try:
        resolved = root.resolve(strict=True)
    except OSError as exc:
        raise ProvenanceError("SOURCE_ROOT_INVALID", f"source_root cannot be resolved: {root}") from exc
    if resolved != root:
        raise ProvenanceError("SOURCE_ROOT_INVALID", "resolved source_root differs from lexical root")
    if not root.is_dir():
        raise ProvenanceError("SOURCE_ROOT_INVALID", "source_root is not a directory")
    return root


def _enumerate_release_files(
    root: Path,
) -> tuple[dict[str, bytes], dict[str, Path], dict[str, tuple[int, int, int, int]]]:
    files: dict[str, bytes] = {}
    paths: dict[str, Path] = {}
    identities: dict[str, tuple[int, int, int, int]] = {}
    for root_name in _CANONICAL_ROOTS:
        package_root = root / PurePosixPath(root_name)
        _assert_no_symlink_path(package_root)
        if not package_root.is_dir():
            raise ProvenanceError("SOURCE_SET_MISMATCH", f"missing canonical root: {root_name}")
        for path in package_root.rglob("*"):
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                raise ProvenanceError("SOURCE_SYMLINK_REJECTED", f"symlinked member: {relative}")
            if path.is_dir():
                if path.name == "__pycache__":
                    continue
                _assert_no_symlink_path(path)
                continue
            if "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
                continue
            _validate_member_name(relative)
            if relative in files:
                raise ProvenanceError("SOURCE_SET_MISMATCH", f"duplicate member: {relative}")
            data, identity = _safe_read(path)
            files[relative] = data
            paths[relative] = path
            identities[relative] = identity
    return files, paths, identities


def _normalize_project_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _canonical_requirement_contract(
    name: str, version: str, requires_python: str, requirements: list[str]
) -> dict[str, object]:
    try:
        from packaging.markers import default_environment
        from packaging.requirements import Requirement
        from packaging.specifiers import SpecifierSet

        environment = default_environment()
        active: list[tuple[str, str]] = []
        for raw in requirements:
            requirement = Requirement(raw)
            marker = requirement.marker
            if marker is not None and not marker.evaluate({**environment, "extra": ""}):
                continue
            active.append((_normalize_project_name(requirement.name), str(requirement)))
    except Exception as exc:
        raise ProvenanceError("DEPENDENCY_IDENTITY_INVALID", "invalid root requirement") from exc
    return {
        "name": _normalize_project_name(name),
        "version": version,
        "requires_python": str(SpecifierSet(requires_python)),
        "requires_dist": [canonical for _, canonical in sorted(active)],
    }


def _root_requirement_contract_from_pyproject(root: Path) -> dict[str, object]:
    import tomllib

    try:
        project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
        return _canonical_requirement_contract(
            project["name"],
            str(project["version"]),
            project["requires-python"],
            [str(value) for value in project.get("dependencies", [])],
        )
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise ProvenanceError("DEPENDENCY_IDENTITY_INVALID", "invalid release pyproject metadata") from exc


def _root_requirement_contract_from_distribution(
    distribution: importlib.metadata.Distribution,
) -> dict[str, object]:
    metadata = distribution.metadata
    try:
        return _canonical_requirement_contract(
            metadata["Name"],
            distribution.version,
            metadata["Requires-Python"],
            list(metadata.get_all("Requires-Dist") or []),
        )
    except (KeyError, TypeError) as exc:
        raise ProvenanceError("PACKAGE_METADATA_UNAVAILABLE", "incomplete package METADATA") from exc


def _unique_distribution(name: str) -> importlib.metadata.Distribution:
    normalized = _normalize_project_name(name)
    matches = [
        distribution
        for distribution in importlib.metadata.distributions()
        if _normalize_project_name(distribution.metadata.get("Name", "")) == normalized
    ]
    if len(matches) != 1:
        code = "PACKAGE_METADATA_UNAVAILABLE" if not matches else "PROVENANCE_AUTHORITY_AMBIGUOUS"
        raise ProvenanceError(code, f"expected one {name} distribution, found {len(matches)}")
    return matches[0]


def _record_rows(distribution: importlib.metadata.Distribution) -> list[tuple[str, str, str]]:
    text = distribution.read_text("RECORD")
    if text is None:
        raise ProvenanceError("PACKAGE_METADATA_UNAVAILABLE", "distribution RECORD is missing")
    rows: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    try:
        for row in csv.reader(io.StringIO(text)):
            if len(row) != 3:
                raise ValueError("RECORD row must have three fields")
            name, digest, size = row
            if (
                not name
                or "\0" in name
                or any(ord(character) < 32 or ord(character) == 127 for character in name)
            ):
                raise ValueError(f"invalid RECORD path: {name!r}")
            if name in seen:
                raise ValueError(f"duplicate RECORD path: {name}")
            seen.add(name)
            rows.append((name, digest, size))
    except (csv.Error, ValueError, ProvenanceError) as exc:
        raise ProvenanceError("DEPENDENCY_IDENTITY_INVALID", f"invalid RECORD: {exc}") from exc
    return rows


def _verify_record_member(
    distribution: importlib.metadata.Distribution,
    name: str,
    encoded_digest: str,
    encoded_size: str,
) -> tuple[Path, bytes, tuple[int, int, int, int]]:
    if not encoded_digest.startswith("sha256=") or not encoded_size.isdigit():
        raise ProvenanceError("DEPENDENCY_IDENTITY_INVALID", f"unhashed RECORD member: {name}")
    path = Path(distribution.locate_file(name))
    data, identity = _safe_read(path)
    observed = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode("ascii")
    if encoded_digest[7:] != observed or int(encoded_size) != len(data):
        raise ProvenanceError("SOURCE_CONTENT_MISMATCH", f"RECORD mismatch: {name}")
    return path, data, identity


def _git_observation(
    root: Path, files: Mapping[str, bytes], provenance: Mapping[str, object]
) -> dict[str, object]:
    unavailable = {
        "commit": None,
        "repository_tree_oid": None,
        "project_tree_oid": None,
        "branch": None,
        "dirty": None,
        "reason": "release-root-has-no-verified-git-context",
    }

    def git(*args: str) -> subprocess.CompletedProcess[str]:
        executable = shutil.which("git") or "/usr/bin/git"
        try:
            return subprocess.run(
                [executable, *args], cwd=root, text=True, capture_output=True, check=False
            )
        except FileNotFoundError:
            return subprocess.CompletedProcess([executable, *args], 127, "", "git unavailable")

    top_result = git("rev-parse", "--show-toplevel")
    if top_result.returncode != 0:
        observation = unavailable
    else:
        top = Path(top_result.stdout.strip()).resolve()
        try:
            relative_root = root.relative_to(top)
        except ValueError:
            observation = unavailable
        else:
            tracked = all(
                git(
                    "ls-files",
                    "--error-unmatch",
                    "--",
                    f":(top){(relative_root / PurePosixPath(member)).as_posix()}",
                ).returncode
                == 0
                for member in files
            )
            if not tracked:
                observation = unavailable
            else:
                commit = git("rev-parse", "HEAD").stdout.strip()
                repository_tree = git("rev-parse", "HEAD^{tree}").stdout.strip()
                treeish = "HEAD^{tree}" if str(relative_root) == "." else f"HEAD:{relative_root.as_posix()}"
                project_tree = git("rev-parse", treeish).stdout.strip()
                branch = git("branch", "--show-current").stdout.strip() or None
                status = git("status", "--porcelain", "--", f":(top){relative_root.as_posix()}")
                observation = {
                    "commit": commit,
                    "repository_tree_oid": repository_tree,
                    "project_tree_oid": project_tree,
                    "branch": branch,
                    "dirty": bool(status.stdout.strip()),
                    "reason": None,
                }
    expected_commit = provenance.get("expected_git_commit")
    expected_tree = provenance.get("expected_project_tree_oid")
    if expected_commit is not None and observation["commit"] != expected_commit:
        raise ProvenanceError("GIT_IDENTITY_MISMATCH", "release Git commit does not match")
    if expected_tree is not None and observation["project_tree_oid"] != expected_tree:
        raise ProvenanceError("GIT_IDENTITY_MISMATCH", "release project tree does not match")
    return observation


def _release_source_capture(provenance: Mapping[str, object]) -> SourceCapture:
    root = _release_root(str(provenance["source_root"]))
    files, paths, identities = _enumerate_release_files(root)
    source_identity = _canonical_source_identity(files)
    if source_identity["sha256"] != provenance["expected_source_sha256"]:
        raise ProvenanceError("SOURCE_CONTENT_MISMATCH", "release source digest does not match")
    requirements = _root_requirement_contract_from_pyproject(root)
    observation = {
        "mode": "release",
        "distribution": None,
        "source_root": str(root),
        "record_verified": None,
        "preflight_sha256": source_identity["sha256"],
        "publication_sha256": None,
        "git": _git_observation(root, files, provenance),
    }
    return SourceCapture(
        provenance=provenance,
        files=MappingProxyType(files),
        source_identity=MappingProxyType(source_identity),
        observation=MappingProxyType(observation),
        root_requirement_contract=MappingProxyType(requirements),
        paths=MappingProxyType(paths),
        stats=MappingProxyType(identities),
    )


def _package_source_capture(provenance: Mapping[str, object]) -> SourceCapture:
    distribution = _unique_distribution(str(provenance["distribution"]))
    rows = _record_rows(distribution)
    files: dict[str, bytes] = {}
    paths: dict[str, Path] = {}
    identities: dict[str, tuple[int, int, int, int]] = {}
    selected_roots: set[str] = set()
    selected_members: set[str] = set()
    for name, digest, size in rows:
        root_name = next(
            (
                candidate
                for candidate in ("gold_research", "quant_platform")
                if name == candidate or name.startswith(candidate + "/")
            ),
            None,
        )
        if (
            root_name is None
            or "__pycache__" in PurePosixPath(name).parts
            or name.endswith((".pyc", ".pyo"))
        ):
            continue
        selected_roots.add(root_name)
        selected_members.add(name)
        if not digest or not size:
            raise ProvenanceError(
                "SOURCE_SET_MISMATCH", f"unhashed first-party RECORD member: {name}"
            )
        path, data, identity = _verify_record_member(distribution, name, digest, size)
        canonical = f"src/{name}"
        _validate_member_name(canonical)
        if canonical in files:
            raise ProvenanceError("SOURCE_SET_MISMATCH", f"duplicate first-party member: {canonical}")
        files[canonical] = data
        paths[canonical] = path
        identities[canonical] = identity
    if selected_roots != {"gold_research", "quant_platform"}:
        raise ProvenanceError("SOURCE_SET_MISMATCH", "package must contain both first-party roots")
    installation_root = Path(str(distribution.locate_file("")))
    actual_members: set[str] = set()
    for root_name in ("gold_research", "quant_platform"):
        package_root = installation_root / root_name
        _assert_no_symlink_path(package_root)
        if not package_root.is_dir():
            raise ProvenanceError("SOURCE_SET_MISMATCH", f"missing package root: {root_name}")
        for path in package_root.rglob("*"):
            if path.is_symlink():
                raise ProvenanceError("SOURCE_SYMLINK_REJECTED", f"symlinked package member: {path}")
            if path.is_dir() or "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
                continue
            actual_members.add(path.relative_to(installation_root).as_posix())
    if actual_members != selected_members:
        raise ProvenanceError("SOURCE_SET_MISMATCH", "installed payload and RECORD path sets differ")
    source_identity = _canonical_source_identity(files)
    requirements = _root_requirement_contract_from_distribution(distribution)
    observation = {
        "mode": "package",
        "distribution": "gold-quant-research",
        "source_root": None,
        "record_verified": True,
        "preflight_sha256": source_identity["sha256"],
        "publication_sha256": None,
        "git": {
            "commit": None,
            "repository_tree_oid": None,
            "project_tree_oid": None,
            "branch": None,
            "dirty": None,
            "reason": "wheel-distribution-has-no-verified-git-context",
        },
    }
    return SourceCapture(
        provenance=provenance,
        files=MappingProxyType(files),
        source_identity=MappingProxyType(source_identity),
        observation=MappingProxyType(observation),
        root_requirement_contract=MappingProxyType(requirements),
        paths=MappingProxyType(paths),
        stats=MappingProxyType(identities),
    )


def capture_source_authority(source_provenance: Mapping[str, object] | None) -> SourceCapture:
    provenance = _validate_source_provenance(source_provenance)
    if provenance["mode"] == "release":
        return _release_source_capture(provenance)
    return _package_source_capture(provenance)


def launch_round4_worker(
    data: object,
    output_root: Path,
    *,
    cost_grid_bps: tuple[float, ...],
    analysis_date: object,
    data_manifest: dict | None,
    source_provenance: Mapping[str, object] | None,
) -> dict:
    """Run the complete Round 4 calculation in one provenance-bound worker."""
    capture = capture_source_authority(source_provenance)
    bootstrap_member = "src/gold_research/_round4_bootstrap.py"
    bootstrap = capture.files.get(bootstrap_member)
    if bootstrap is None:
        raise ProvenanceError("SOURCE_SET_MISMATCH", f"missing {bootstrap_member}")
    try:
        bootstrap_text = bootstrap.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProvenanceError("SOURCE_CONTENT_MISMATCH", "bootstrap is not UTF-8 source") from exc

    request = {
        "data": data,
        "output_root": Path(output_root).absolute(),
        "cost_grid_bps": tuple(cost_grid_bps),
        "analysis_date": analysis_date,
        "data_manifest": data_manifest,
    }
    package_site_roots: list[str] = []
    if capture.provenance["mode"] == "package":
        roots: set[Path] = set()
        for member, path in capture.paths.items():
            tail_parts = PurePosixPath(member).parts[1:]
            roots.add(path.parents[len(tail_parts) - 1])
        package_site_roots = sorted(str(path) for path in roots)
    header = _canonical_json(
        {
            "source_provenance": dict(capture.provenance),
            "package_site_roots": package_site_roots,
        }
    ) + b"\n"
    payload = header + pickle.dumps(request, protocol=5)
    executable = Path(sys.executable).absolute()
    if not executable.exists():
        raise ProvenanceError("RUNTIME_IDENTITY_INVALID", "Python executable is unavailable")
    with tempfile.TemporaryDirectory(prefix="gold-round4-worker-") as temporary:
        private_root = Path(temporary)
        work = private_root / "work"
        home = private_root / "home"
        config = private_root / "config"
        cache = private_root / "cache"
        pycache = private_root / "pycache"
        for directory in (work, home, config, cache, pycache):
            directory.mkdir(mode=0o700)
        environment = {
            "HOME": str(home),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "MKL_NUM_THREADS": "1",
            "MPLCONFIGDIR": str(config),
            "NUMEXPR_NUM_THREADS": "1",
            "OMP_NUM_THREADS": "1",
            "OPENBLAS_NUM_THREADS": "1",
            "PYTHONHASHSEED": "0",
            "PYTHONPYCACHEPREFIX": str(pycache),
            "TZ": "UTC",
            "XDG_CACHE_HOME": str(cache),
            "XDG_CONFIG_HOME": str(config),
        }
        completed = subprocess.run(
            [str(executable), "-B", "-P", "-s", "-S", "-c", bootstrap_text],
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=work,
            env=environment,
            check=False,
        )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        try:
            failure = next(
                json.loads(line)
                for line in detail.splitlines()
                if line.startswith("{") and '"code"' in line
            )
            code = str(failure["code"])
            message = str(failure["detail"])
            exception_type = failure.get("exception_type")
        except (IndexError, KeyError, StopIteration, TypeError, ValueError, json.JSONDecodeError):
            code = "WORKER_FAILED"
            message = detail or f"worker exited {completed.returncode}"
            exception_type = None
        if exception_type == "ValueError":
            raise ValueError(message)
        if exception_type == "FileExistsError":
            raise FileExistsError(message)
        raise ProvenanceError(code, message)
    try:
        result = pickle.loads(completed.stdout)
    except Exception as exc:
        raise ProvenanceError("WORKER_FAILED", "worker returned an invalid result frame") from exc
    if not isinstance(result, dict):
        raise ProvenanceError("WORKER_FAILED", "worker result is not an object")
    return result


def _hash_regular_file(path: Path, code: str) -> tuple[str, int]:
    try:
        data, _ = _safe_read(path)
    except ProvenanceError as exc:
        raise ProvenanceError(code, exc.detail) from exc
    return hashlib.sha256(data).hexdigest(), len(data)


def _loaded_module_identity(context: Mapping[str, object]) -> dict[str, object]:
    allowed_files = context["allowed_files"]
    site_roots = tuple(Path(item) for item in context["site_roots"])
    stdlib_roots = tuple(
        Path(value).resolve()
        for key in ("stdlib", "platstdlib")
        if (value := sysconfig.get_path(key))
    )
    rows: list[dict[str, object]] = []
    for name, module in sorted(sys.modules.items()):
        member = getattr(module, "__provenance_member__", None)
        if member is not None:
            source = context["files"].get(member)
            if source is None:
                raise ProvenanceError("LOADED_CODE_UNBOUND", f"unknown memory-loaded module: {name}")
            rows.append(
                {
                    "module": name,
                    "kind": "first-party-memory",
                    "owner": "gold-quant-research",
                    "member": member,
                    "sha256": hashlib.sha256(source).hexdigest(),
                    "size": len(source),
                    "loader": type(getattr(module, "__loader__", None)).__name__,
                }
            )
            continue
        origin = getattr(module, "__file__", None)
        if origin is None:
            spec = getattr(module, "__spec__", None)
            spec_origin = getattr(spec, "origin", None)
            if spec_origin not in {"built-in", "frozen", None}:
                raise ProvenanceError("UNVERIFIED_LOADED_MODULE", f"pathless module: {name}")
            if (
                spec_origin is None
                and name not in context.get("bootstrap_pathless_modules", frozenset())
            ):
                raise ProvenanceError("UNVERIFIED_LOADED_MODULE", f"unbound pathless module: {name}")
            rows.append(
                {
                    "module": name,
                    "kind": spec_origin or "bootstrap-pathless",
                    "owner": "python-runtime",
                    "member": name,
                    "sha256": context["runtime_identity"]["executable"]["sha256"],
                    "size": context["runtime_identity"]["executable"]["size"],
                    "loader": type(getattr(module, "__loader__", None)).__name__,
                }
            )
            continue
        if str(origin).startswith("provenance://"):
            raise ProvenanceError("LOADED_CODE_UNBOUND", f"unattested memory module: {name}")
        path = Path(origin).resolve()
        if path.suffix == ".pyc":
            try:
                source_path = Path(importlib.util.source_from_cache(str(path))).resolve()
            except ValueError as exc:
                raise ProvenanceError("UNVERIFIED_LOADED_MODULE", f"sourceless bytecode: {name}") from exc
            if not source_path.is_file():
                raise ProvenanceError("UNVERIFIED_LOADED_MODULE", f"sourceless bytecode: {name}")
            path = source_path
        digest, size = _hash_regular_file(path, "UNVERIFIED_LOADED_MODULE")
        record = allowed_files.get(str(path))
        if record is not None:
            rows.append(
                {
                    "module": name,
                    "kind": "dependency",
                    "owners": record["owners"],
                    "member": record["environment_member"],
                    "sha256": digest,
                    "size": size,
                    "loader": type(getattr(module, "__loader__", None)).__name__,
                }
            )
            continue
        if any(path == root or root in path.parents for root in site_roots):
            raise ProvenanceError("UNVERIFIED_LOADED_MODULE", f"module outside dependency closure: {name}")
        stdlib_root = next((root for root in stdlib_roots if path == root or root in path.parents), None)
        if stdlib_root is None:
            raise ProvenanceError("UNVERIFIED_LOADED_MODULE", f"module outside verified stdlib: {name}")
        rows.append(
            {
                "module": name,
                "kind": "stdlib",
                "owner": "python-stdlib",
                "member": path.relative_to(stdlib_root).as_posix(),
                "sha256": digest,
                "size": size,
                "loader": type(getattr(module, "__loader__", None)).__name__,
            }
        )
    payload = sorted(rows, key=lambda row: (str(row["module"]), str(row["member"])))
    return {"modules": payload, "sha256": hashlib.sha256(_canonical_json(payload)).hexdigest()}


def _native_identity(context: Mapping[str, object]) -> dict[str, object]:
    maps_path = Path("/proc/self/maps")
    if not maps_path.is_file():
        raise ProvenanceError("NATIVE_IDENTITY_INVALID", "/proc/self/maps is unavailable")
    allowed_files = context["allowed_files"]
    executable = Path(sys.executable).resolve()
    paths: set[Path] = set()
    try:
        for line in maps_path.read_text(encoding="utf-8").splitlines():
            fields = line.split(maxsplit=5)
            if len(fields) < 6 or "x" not in fields[1] or not fields[5].startswith("/"):
                continue
            if fields[5].endswith(" (deleted)"):
                raise ProvenanceError("NATIVE_IDENTITY_INVALID", "deleted executable mapping")
            paths.add(Path(fields[5]).resolve())
    except OSError as exc:
        raise ProvenanceError("NATIVE_IDENTITY_INVALID", "cannot enumerate mapped objects") from exc
    rows: list[dict[str, object]] = []
    for path in sorted(paths, key=str):
        digest, size = _hash_regular_file(path, "NATIVE_IDENTITY_INVALID")
        record = allowed_files.get(str(path))
        if path == executable:
            ownership = {"owner": "python-runtime"}
            label = "python-executable"
        elif record is not None:
            ownership = {"owners": record["owners"]}
            label = record["environment_member"]
        elif path.is_relative_to("/usr/lib") or path.is_relative_to("/lib") or path.is_relative_to("/lib64"):
            ownership = {"owner": "system-runtime"}
            label = f"{path.name}:{digest}"
        else:
            raise ProvenanceError("NATIVE_IDENTITY_INVALID", f"unclassified mapped object: {path}")
        rows.append(
            {
                **ownership,
                "label": label,
                "build_id": None,
                "build_id_reason": "not-extracted",
                "size": size,
                "sha256": digest,
            }
        )
    payload = sorted(rows, key=_canonical_json)
    return {"objects": payload, "sha256": hashlib.sha256(_canonical_json(payload)).hexdigest()}


def _render_identity(context: Mapping[str, object]) -> dict[str, object]:
    import matplotlib
    from matplotlib import font_manager

    def json_value(value: object) -> object:
        if value is None or isinstance(value, (bool, int, float, str)):
            return value
        if isinstance(value, (list, tuple)):
            return [json_value(item) for item in value]
        return str(value)

    font_path = Path(font_manager.findfont(font_manager.FontProperties())).resolve()
    record = context["allowed_files"].get(str(font_path))
    if record is None or not any(
        owner["distribution_name"] == "matplotlib" for owner in record["owners"]
    ):
        raise ProvenanceError("EXECUTION_RESOURCE_UNBOUND", f"font is not Matplotlib RECORD-bound: {font_path}")
    digest, size = _hash_regular_file(font_path, "EXECUTION_RESOURCE_UNBOUND")
    tracker = context.get("resource_tracker")
    resources = tracker.rows() if tracker is not None else []
    payload = {
        "profile_version": "gold-round4-render-v1",
        "matplotlib_version": matplotlib.__version__,
        "backend": str(matplotlib.get_backend()),
        "renderer": "RendererAgg",
        "dpi": 130,
        "rc_params": {key: json_value(value) for key, value in sorted(matplotlib.rcParams.items())},
        "fonts": [
            {
                "family": list(matplotlib.rcParams["font.family"]),
                "style": "normal",
                "weight": "normal",
                "owners": record["owners"],
                "member": record["environment_member"],
                "size": size,
                "sha256": digest,
            }
        ],
        "resources": resources,
    }
    return {**payload, "sha256": hashlib.sha256(_canonical_json(payload)).hexdigest()}


def seal_execution_identity(context: Mapping[str, object]) -> dict[str, object]:
    recapture = context.get("recapture_environment")
    fresh = recapture() if recapture is not None else context
    if fresh["root_requirement_contract"] != context["root_requirement_contract"]:
        raise ProvenanceError(
            "DEPENDENCY_CHANGED_DURING_RUN", "root requirement contract changed during run"
        )
    if fresh["dependency_identity"] != context["dependency_identity"]:
        raise ProvenanceError(
            "DEPENDENCY_CHANGED_DURING_RUN",
            "dependency environment differs from the bootstrap capture",
        )
    if fresh.get("dependency_filesystem_state") != context.get(
        "dependency_filesystem_state"
    ):
        raise ProvenanceError(
            "DEPENDENCY_CHANGED_DURING_RUN",
            "dependency filesystem state differs from the bootstrap capture",
        )
    if (
        fresh["runtime_identity"] != context["runtime_identity"]
        or fresh["process_identity"] != context["process_identity"]
    ):
        raise ProvenanceError(
            "RUNTIME_CHANGED_DURING_RUN",
            "runtime or process environment differs from the bootstrap capture",
        )
    loaded = _loaded_module_identity(context)
    native = _native_identity(context)
    render = _render_identity(context)
    members = {
        "source_identity": context["source_identity"],
        "root_requirement_contract": context["root_requirement_contract"],
        "dependency_identity": fresh["dependency_identity"],
        "runtime_identity": fresh["runtime_identity"],
        "process_identity": fresh["process_identity"],
        "loaded_module_identity": loaded,
        "native_identity": native,
        "render_identity": render,
    }
    identity = {
        "schema": "gold-round4-execution-identity-v2",
        **members,
        "sha256": hashlib.sha256(_canonical_json(members)).hexdigest(),
    }
    tracker = context.get("resource_tracker")
    if tracker is not None and tracker.sealed_paths is None:
        tracker.seal()
    return identity


def revalidate_execution_identity(
    context: Mapping[str, object], sealed: Mapping[str, object]
) -> None:
    try:
        current = seal_execution_identity(context)
    except Exception as exc:
        observed_code = getattr(exc, "code", None)
        if observed_code in {
            "DEPENDENCY_IDENTITY_INVALID",
            "PACKAGE_METADATA_UNAVAILABLE",
        }:
            raise ProvenanceError(
                "DEPENDENCY_CHANGED_DURING_RUN",
                "dependency environment became invalid after identity seal",
            ) from exc
        if observed_code == "RUNTIME_IDENTITY_INVALID":
            raise ProvenanceError(
                "RUNTIME_CHANGED_DURING_RUN",
                "runtime environment became invalid after identity seal",
            ) from exc
        raise
    if current != sealed:
        if current["dependency_identity"] != sealed["dependency_identity"]:
            code = "DEPENDENCY_CHANGED_DURING_RUN"
        elif (
            current["runtime_identity"] != sealed["runtime_identity"]
            or current["process_identity"] != sealed["process_identity"]
        ):
            code = "RUNTIME_CHANGED_DURING_RUN"
        elif current["native_identity"] != sealed["native_identity"]:
            code = "NATIVE_IDENTITY_INVALID"
        elif current["loaded_module_identity"] != sealed["loaded_module_identity"]:
            code = "UNVERIFIED_LOADED_MODULE"
        else:
            code = "EXECUTION_RESOURCE_UNBOUND"
        raise ProvenanceError(code, "execution environment changed after identity seal")


def _redacted_tracking_uri(uri: str | None) -> str:
    if not uri:
        return "disabled"
    parts = urlsplit(uri)
    hostname = parts.hostname or ""
    netloc = hostname
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def _normalize_data(data: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    normalized = {}
    for symbol, frame in data.items():
        item = frame.copy()
        if "Date" in item.columns:
            item["Date"] = pd.to_datetime(item["Date"])
            item = item.set_index("Date")
        item.index = pd.to_datetime(item.index)
        required = [column for column in ["Open", "Close"] if column in item.columns]
        if required != ["Open", "Close"]:
            raise ValueError(f"{symbol} requires Open and Close columns")
        normalized[symbol] = item.sort_index().dropna(subset=required)
    return normalized


def _data_hash(data: dict[str, pd.DataFrame]) -> str:
    digest = hashlib.sha256()
    canonical = ["Open", "High", "Low", "Close", "Adj Close", "Volume"]
    for symbol in sorted(data):
        digest.update(symbol.encode())
        frame = data[symbol].sort_index()
        columns = [column for column in canonical if column in frame.columns]
        digest.update(frame.loc[:, columns].to_csv(index=True, float_format="%.17g", na_rep="NA").encode())
    return digest.hexdigest()


def _safe_metrics(result: pd.DataFrame) -> dict:
    if len(result) < 2:
        return {}
    return metrics(result)


def _chart(equity: pd.DataFrame, trades: pd.DataFrame, symbol: str, cost_bps: float) -> str:
    fig, axes = plt.subplots(2, 1, figsize=(10, 7), constrained_layout=True)
    subset = equity[equity["symbol"] == symbol]
    for name, group in subset.groupby("strategy"):
        axes[0].plot(pd.to_datetime(group["date"]), group["equity_net"], label=name, linewidth=1.4)
    axes[0].set_title(f"{symbol}: net equity ({cost_bps:g} bps one-way)")
    axes[0].set_ylabel("Growth of 1.0")
    axes[0].grid(alpha=.25)
    axes[0].legend(fontsize=8)
    prices = subset[subset["strategy"] == "donchian_55_20"]
    axes[1].plot(pd.to_datetime(prices["date"]), prices["open"], color="#27364b", linewidth=1)
    ledger = trades[(trades["symbol"] == symbol) & (trades["strategy"] == "donchian_55_20")]
    if not ledger.empty:
        axes[1].scatter(pd.to_datetime(ledger["entry_date"]), ledger["entry_price"], marker="^", s=25, color="#16855b", label="entry")
        closed = ledger[~ledger["is_open"].astype(bool)]
        if not closed.empty:
            axes[1].scatter(pd.to_datetime(closed["exit_date"]), closed["exit_price"], marker="v", s=25, color="#c93f3f", label="exit")
        axes[1].legend(fontsize=8)
    axes[1].set_title("Donchian 55/20 next-open executions")
    axes[1].set_ylabel("Price")
    axes[1].grid(alpha=.25)
    buffer = io.BytesIO()
    fig.savefig(buffer, format="png", dpi=130)
    plt.close(fig)
    return base64.b64encode(buffer.getvalue()).decode()


def _render_report(run_id: str, config: dict, manifest: dict, metrics_df: pd.DataFrame, equity: pd.DataFrame, trades: pd.DataFrame) -> str:
    base = metrics_df[(metrics_df["scenario"] == "base") & (metrics_df["segment"] == "full")]
    oos = metrics_df[(metrics_df["scenario"] == "base") & (metrics_df["segment"] == "out_of_sample")]
    stress = metrics_df[(metrics_df["scenario"] == "cost_stress") & (metrics_df["segment"] == "full")]
    best = oos.sort_values("sharpe", ascending=False).iloc[0]
    comparison_cols = ["symbol", "strategy", "cagr", "cumulative_return", "max_drawdown", "sharpe", "trade_count", "open_trade_count", "market_exposure"]
    table = base[comparison_cols].copy()
    for col in ["cagr", "cumulative_return", "max_drawdown", "market_exposure"]:
        table[col] = table[col].map(lambda x: f"{x:.1%}")
    table["sharpe"] = table["sharpe"].map(lambda x: f"{x:.2f}")
    oos_table = oos[comparison_cols].copy()
    stress_table = stress[comparison_cols].copy()
    stable = metrics_df[metrics_df["scenario"] == "parameter_stability"][["symbol", "strategy", "cagr", "max_drawdown", "sharpe", "trade_count"]]
    trade_view = trades.sort_values("entry_date", ascending=False).head(30).copy()
    if not trade_view.empty:
        trade_view["net_return"] = trade_view["net_return"].map(lambda x: f"{x:.2%}")
    charts = "".join(f'<h3>{symbol}</h3><img alt="{symbol} equity and trades" src="data:image/png;base64,{_chart(equity, trades, symbol, config["cost_bps"])}">' for symbol in sorted(equity["symbol"].unique()))
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Gold research {run_id}</title><style>
:root{{--ink:#172033;--muted:#5d6878;--paper:#f4f6f8;--card:#fff;--accent:#9a6b12}}*{{box-sizing:border-box}}body{{margin:0;background:var(--paper);color:var(--ink);font:15px/1.55 system-ui,sans-serif}}main{{max-width:1080px;margin:auto;padding:20px}}h1{{font-size:30px;line-height:1.15}}h2{{margin-top:30px}}.decision{{border-left:5px solid var(--accent);background:#fff8e8;padding:16px;border-radius:8px}}.card{{background:var(--card);padding:16px;border-radius:10px;margin:14px 0;box-shadow:0 1px 4px #0001}}.scroll{{overflow-x:auto}}table{{border-collapse:collapse;width:100%;font-size:13px}}th,td{{padding:8px;border-bottom:1px solid #e1e5eb;text-align:right;white-space:nowrap}}th:first-child,td:first-child{{text-align:left}}img{{width:100%;height:auto}}code{{overflow-wrap:anywhere}}.muted{{color:var(--muted)}}
@media(max-width:390px){{main{{padding:12px}}h1{{font-size:24px}}.card,.decision{{padding:12px}}table{{font-size:12px}}th,td{{padding:6px}}}}
</style></head><body><main>
<p class="muted">DECISION-FIRST RESEARCH NOTE · {manifest['created_at'][:10]}</p><h1>Gold strategy research: first vertical slice</h1>
<section class="decision"><h2>Conclusion</h2><p><strong>Best out-of-sample Sharpe in this mechanical comparison:</strong> {best['symbol']} / {best['strategy']} ({best['sharpe']:.2f}). This is a research ranking, not a trading recommendation.</p><p>Do not deploy. The evidence uses daily Yahoo proxies, simplified one-way costs, no slippage/roll mechanics/taxes, and only one historical path.</p></section>
<section class="card"><h2>GC=F vs GLD strategy comparison ({config['cost_bps']:g} bps one-way)</h2><div class="scroll">{table.to_html(index=False, border=0)}</div></section>
<section class="card"><h2>Out-of-sample (last 30%)</h2><p>Signals use closes through day <code>t-1</code>, execute at the next available daily open <code>t</code>, and earn open-to-next-open returns. This avoids an unattainable same-close fill, but still does not model opening-auction slippage or intraday execution. The chronological split is 70% research / 30% out-of-sample.</p><div class="scroll">{oos_table.to_html(index=False, border=0, float_format=lambda x:f'{x:.4f}')}</div></section>
<section class="card"><h2>Double-cost stress ({config['stress_cost_bps']:g} bps one-way)</h2><div class="scroll">{stress_table.to_html(index=False, border=0, float_format=lambda x:f'{x:.4f}')}</div></section>
<section class="card"><h2>Donchian parameter stability</h2><p>Neighbor check: entry 50/55/60 days, exit 20 days.</p><div class="scroll">{stable.to_html(index=False, border=0, float_format=lambda x:f'{x:.4f}')}</div></section>
<section class="card"><h2>Equity and trade charts</h2>{charts}</section>
<section class="card"><h2>Recent trades (up to 30)</h2><div class="scroll">{trade_view.to_html(index=False, border=0)}</div></section>
<section class="card"><h2>Data definition and limitations</h2><ul><li><strong>GC=F:</strong> continuous gold-futures research proxy from Yahoo; it is not an executable contract and obscures roll construction.</li><li><strong>GLD:</strong> tradable ETF proxy used for cross-validation; dividends/adjustments depend on Yahoo fields.</li><li>Daily next-open model from 2010 onward; no opening-auction slippage, bid/ask spread model, market impact, financing, tax, futures margin, or contract-roll implementation.</li><li>Survivorship is not the main issue for two fixed proxies, but vendor corrections and symbol methodology can change.</li></ul></section>
<section class="card"><h2>Reproducibility metadata</h2><pre><code>{json.dumps({'run_id':run_id,'config':config,'data_hash':manifest['data_hash'],'git':manifest['git'],'data_manifest':manifest.get('data_manifest')}, indent=2)}</code></pre></section>
</main></body></html>"""


def _mlflow_payloads(rows: list[dict]) -> dict[tuple[str, str], dict[str, float]]:
    payloads: dict[tuple[str, str], dict[str, float]] = {}
    prefixes = {
        ("base", "full"): "full",
        ("base", "out_of_sample"): "oos",
        ("cost_stress", "full"): "stress",
    }
    base_strategies = {
        (row["symbol"], row["strategy"])
        for row in rows
        if row["scenario"] == "base" and row["segment"] == "full"
    }
    for key in base_strategies:
        payloads[key] = {}
    for row in rows:
        key = (row["symbol"], row["strategy"])
        prefix = prefixes.get((row["scenario"], row["segment"]))
        if key not in payloads or prefix is None:
            continue
        for metric_name, value in row.items():
            if metric_name in {"symbol", "strategy", "scenario", "segment"}:
                continue
            if isinstance(value, (int, float)) and math.isfinite(float(value)):
                payloads[key][f"{prefix}_{metric_name}"] = float(value)
    return payloads


def _log_mlflow(tracking_uri: str, run_dir: Path, rows: list[dict], run_id: str, config: dict, manifest: dict):
    import mlflow
    mlflow.set_tracking_uri(tracking_uri)
    mlflow.set_experiment("gold-quant-research")
    for (symbol, strategy), metric_payload in sorted(_mlflow_payloads(rows).items()):
        name = f"{run_id}-{symbol}-{strategy}"
        artifact = run_dir / "mlflow" / f"{symbol.replace('=', '_')}-{strategy}.json"
        artifact.parent.mkdir(exist_ok=True)
        artifact.write_text(json.dumps(metric_payload, indent=2, default=str) + "\n")
        with mlflow.start_run(run_name=name):
            mlflow.set_tags({
                "research_run_id": run_id,
                "prefect_flow_run_id": manifest.get("orchestration_run_id") or "disabled",
                "source_hash": manifest["git"]["source_hash"],
            })
            mlflow.log_params({"run_id": run_id, "symbol": symbol, "strategy": strategy, "cost_bps": config["cost_bps"], "stress_cost_bps": config["stress_cost_bps"], "split_ratio": config["split_ratio"], "data_hash": manifest["data_hash"][:16], "git_commit": manifest["git"]["commit"][:12], "source_hash": manifest["git"]["source_hash"][:16]})
            mlflow.log_metrics(metric_payload)
            mlflow.log_artifact(str(artifact), artifact_path="strategy-results")
            for filename in ["metrics.csv", "trades.csv", "report.html"]:
                mlflow.log_artifact(str(run_dir / filename), artifact_path="research-run")


def run_research(
    data: dict[str, pd.DataFrame],
    output_root: Path,
    cost_bps: float = 5.0,
    tracking_uri: str | None = None,
    data_manifest: dict | None = None,
    orchestration_run_id: str | None = None,
) -> dict:
    data = _normalize_data(data)
    config = {"version": 1, "symbols": sorted(data), "strategies": ["buy_and_hold", "sma_50_200", "donchian_55_20"], "cost_bps": cost_bps, "split_ratio": 0.7, "stress_cost_bps": cost_bps * 2, "donchian_stability_entries": [50, 55, 60], "donchian_exit": 20}
    data_hash = _data_hash(data)
    git = get_git_state()
    run_id = stable_run_id(config, data_hash, canonical_hash(git))
    run_dir = Path(output_root) / run_id
    if run_dir.exists():
        raise FileExistsError(f"immutable run already exists: {run_dir}")
    run_dir.mkdir(parents=True, exist_ok=False)
    manifest = {
        "run_id": run_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "data_hash": data_hash,
        "git": git,
        "data_manifest": data_manifest,
        "tracking_uri": _redacted_tracking_uri(tracking_uri),
        "orchestration_run_id": orchestration_run_id,
    }
    rows, equities, ledgers = [], [], []
    for symbol, frame in data.items():
        close = frame["Close"]
        open_price = frame["Open"]
        cut = int(len(close) * config["split_ratio"])
        for strategy, signal in strategy_signals(close).items():
            for segment, slc in [("full", slice(None)), ("research", slice(0, cut)), ("out_of_sample", slice(cut, None))]:
                result = backtest(open_price.iloc[slc], signal.iloc[slc], cost_bps)
                rows.append({"symbol": symbol, "strategy": strategy, "segment": segment, "scenario": "base", **_safe_metrics(result)})
                if segment == "full":
                    eq = result.reset_index(names="date")
                    eq.insert(0, "strategy", strategy)
                    eq.insert(0, "symbol", symbol)
                    equities.append(eq)
                    ledger = trade_ledger(result)
                    if not ledger.empty:
                        ledger.insert(0, "strategy", strategy)
                        ledger.insert(0, "symbol", symbol)
                        ledgers.append(ledger)
            stress_result = backtest(open_price, signal, cost_bps * 2)
            rows.append({"symbol": symbol, "strategy": strategy, "segment": "full", "scenario": "cost_stress", **_safe_metrics(stress_result)})
        for entry in config["donchian_stability_entries"]:
            result = backtest(open_price, donchian_signal(close, entry, 20), cost_bps)
            rows.append({"symbol": symbol, "strategy": f"donchian_{entry}_20", "segment": "full", "scenario": "parameter_stability", **_safe_metrics(result)})
    metrics_df = pd.DataFrame(rows)
    equity_df = pd.concat(equities, ignore_index=True)
    trades_df = pd.concat(ledgers, ignore_index=True) if ledgers else pd.DataFrame(columns=["symbol", "strategy", "entry_date", "exit_date", "entry_price", "exit_price", "net_return", "bars", "is_open"])
    validations = {"research": "first 70% chronological", "out_of_sample": "last 30% chronological", "execution": "prior-close signal, next-open fill, open-to-open return", "cost_stress": f"{cost_bps * 2:g} bps one-way", "parameter_stability": "Donchian entry 50/55/60, exit 20"}
    (run_dir / "config.json").write_text(json.dumps(config, indent=2, sort_keys=True) + "\n")
    (run_dir / "run_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True, default=str) + "\n")
    (run_dir / "metrics.json").write_text(json.dumps(rows, indent=2, default=str) + "\n")
    metrics_df.to_csv(run_dir / "metrics.csv", index=False)
    equity_df.to_csv(run_dir / "equity.csv", index=False)
    trades_df.to_csv(run_dir / "trades.csv", index=False)
    report = _render_report(run_id, config, manifest, metrics_df, equity_df, trades_df)
    (run_dir / "report.html").write_text(report)
    if tracking_uri:
        _log_mlflow(tracking_uri, run_dir, rows, run_id, config, manifest)
    return {"run_id": run_id, "run_dir": str(run_dir), "validations": validations, "metrics": rows}
