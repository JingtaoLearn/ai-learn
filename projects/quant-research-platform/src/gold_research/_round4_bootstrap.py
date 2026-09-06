"""Isolated Gold Round 4 worker bootstrap.

This module is executed from source bytes selected by the provenance authority.  It
uses only the standard library until source and installed-distribution metadata
have been captured, then installs the sole first-party loader and an import guard.
"""
from __future__ import annotations

import base64
import contextlib
import csv
import hashlib
import importlib.abc
import importlib.machinery
import importlib.metadata
import importlib.util
import io
import json
import os
import pickle
import re
import stat
import sys
import sysconfig
import tomllib
import traceback
from dataclasses import dataclass
from email.message import Message
from pathlib import Path, PurePosixPath
from typing import Mapping

_CANONICAL_ROOTS = ("src/gold_research", "src/quant_platform")
_FIXED_ENV_KEYS = {
    "HOME",
    "LANG",
    "LC_ALL",
    "MKL_NUM_THREADS",
    "MPLCONFIGDIR",
    "NUMEXPR_NUM_THREADS",
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "PYTHONHASHSEED",
    "PYTHONPYCACHEPREFIX",
    "TZ",
    "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME",
}
_NAME_RX = re.compile(r"[-_.]+")
_FONT_SUFFIXES = {".afm", ".otf", ".pfb", ".ttc", ".ttf"}
_CODE_SUFFIXES = {".py", ".pyc", ".pyo", ".so", ".pyd"}
_BOOTSTRAP_PATHLESS_MODULES = frozenset(
    {
        "__main__",
        "_cython_3_1_2",
        "cython_runtime",
        "pyexpat.errors",
        "pyexpat.model",
        "six.moves",
        "typing.io",
        "typing.re",
        "xml.parsers.expat.errors",
        "xml.parsers.expat.model",
    }
)


class BootstrapError(RuntimeError):
    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _normalize_name(value: str) -> str:
    return _NAME_RX.sub("-", value).lower()


def _site_roots(extra_roots: object = None) -> tuple[Path, ...]:
    roots: list[Path] = []
    if extra_roots is not None:
        if not isinstance(extra_roots, list) or not all(isinstance(item, str) for item in extra_roots):
            raise BootstrapError("PACKAGE_METADATA_UNAVAILABLE", "invalid package installation roots")
        for item in extra_roots:
            root = Path(item)
            if not root.is_absolute() or root.resolve(strict=True) != root or not root.is_dir():
                raise BootstrapError("PACKAGE_METADATA_UNAVAILABLE", "invalid package installation root")
            _assert_no_symlink(root, "PACKAGE_METADATA_UNAVAILABLE")
            roots.append(root)
    executable_environment = Path(sys.executable).absolute().parent.parent
    venv_site = executable_environment / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
    if (executable_environment / "pyvenv.cfg").is_file() and venv_site.is_dir():
        roots.append(venv_site.resolve())
    else:
        for key in ("purelib", "platlib"):
            value = sysconfig.get_path(key)
            if value:
                root = Path(value).resolve()
                if root not in roots:
                    roots.append(root)
    if not roots:
        raise BootstrapError("DEPENDENCY_IDENTITY_INVALID", "no interpreter package roots")
    return tuple(dict.fromkeys(roots))


def _validate_source_member(name: str) -> PurePosixPath:
    if not name or name.startswith("/") or "\\" in name or "\0" in name or "//" in name:
        raise BootstrapError("SOURCE_ROOT_INVALID", f"invalid source member: {name!r}")
    parts = name.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise BootstrapError("SOURCE_ROOT_INVALID", f"invalid source member: {name!r}")
    return PurePosixPath(*parts)


def _assert_no_symlink(path: Path, code: str) -> None:
    absolute = path if path.is_absolute() else path.absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        try:
            mode = current.lstat().st_mode
        except OSError as exc:
            raise BootstrapError(code, f"missing path: {current}") from exc
        if stat.S_ISLNK(mode):
            raise BootstrapError(code, f"symlink is not permitted: {current}")


def _safe_read(path: Path, code: str) -> tuple[bytes, tuple[int, int, int, int]]:
    _assert_no_symlink(path, code)
    try:
        before = path.stat(follow_symlinks=False)
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise BootstrapError(code, f"cannot open regular file: {path}") from exc
    try:
        opened = os.fstat(descriptor)
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if not stat.S_ISREG(before.st_mode):
        raise BootstrapError(code, f"not a regular file: {path}")
    identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    if identity != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns):
        raise BootstrapError(code, f"file changed while opening: {path}")
    if identity != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise BootstrapError(code, f"file changed while reading: {path}")
    data = b"".join(chunks)
    if len(data) != before.st_size:
        raise BootstrapError(code, f"short read: {path}")
    return data, identity


def _source_identity(files: Mapping[str, bytes]) -> dict[str, object]:
    if not files:
        raise BootstrapError("SOURCE_SET_EMPTY", "canonical source payload is empty")
    digest = hashlib.sha256()
    for name in sorted(files, key=lambda item: item.encode("utf-8")):
        _validate_source_member(name)
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(files[name])
        digest.update(b"\0")
    return {
        "schema": "gold-first-party-runtime-v1",
        "algorithm": "sha256",
        "sha256": digest.hexdigest(),
        "file_count": len(files),
    }


def _validate_provenance(value: object) -> dict[str, object]:
    if value is None:
        return {"mode": "package", "distribution": "gold-quant-research"}
    if not isinstance(value, dict):
        raise BootstrapError("PROVENANCE_AUTHORITY_AMBIGUOUS", "source_provenance must be an object")
    mode = value.get("mode")
    if mode == "package":
        if value != {"mode": "package", "distribution": "gold-quant-research"}:
            raise BootstrapError("PROVENANCE_AUTHORITY_AMBIGUOUS", "invalid package authority")
    elif mode == "release":
        required = {"mode", "source_root", "expected_source_sha256"}
        optional = {"expected_git_commit", "expected_project_tree_oid"}
        if not required <= set(value) or not set(value) <= required | optional:
            raise BootstrapError("PROVENANCE_AUTHORITY_AMBIGUOUS", "invalid release authority")
        digest = value.get("expected_source_sha256")
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise BootstrapError("PROVENANCE_AUTHORITY_AMBIGUOUS", "invalid expected source digest")
    else:
        raise BootstrapError("PROVENANCE_AUTHORITY_AMBIGUOUS", f"unknown authority mode: {mode!r}")
    return dict(value)


def _distribution_map(site_roots: tuple[Path, ...]) -> dict[str, list[importlib.metadata.Distribution]]:
    result: dict[str, list[importlib.metadata.Distribution]] = {}
    for distribution in importlib.metadata.distributions(path=[str(path) for path in site_roots]):
        name = distribution.metadata.get("Name")
        if name:
            result.setdefault(_normalize_name(name), []).append(distribution)
    return result


def _unique_distribution(
    distributions: Mapping[str, list[importlib.metadata.Distribution]], name: str
) -> importlib.metadata.Distribution:
    matches = distributions.get(_normalize_name(name), [])
    if len(matches) != 1:
        code = "PACKAGE_METADATA_UNAVAILABLE" if not matches else "DEPENDENCY_IDENTITY_INVALID"
        raise BootstrapError(code, f"expected one {name} distribution, found {len(matches)}")
    return matches[0]


def _record_rows(distribution: importlib.metadata.Distribution) -> list[tuple[str, str, str]]:
    text = distribution.read_text("RECORD")
    if text is None:
        raise BootstrapError("DEPENDENCY_IDENTITY_INVALID", "distribution RECORD is missing")
    rows: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    try:
        parsed = csv.reader(io.StringIO(text))
        for row in parsed:
            if len(row) != 3:
                raise ValueError("RECORD row must contain three fields")
            name, digest, size = row
            parts = name.split("/")
            first_payload_part = next(
                (index for index, part in enumerate(parts) if part != ".."), len(parts)
            )
            if (
                not name
                or name.startswith("/")
                or "//" in name
                or "\\" in name
                or "\0" in name
                or any(ord(character) < 32 or ord(character) == 127 for character in name)
                or any(part in {"", "."} for part in parts)
                or any(part == ".." for part in parts[first_payload_part:])
                or first_payload_part == len(parts)
            ):
                raise ValueError(f"invalid RECORD path: {name!r}")
            normalized = PurePosixPath(name).as_posix()
            if normalized in seen:
                raise ValueError(f"duplicate RECORD path: {normalized}")
            seen.add(normalized)
            rows.append((normalized, digest, size))
    except (csv.Error, ValueError) as exc:
        raise BootstrapError("DEPENDENCY_IDENTITY_INVALID", str(exc)) from exc
    return rows


def _inside(path: Path, roots: tuple[Path, ...]) -> bool:
    return any(path == root or root in path.parents for root in roots)


def _environment_roots(site_roots: tuple[Path, ...]) -> tuple[Path, ...]:
    roots: list[Path] = []
    for site_root in site_roots:
        root = site_root
        for candidate in (site_root, *site_root.parents):
            if (candidate / "pyvenv.cfg").is_file():
                root = candidate
                break
        if root not in roots:
            roots.append(root)
    return tuple(roots)


@dataclass(frozen=True)
class _InstalledDistribution:
    distribution: importlib.metadata.Distribution
    name: str
    version: str
    metadata_dir: Path
    distribution_base: Path
    environment_root: Path
    claims: tuple[dict[str, object], ...]
    metadata_sha256: str
    record_sha256: str


def _environment_root_for(path: Path, roots: tuple[Path, ...]) -> Path:
    matches = [root for root in roots if path == root or root in path.parents]
    if len(matches) != 1:
        raise BootstrapError(
            "DEPENDENCY_IDENTITY_INVALID",
            f"metadata root has no unique interpreter environment: {path}",
        )
    return matches[0]


def _canonical_record_target(
    distribution_base: Path,
    environment_root: Path,
    record_member: str,
) -> tuple[str, Path]:
    parts = record_member.split("/")
    leading_parent_count = 0
    for part in parts:
        if part != "..":
            break
        leading_parent_count += 1
    if leading_parent_count == len(parts) or any(
        part in {"", ".", ".."} for part in parts[leading_parent_count:]
    ):
        raise BootstrapError(
            "DEPENDENCY_IDENTITY_INVALID", f"invalid RECORD path: {record_member!r}"
        )
    target = distribution_base
    for _ in range(leading_parent_count):
        target = target.parent
    target = target.joinpath(*parts[leading_parent_count:])
    try:
        environment_member = target.relative_to(environment_root).as_posix()
    except ValueError as exc:
        raise BootstrapError(
            "DEPENDENCY_IDENTITY_INVALID",
            f"RECORD path escapes environment: {record_member}",
        ) from exc
    recomputed = os.path.relpath(target, distribution_base).replace(os.sep, "/")
    if recomputed != record_member:
        raise BootstrapError(
            "DEPENDENCY_IDENTITY_INVALID",
            f"noncanonical RECORD path: {record_member!r}",
        )
    if environment_member.startswith("../") or environment_member in {"", "."}:
        raise BootstrapError(
            "DEPENDENCY_IDENTITY_INVALID",
            f"RECORD path escapes environment: {record_member}",
        )
    return environment_member, target


def _secure_environment_read(
    environment_root: Path,
    environment_member: str,
    code: str = "DEPENDENCY_IDENTITY_INVALID",
) -> tuple[bytes, tuple[int, int, int, int, int]]:
    parts = PurePosixPath(environment_member).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise BootstrapError(code, f"invalid environment member: {environment_member!r}")
    _assert_no_symlink(environment_root, code)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    directory_flags = flags | getattr(os, "O_DIRECTORY", 0)
    descriptors: list[int] = []
    try:
        descriptor = os.open(environment_root, directory_flags)
        descriptors.append(descriptor)
        for component in parts[:-1]:
            descriptor = os.open(component, directory_flags, dir_fd=descriptor)
            descriptors.append(descriptor)
        file_descriptor = os.open(parts[-1], flags, dir_fd=descriptor)
        descriptors.append(file_descriptor)
        before = os.fstat(file_descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise BootstrapError(code, f"payload is not one singly linked regular file: {environment_member}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(file_descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(file_descriptor)
    except BootstrapError:
        raise
    except OSError as exc:
        raise BootstrapError(code, f"cannot securely read environment member: {environment_member}") from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
    identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_nlink)
    if identity != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_nlink):
        raise BootstrapError(code, f"environment member changed while reading: {environment_member}")
    data = b"".join(chunks)
    if len(data) != before.st_size:
        raise BootstrapError(code, f"short read: {environment_member}")
    return data, identity


def _installed_inventory(
    site_roots: tuple[Path, ...],
) -> tuple[dict[str, _InstalledDistribution], dict[str, tuple[dict[str, object], ...]], dict[str, object]]:
    environment_roots = _environment_roots(site_roots)
    installed: dict[str, _InstalledDistribution] = {}
    claimant_rows: dict[str, list[dict[str, object]]] = {}
    metadata_root_members: list[str] = []
    for site_root in site_roots:
        environment_root = _environment_root_for(site_root, environment_roots)
        _assert_no_symlink(site_root, "DEPENDENCY_IDENTITY_INVALID")
        if site_root.resolve(strict=True) != site_root:
            raise BootstrapError("DEPENDENCY_IDENTITY_INVALID", "metadata root is not canonical")
        metadata_root_members.append(site_root.relative_to(environment_root).as_posix())
        root_descriptor = os.open(
            site_root,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            with os.scandir(root_descriptor) as entries:
                names = sorted(entry.name for entry in entries if entry.name.endswith(".dist-info"))
        finally:
            os.close(root_descriptor)
        for directory_name in names:
            metadata_dir = site_root / directory_name
            try:
                metadata_mode = metadata_dir.lstat().st_mode
            except OSError as exc:
                raise BootstrapError(
                    "DEPENDENCY_IDENTITY_INVALID", f"missing metadata directory: {metadata_dir}"
                ) from exc
            if stat.S_ISLNK(metadata_mode) or not stat.S_ISDIR(metadata_mode):
                raise BootstrapError(
                    "DEPENDENCY_IDENTITY_INVALID", f"invalid metadata directory: {metadata_dir}"
                )
            metadata_member = metadata_dir.relative_to(environment_root).joinpath("METADATA").as_posix()
            record_member = metadata_dir.relative_to(environment_root).joinpath("RECORD").as_posix()
            metadata_bytes, _ = _secure_environment_read(environment_root, metadata_member)
            record_bytes, _ = _secure_environment_read(environment_root, record_member)
            distribution = importlib.metadata.PathDistribution(metadata_dir)
            raw_name = distribution.metadata.get("Name")
            raw_version = distribution.metadata.get("Version")
            if not raw_name or raw_version is None:
                raise BootstrapError(
                    "DEPENDENCY_IDENTITY_INVALID", f"invalid METADATA identity: {directory_name}"
                )
            normalized_name = _normalize_name(str(raw_name))
            if (
                normalized_name.encode("ascii", errors="ignore").decode("ascii") != normalized_name
                or re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", normalized_name) is None
            ):
                raise BootstrapError(
                    "DEPENDENCY_IDENTITY_INVALID", f"invalid distribution name: {raw_name!r}"
                )
            if normalized_name in installed:
                raise BootstrapError(
                    "DEPENDENCY_IDENTITY_INVALID", f"duplicate installed distribution: {normalized_name}"
                )
            distribution_base = metadata_dir.parent
            claims: list[dict[str, object]] = []
            seen_targets: set[str] = set()
            for member, encoded_digest, encoded_size in _record_rows(distribution):
                environment_member, absolute_path = _canonical_record_target(
                    distribution_base, environment_root, member
                )
                if environment_member in seen_targets:
                    raise BootstrapError(
                        "DEPENDENCY_IDENTITY_INVALID",
                        f"duplicate normalized RECORD target: {normalized_name}:{environment_member}",
                    )
                seen_targets.add(environment_member)
                claim = {
                    "distribution_name": normalized_name,
                    "distribution_version": str(raw_version),
                    "record_member": member,
                    "record_hash": encoded_digest,
                    "record_size": encoded_size,
                    "environment_member": environment_member,
                    "absolute_path": str(absolute_path),
                }
                claims.append(claim)
                claimant_rows.setdefault(environment_member, []).append(
                    {key: value for key, value in claim.items() if key != "absolute_path"}
                )
            installed[normalized_name] = _InstalledDistribution(
                distribution=distribution,
                name=normalized_name,
                version=str(raw_version),
                metadata_dir=metadata_dir,
                distribution_base=distribution_base,
                environment_root=environment_root,
                claims=tuple(claims),
                metadata_sha256=_hash_bytes(metadata_bytes),
                record_sha256=_hash_bytes(record_bytes),
            )
    if not installed:
        raise BootstrapError("DEPENDENCY_IDENTITY_INVALID", "installed distribution inventory is empty")
    canonical_claimants = {
        member: tuple(
            sorted(rows, key=lambda row: _canonical_json(row))
        )
        for member, rows in claimant_rows.items()
    }
    inventory_rows = [
        {
            "distribution_name": item.name,
            "distribution_version": item.version,
            "metadata_sha256": item.metadata_sha256,
            "record_sha256": item.record_sha256,
        }
        for item in sorted(installed.values(), key=lambda item: item.name)
    ]
    claimant_payload = [
        {"environment_member": member, "claimants": list(canonical_claimants[member])}
        for member in sorted(canonical_claimants, key=lambda value: value.encode("utf-8"))
    ]
    seal = {
        "schema": "gold-installed-claimant-inventory-v1",
        "metadata_roots": sorted(metadata_root_members, key=lambda value: value.encode("utf-8")),
        "distribution_count": len(inventory_rows),
        "record_row_count": sum(len(item.claims) for item in installed.values()),
        "distributions_sha256": _hash_bytes(_canonical_json(inventory_rows)),
        "claimants_sha256": _hash_bytes(_canonical_json(claimant_payload)),
    }
    return installed, canonical_claimants, seal


def _record_digest(encoded_digest: str, member: str) -> str:
    if not encoded_digest.startswith("sha256="):
        raise BootstrapError("DEPENDENCY_IDENTITY_INVALID", f"unsupported RECORD hash: {member}")
    digest = encoded_digest[7:]
    if re.fullmatch(r"[A-Za-z0-9_-]{43}", digest) is None:
        raise BootstrapError("DEPENDENCY_IDENTITY_INVALID", f"invalid RECORD digest: {member}")
    try:
        decoded = base64.urlsafe_b64decode(digest + "=")
    except ValueError as exc:
        raise BootstrapError("DEPENDENCY_IDENTITY_INVALID", f"invalid RECORD digest: {member}") from exc
    if len(decoded) != 32:
        raise BootstrapError("DEPENDENCY_IDENTITY_INVALID", f"invalid RECORD digest: {member}")
    return digest


def _verify_record(
    installed: _InstalledDistribution,
) -> tuple[dict[str, object], dict[str, dict[str, object]]]:
    members: list[dict[str, object]] = []
    allowed: dict[str, dict[str, object]] = {}
    for claim in installed.claims:
        name = str(claim["record_member"])
        encoded_digest = str(claim["record_hash"])
        encoded_size = str(claim["record_size"])
        parts = PurePosixPath(name).parts
        if "__pycache__" in parts or name.endswith((".pyc", ".pyo")):
            continue
        is_record = name.endswith(".dist-info/RECORD")
        if not encoded_digest or not encoded_size:
            if is_record and not encoded_digest and not encoded_size:
                continue
            raise BootstrapError("DEPENDENCY_IDENTITY_INVALID", f"unhashed RECORD member: {name}")
        digest = _record_digest(encoded_digest, name)
        if not encoded_size.isdigit() or int(encoded_size) > 9_007_199_254_740_991:
            raise BootstrapError("DEPENDENCY_IDENTITY_INVALID", f"unsupported RECORD identity: {name}")
        environment_member = str(claim["environment_member"])
        data, _ = _secure_environment_read(installed.environment_root, environment_member)
        observed = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode("ascii")
        if observed != digest or len(data) != int(encoded_size):
            raise BootstrapError("DEPENDENCY_IDENTITY_INVALID", f"RECORD mismatch: {name}")
        owner = {
            "distribution_name": installed.name,
            "distribution_version": installed.version,
            "record_hash_algorithm": "sha256",
            "record_hash_digest": digest,
            "record_member": name,
            "record_size": len(data),
        }
        record = {
            "environment_member": environment_member,
            "observed_sha256": _hash_bytes(data),
            "observed_size": len(data),
            "owners": [owner],
        }
        members.append(record)
        key = str(claim["absolute_path"])
        if key in allowed:
            raise BootstrapError(
                "DEPENDENCY_IDENTITY_INVALID", f"duplicate resolved RECORD owner: {name}"
            )
        allowed[key] = record
    direct_url = installed.distribution.read_text("direct_url.json")
    if direct_url:
        try:
            if json.loads(direct_url).get("dir_info", {}).get("editable") is True:
                raise BootstrapError(
                    "DEPENDENCY_IDENTITY_INVALID", f"editable distribution: {installed.name}"
                )
        except json.JSONDecodeError as exc:
            raise BootstrapError(
                "DEPENDENCY_IDENTITY_INVALID", f"invalid direct_url.json: {installed.name}"
            ) from exc
    canonical_members = sorted(
        members, key=lambda item: str(item["environment_member"]).encode("utf-8")
    )
    return {
        "member_count": len(canonical_members),
        "record_payload_sha256": _hash_bytes(_canonical_json(canonical_members)),
    }, allowed


def _metadata_requirements(metadata: Message) -> list[str]:
    return [str(value) for value in (metadata.get_all("Requires-Dist") or [])]


def _release_files(root_value: object) -> tuple[Path, dict[str, bytes], dict[str, tuple[int, int, int, int]]]:
    if not isinstance(root_value, str) or not root_value.startswith("/") or "\\" in root_value:
        raise BootstrapError("SOURCE_ROOT_INVALID", "release root must be an absolute POSIX path")
    if "//" in root_value or any(part in {".", ".."} for part in root_value.split("/")[1:]):
        raise BootstrapError("SOURCE_ROOT_INVALID", "release root contains a lexical alias")
    root = Path(root_value)
    _assert_no_symlink(root, "SOURCE_ROOT_INVALID")
    if root.resolve(strict=True) != root or not root.is_dir():
        raise BootstrapError("SOURCE_ROOT_INVALID", "release root is not a canonical directory")
    files: dict[str, bytes] = {}
    identities: dict[str, tuple[int, int, int, int]] = {}
    for root_name in _CANONICAL_ROOTS:
        package_root = root / PurePosixPath(root_name)
        _assert_no_symlink(package_root, "SOURCE_SET_MISMATCH")
        if not package_root.is_dir():
            raise BootstrapError("SOURCE_SET_MISMATCH", f"missing canonical root: {root_name}")
        for path in package_root.rglob("*"):
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                raise BootstrapError("SOURCE_SYMLINK_REJECTED", f"symlinked source member: {relative}")
            if path.is_dir() or "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
                continue
            _validate_source_member(relative)
            data, identity = _safe_read(path, "SOURCE_CHANGED_DURING_RUN")
            files[relative] = data
            identities[relative] = identity
    return root, files, identities


def _package_files(
    distribution: importlib.metadata.Distribution,
) -> tuple[dict[str, bytes], dict[str, tuple[int, int, int, int]]]:
    files: dict[str, bytes] = {}
    identities: dict[str, tuple[int, int, int, int]] = {}
    roots: set[str] = set()
    selected_members: set[str] = set()
    for name, digest, size in _record_rows(distribution):
        first = PurePosixPath(name).parts[0] if PurePosixPath(name).parts else ""
        if first not in {"gold_research", "quant_platform"}:
            continue
        if "__pycache__" in PurePosixPath(name).parts or name.endswith((".pyc", ".pyo")):
            continue
        if not digest.startswith("sha256=") or not size.isdigit():
            raise BootstrapError("SOURCE_SET_MISMATCH", f"unhashed first-party member: {name}")
        path = Path(distribution.locate_file(name))
        data, identity = _safe_read(path, "SOURCE_CHANGED_DURING_RUN")
        observed = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode("ascii")
        if observed != digest[7:] or len(data) != int(size):
            raise BootstrapError("SOURCE_CONTENT_MISMATCH", f"first-party RECORD mismatch: {name}")
        canonical = f"src/{name}"
        _validate_source_member(canonical)
        if canonical in files:
            raise BootstrapError("SOURCE_SET_MISMATCH", f"duplicate first-party member: {canonical}")
        files[canonical] = data
        identities[canonical] = identity
        roots.add(first)
        selected_members.add(name)
    if roots != {"gold_research", "quant_platform"}:
        raise BootstrapError("SOURCE_SET_MISMATCH", "package must contain both first-party roots")
    installation_root = Path(str(distribution.locate_file("")))
    actual_members: set[str] = set()
    for root_name in ("gold_research", "quant_platform"):
        package_root = installation_root / root_name
        _assert_no_symlink(package_root, "SOURCE_SET_MISMATCH")
        if not package_root.is_dir():
            raise BootstrapError("SOURCE_SET_MISMATCH", f"missing package root: {root_name}")
        for path in package_root.rglob("*"):
            if path.is_symlink():
                raise BootstrapError("SOURCE_SYMLINK_REJECTED", f"symlinked package member: {path}")
            if path.is_dir() or "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
                continue
            actual_members.add(path.relative_to(installation_root).as_posix())
    if actual_members != selected_members:
        raise BootstrapError("SOURCE_SET_MISMATCH", "installed payload and RECORD path sets differ")
    return files, identities


def _canonical_requirement_contract(
    name: str,
    version: str,
    requires_python: str,
    requirements: list[str],
) -> tuple[dict[str, object], list[object]]:
    from packaging.markers import default_environment
    from packaging.requirements import Requirement
    from packaging.specifiers import SpecifierSet

    environment = default_environment()
    active: list[object] = []
    canonical: list[tuple[str, str]] = []
    try:
        for raw in requirements:
            requirement = Requirement(raw)
            if requirement.marker is not None and not requirement.marker.evaluate(
                {**environment, "extra": ""}
            ):
                continue
            active.append(requirement)
            canonical.append((_normalize_name(requirement.name), str(requirement)))
    except Exception as exc:
        raise BootstrapError("DEPENDENCY_IDENTITY_INVALID", "invalid requirement metadata") from exc
    return {
        "name": _normalize_name(name),
        "version": version,
        "requires_python": str(SpecifierSet(requires_python)),
        "requires_dist": [value for _, value in sorted(canonical)],
    }, active


def _root_fields(
    provenance: Mapping[str, object],
    source_root: Path | None,
    distributions: Mapping[str, list[importlib.metadata.Distribution]],
) -> tuple[str, str, str, list[str]]:
    if provenance["mode"] == "package":
        distribution = _unique_distribution(distributions, "gold-quant-research")
        metadata = distribution.metadata
        try:
            return (
                str(metadata["Name"]),
                str(distribution.version),
                str(metadata["Requires-Python"]),
                _metadata_requirements(metadata),
            )
        except (KeyError, TypeError) as exc:
            raise BootstrapError(
                "PACKAGE_METADATA_UNAVAILABLE", "incomplete first-party METADATA"
            ) from exc
    if source_root is None:
        raise BootstrapError("DEPENDENCY_IDENTITY_INVALID", "release source root is unavailable")
    pyproject_bytes, _ = _safe_read(
        source_root / "pyproject.toml", "DEPENDENCY_IDENTITY_INVALID"
    )
    try:
        project = tomllib.loads(pyproject_bytes.decode("utf-8"))["project"]
        return (
            str(project["name"]),
            str(project["version"]),
            str(project["requires-python"]),
            [str(item) for item in project.get("dependencies", [])],
        )
    except (KeyError, TypeError, ValueError, UnicodeError) as exc:
        raise BootstrapError("DEPENDENCY_IDENTITY_INVALID", "invalid release pyproject") from exc


def _dependency_environment(
    provenance: Mapping[str, object],
    source_root: Path | None,
    site_roots: tuple[Path, ...],
) -> dict[str, object]:
    distributions = _distribution_map(site_roots)
    installed, installed_claimants, inventory_seal = _installed_inventory(site_roots)
    root_contract, pending = _canonical_requirement_contract(
        *_root_fields(provenance, source_root, distributions)
    )
    try:
        from packaging.markers import default_environment
        from packaging.requirements import Requirement
        from packaging.version import Version

        environment = default_environment()
        closure: dict[str, dict[str, object]] = {}
        allowed_files: dict[str, dict[str, object]] = {}
        queue = list(pending)
        while queue:
            requirement = queue.pop(0)
            normalized = _normalize_name(requirement.name)
            if normalized in closure:
                if not requirement.specifier.contains(
                    Version(str(closure[normalized]["version"])), prereleases=True
                ):
                    raise BootstrapError(
                        "DEPENDENCY_IDENTITY_INVALID", f"version mismatch: {requirement}"
                    )
                continue
            matches = [installed[_normalize_name(requirement.name)]] if _normalize_name(requirement.name) in installed else []
            if len(matches) != 1:
                raise BootstrapError(
                    "DEPENDENCY_IDENTITY_INVALID",
                    f"expected one {requirement.name} distribution, found {len(matches)}",
                )
            installed_distribution = matches[0]
            distribution = installed_distribution.distribution
            if not requirement.specifier.contains(
                Version(installed_distribution.version), prereleases=True
            ):
                raise BootstrapError(
                    "DEPENDENCY_IDENTITY_INVALID", f"version mismatch: {requirement}"
                )
            requires = []
            for raw in _metadata_requirements(distribution.metadata):
                child = Requirement(raw)
                if child.marker is None or child.marker.evaluate({**environment, "extra": ""}):
                    requires.append(child)
            record_identity, member_files = _verify_record(installed_distribution)
            for path, incoming in member_files.items():
                existing = allowed_files.get(path)
                if existing is None:
                    allowed_files[path] = incoming
                    continue
                if (
                    existing["environment_member"] != incoming["environment_member"]
                    or existing["observed_sha256"] != incoming["observed_sha256"]
                    or existing["observed_size"] != incoming["observed_size"]
                ):
                    raise BootstrapError(
                        "DEPENDENCY_IDENTITY_INVALID", f"divergent RECORD ownership: {path}"
                    )
                existing["owners"].extend(incoming["owners"])
            closure[normalized] = {
                "name": normalized,
                "version": installed_distribution.version,
                "requires_dist": sorted(str(item) for item in requires),
                **record_identity,
            }
            queue.extend(requires)
    except BootstrapError:
        raise
    except Exception as exc:
        raise BootstrapError("DEPENDENCY_IDENTITY_INVALID", str(exc)) from exc
    for record in allowed_files.values():
        owners = sorted(record["owners"], key=_canonical_json)
        owner_names = [str(owner["distribution_name"]) for owner in owners]
        if len(owner_names) != len(set(owner_names)):
            raise BootstrapError("DEPENDENCY_IDENTITY_INVALID", "duplicate payload owner")
        installed_names = {
            str(row["distribution_name"])
            for row in installed_claimants.get(str(record["environment_member"]), ())
        }
        if installed_names != set(owner_names):
            raise BootstrapError(
                "DEPENDENCY_IDENTITY_INVALID",
                "installed claimant set differs from active owner set: "
                f"{record['environment_member']}",
            )
        record["owners"] = owners
    closure_rows = [closure[name] for name in sorted(closure)]
    payload_ownership = sorted(
        allowed_files.values(),
        key=lambda item: str(item["environment_member"]).encode("utf-8"),
    )
    payload_ownership_sha256 = _hash_bytes(_canonical_json(payload_ownership))
    return {
        "root_requirement_contract": root_contract,
        "dependency_identity": {
            "schema": "gold-installed-dependency-closure-v1",
            "distributions": closure_rows,
            "installed_claimant_inventory": inventory_seal,
            "payload_ownership_schema": "gold-record-payload-ownership-v1",
            "payload_ownership": payload_ownership,
            "payload_ownership_sha256": payload_ownership_sha256,
            "sha256": _hash_bytes(
                _canonical_json(
                    {
                        "distributions": closure_rows,
                        "installed_claimant_inventory": inventory_seal,
                        "payload_ownership_schema": "gold-record-payload-ownership-v1",
                        "payload_ownership_sha256": payload_ownership_sha256,
                    }
                )
            ),
        },
        "allowed_files": allowed_files,
    }


class _ResourceTracker:
    """Audit RECORD-owned runtime resources and deny additions after sealing."""

    def __init__(
        self,
        allowed_files: Mapping[str, Mapping[str, object]],
        site_roots: tuple[Path, ...],
        private_root: Path,
    ):
        self.allowed_files = dict(allowed_files)
        self.site_roots = site_roots
        self.private_root = private_root
        self.opened: dict[str, dict[str, object]] = {}
        self.sealed_paths: frozenset[str] | None = None
        self.suspend_depth = 0
        self.font_bootstrap = True
        self.stdlib_roots = tuple(
            Path(value).resolve()
            for key in ("stdlib", "platstdlib")
            if (value := sysconfig.get_path(key))
        )
        self.executable = Path(sys.executable).resolve()
        self.system_runtime_roots = tuple(
            path for path in (Path("/usr/lib"), Path("/lib"), Path("/lib64")) if path.exists()
        )
        self.font_directories = tuple(
            path.parent
            for path_string, record in self.allowed_files.items()
            if any(
                owner["distribution_name"] == "matplotlib" for owner in record["owners"]
            )
            and (path := Path(path_string)).suffix.lower() in _FONT_SUFFIXES
        )

    @contextlib.contextmanager
    def suspended(self):
        self.suspend_depth += 1
        try:
            yield
        finally:
            self.suspend_depth -= 1

    def __call__(self, event: str, args: tuple[object, ...]) -> None:
        if self.suspend_depth or not args:
            return
        if event == "subprocess.Popen" and self.font_bootstrap:
            raise PermissionError("external font discovery is disabled")
        if event == "os.scandir" and self.font_bootstrap:
            try:
                directory = Path(os.fsdecode(args[0])).absolute().resolve(strict=False)
            except (OSError, TypeError, ValueError):
                raise PermissionError("unbound font directory") from None
            if not any(
                directory == root or root in directory.parents for root in self.font_directories
            ):
                raise PermissionError(f"unbound font directory: {directory}")
            return
        if event != "open":
            return
        raw_path = args[0]
        if not isinstance(raw_path, (str, bytes, os.PathLike)):
            return
        mode = args[1] if len(args) > 1 else None
        flags = args[2] if len(args) > 2 else 0
        if isinstance(mode, str):
            if not any(marker in mode for marker in ("r", "+")):
                return
        elif isinstance(flags, int) and flags & os.O_ACCMODE == os.O_WRONLY:
            return
        try:
            path = Path(os.fsdecode(raw_path)).absolute().resolve(strict=False)
        except (OSError, TypeError, ValueError):
            return
        key = str(path)
        record = self.allowed_files.get(key)
        if record is not None:
            if path.suffix.lower() not in _CODE_SUFFIXES:
                if self.sealed_paths is not None and key not in self.sealed_paths:
                    raise BootstrapError(
                        "EXECUTION_RESOURCE_UNBOUND", f"new resource opened after seal: {path}"
                    )
                self.opened[key] = dict(record)
            return
        if path == self.private_root or self.private_root in path.parents:
            return
        if path == Path("/proc/self/maps"):
            return
        if path == self.executable:
            return
        if _inside(path, self.stdlib_roots) and path.suffix.lower() in _CODE_SUFFIXES:
            return
        if _inside(path, self.system_runtime_roots) and (
            path.suffix.lower() in _CODE_SUFFIXES or ".so" in path.name
        ):
            return
        raise BootstrapError(
            "EXECUTION_RESOURCE_UNBOUND", f"resource is not RECORD-bound: {path}"
        )

    def seal(self) -> None:
        self.sealed_paths = frozenset(self.opened)

    def rows(self) -> list[dict[str, object]]:
        return sorted(
            (dict(record) for record in self.opened.values()),
            key=lambda row: str(row["environment_member"]).encode("utf-8"),
        )


def _install_matplotlib_policy(
    capture: Mapping[str, object], tracker: _ResourceTracker
) -> None:
    allowed_font_paths = {
        path
        for path, record in capture["allowed_files"].items()
        if any(owner["distribution_name"] == "matplotlib" for owner in record["owners"])
        and Path(path).suffix.lower() in _FONT_SUFFIXES
    }
    if not allowed_font_paths:
        raise BootstrapError("EXECUTION_RESOURCE_UNBOUND", "Matplotlib RECORD contains no fonts")
    allowed_directories = tuple(sorted({Path(path).parent for path in allowed_font_paths}, key=str))
    original_walk = os.walk
    import subprocess

    original_check_output = subprocess.check_output

    def record_only_walk(top, *args, **kwargs):
        try:
            directory = Path(os.fsdecode(top)).absolute().resolve(strict=False)
        except (OSError, TypeError, ValueError):
            return iter(())
        if not any(directory == root or root in directory.parents for root in allowed_directories):
            return iter(())
        return original_walk(top, *args, **kwargs)

    def no_fontconfig(*args, **kwargs):
        return b""

    os.walk = record_only_walk
    subprocess.check_output = no_fontconfig
    try:
        import matplotlib
        from matplotlib import font_manager

        default_rc = Path(matplotlib.matplotlib_fname()).resolve()
        rc_record = capture["allowed_files"].get(str(default_rc))
        if rc_record is None or not any(
            owner["distribution_name"] == "matplotlib" for owner in rc_record["owners"]
        ):
            raise BootstrapError(
                "EXECUTION_RESOURCE_UNBOUND",
                f"default matplotlibrc is not RECORD-bound: {default_rc}",
            )
        private_manager = font_manager.FontManager()
        private_manager.ttflist = [
            entry
            for entry in private_manager.ttflist
            if str(Path(entry.fname).resolve()) in allowed_font_paths
        ]
        private_manager.afmlist = [
            entry
            for entry in private_manager.afmlist
            if str(Path(entry.fname).resolve()) in allowed_font_paths
        ]
        if not private_manager.ttflist:
            raise BootstrapError(
                "EXECUTION_RESOURCE_UNBOUND", "private Matplotlib resolver contains no fonts"
            )
        font_manager.fontManager = private_manager
        font_manager.findfont = private_manager.findfont
        capture["matplotlib_default_rc"] = str(default_rc)
        capture["matplotlib_font_paths"] = frozenset(allowed_font_paths)
    finally:
        subprocess.check_output = original_check_output
        os.walk = original_walk


def _capture(header: Mapping[str, object]) -> dict[str, object]:
    provenance = _validate_provenance(header.get("source_provenance"))
    site_roots = _site_roots(header.get("package_site_roots"))
    distributions = _distribution_map(site_roots)
    first_party = _unique_distribution(distributions, "gold-quant-research") if provenance["mode"] == "package" else None
    if first_party is not None:
        files, stats = _package_files(first_party)
        source_root = None
    else:
        source_root, files, stats = _release_files(provenance["source_root"])
    source_identity = _source_identity(files)
    expected = provenance.get("expected_source_sha256")
    if expected is not None and source_identity["sha256"] != expected:
        raise BootstrapError("SOURCE_CONTENT_MISMATCH", "release source digest does not match")

    for root in reversed(site_roots):
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
    environment = _dependency_environment(provenance, source_root, site_roots)
    return {
        "provenance": provenance,
        "files": files,
        "stats": stats,
        "source_identity": source_identity,
        "source_root": str(source_root) if source_root is not None else None,
        **environment,
        "site_roots": [str(path) for path in site_roots],
    }


class _MemoryLoader(importlib.abc.Loader):
    def __init__(self, member: str, source: bytes, is_package: bool):
        self.member = member
        self.source = source
        self.is_package = is_package

    def create_module(self, spec):
        return None

    def exec_module(self, module) -> None:
        module.__file__ = f"provenance://{self.member}"
        module.__provenance_member__ = self.member
        if self.is_package:
            module.__path__ = [f"provenance://{self.member.rsplit('/', 1)[0]}"]
        code = compile(self.source, module.__file__, "exec", dont_inherit=True)
        exec(code, module.__dict__)


class _MemoryFinder(importlib.abc.MetaPathFinder):
    def __init__(self, files: Mapping[str, bytes]):
        modules: dict[str, tuple[str, bytes, bool]] = {}
        for member, source in files.items():
            if not member.endswith(".py") or not member.startswith("src/"):
                continue
            relative = member[4:]
            parts = relative.split("/")
            if parts[-1] == "__init__.py":
                name = ".".join(parts[:-1])
                is_package = True
            else:
                name = ".".join(parts)[:-3]
                is_package = False
            modules[name] = (member, source, is_package)
        self.modules = modules

    def find_spec(self, fullname, path=None, target=None):
        found = self.modules.get(fullname)
        if found is None:
            if fullname.split(".", 1)[0] in {"gold_research", "quant_platform"}:
                raise BootstrapError("SOURCE_SET_MISMATCH", f"unlisted first-party import: {fullname}")
            return None
        member, source, is_package = found
        loader = _MemoryLoader(member, source, is_package)
        return importlib.util.spec_from_loader(fullname, loader, origin=f"provenance://{member}", is_package=is_package)


class _VerifiedPathFinder(importlib.abc.MetaPathFinder):
    def __init__(self, allowed_files: Mapping[str, object], site_roots: tuple[Path, ...]):
        self.allowed_files = allowed_files
        self.site_roots = site_roots

    def find_spec(self, fullname, path=None, target=None):
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None:
            return None
        if spec.origin is None:
            locations = list(spec.submodule_search_locations or [])
            if any(_inside(Path(item).resolve(), self.site_roots) for item in locations):
                raise BootstrapError("UNVERIFIED_LOADED_MODULE", f"namespace package rejected: {fullname}")
            return spec
        if spec.origin in {"built-in", "frozen"}:
            return spec
        origin = Path(spec.origin).resolve()
        if _inside(origin, self.site_roots) and str(origin) not in self.allowed_files:
            raise BootstrapError("UNVERIFIED_LOADED_MODULE", f"unverified import: {fullname}")
        return spec


def _runtime_identity() -> dict[str, object]:
    executable = Path(sys.executable).resolve()
    executable_bytes, _ = _safe_read(executable, "RUNTIME_IDENTITY_INVALID")
    library = None
    library_reason = "libpython-not-present"
    library_name = sysconfig.get_config_var("LDLIBRARY")
    library_dir = sysconfig.get_config_var("LIBDIR")
    if library_name and library_dir:
        candidate = Path(library_dir) / library_name
        if candidate.is_file():
            data, _ = _safe_read(candidate, "RUNTIME_IDENTITY_INVALID")
            library = {"sha256": _hash_bytes(data), "size": len(data)}
            library_reason = None
    import platform

    libc_name, libc_version = platform.libc_ver()
    payload = {
        "implementation": sys.implementation.name,
        "cache_tag": sys.implementation.cache_tag,
        "version": sys.version,
        "abiflags": getattr(sys, "abiflags", ""),
        "executable": {"sha256": _hash_bytes(executable_bytes), "size": len(executable_bytes)},
        "libpython": library,
        "libpython_reason": library_reason,
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "libc_name": libc_name,
            "libc_version": libc_version,
        },
    }
    return {**payload, "sha256": _hash_bytes(_canonical_json(payload))}


def _process_identity(private_root: Path) -> dict[str, object]:
    unknown = set(os.environ) - _FIXED_ENV_KEYS
    if unknown:
        raise BootstrapError("EXECUTION_RESOURCE_UNBOUND", f"unknown environment keys: {sorted(unknown)}")
    environment = {
        key: ("$PRIVATE_ROOT" + value[len(str(private_root)) :] if value.startswith(str(private_root)) else value)
        for key, value in sorted(os.environ.items())
    }
    current_umask = os.umask(0)
    os.umask(current_umask)
    payload = {
        "environment": environment,
        "locale": os.environ.get("LC_ALL"),
        "timezone": os.environ.get("TZ"),
        "umask": f"{current_umask:04o}",
        "cwd_policy": "empty-private-root",
        "argv_flags": ["-B", "-P", "-s", "-S"],
    }
    return {**payload, "sha256": _hash_bytes(_canonical_json(payload))}


def _recapture_environment(context: Mapping[str, object], private_root: Path) -> dict[str, object]:
    source_root_value = context.get("source_root")
    source_root = Path(str(source_root_value)) if source_root_value is not None else None
    environment = _dependency_environment(
        context["provenance"],
        source_root,
        tuple(Path(item) for item in context["site_roots"]),
    )
    return {
        **environment,
        "runtime_identity": _runtime_identity(),
        "process_identity": _process_identity(private_root),
    }


def _install_import_policy(capture: Mapping[str, object]) -> None:
    contaminated = sorted(
        name
        for name in sys.modules
        if name == "gold_research"
        or name.startswith("gold_research.")
        or name == "quant_platform"
        or name.startswith("quant_platform.")
    )
    if contaminated:
        raise BootstrapError(
            "LOADED_CODE_UNBOUND", f"first-party modules were preloaded: {contaminated}"
        )
    memory = _MemoryFinder(capture["files"])
    guard = _VerifiedPathFinder(
        capture["allowed_files"],
        tuple(Path(item) for item in capture["site_roots"]),
    )
    path_finder_index = sys.meta_path.index(importlib.machinery.PathFinder)
    sys.meta_path.insert(path_finder_index, memory)
    sys.meta_path.insert(path_finder_index + 1, guard)


def main() -> int:
    work_root = Path.cwd().resolve()
    private_root = work_root.parent
    if any(work_root.iterdir()):
        raise BootstrapError("EXECUTION_RESOURCE_UNBOUND", "worker cwd was not empty at startup")
    os.umask(0o077)
    header_line = sys.stdin.buffer.readline()
    if not header_line:
        raise BootstrapError("PROVENANCE_AUTHORITY_AMBIGUOUS", "missing worker header")
    try:
        header = json.loads(header_line)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise BootstrapError("PROVENANCE_AUTHORITY_AMBIGUOUS", "invalid worker header") from exc
    capture = _capture(header)
    capture["runtime_identity"] = _runtime_identity()
    capture["process_identity"] = _process_identity(private_root)
    observed_pathless = frozenset(
        name
        for name, module in sys.modules.items()
        if getattr(module, "__file__", None) is None
        and getattr(getattr(module, "__spec__", None), "origin", None) is None
    )
    if not observed_pathless <= _BOOTSTRAP_PATHLESS_MODULES:
        raise BootstrapError(
            "UNVERIFIED_LOADED_MODULE",
            f"unexpected pathless bootstrap modules: {sorted(observed_pathless - _BOOTSTRAP_PATHLESS_MODULES)}",
        )
    capture["bootstrap_pathless_modules"] = _BOOTSTRAP_PATHLESS_MODULES
    _install_import_policy(capture)
    tracker = _ResourceTracker(
        capture["allowed_files"],
        tuple(Path(item) for item in capture["site_roots"]),
        private_root,
    )
    capture["resource_tracker"] = tracker
    def recapture_environment():
        with tracker.suspended():
            return _recapture_environment(capture, private_root)

    capture["recapture_environment"] = recapture_environment
    sys.addaudithook(tracker)
    sys._gold_round4_provenance_context = capture
    request = pickle.loads(sys.stdin.buffer.read())
    _install_matplotlib_policy(capture, tracker)
    from gold_research.round4 import _run_round4_worker

    tracker.font_bootstrap = False
    result = _run_round4_worker(**request, _provenance_context=capture)
    sys.stdout.buffer.write(pickle.dumps(result, protocol=5))
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BootstrapError as exc:
        sys.stderr.write(json.dumps({"code": exc.code, "detail": exc.detail}, sort_keys=True) + "\n")
        raise SystemExit(2)
    except Exception as exc:
        code = getattr(exc, "code", "WORKER_FAILED")
        detail = getattr(exc, "detail", str(exc))
        sys.stderr.write(
            json.dumps(
                {"code": code, "detail": detail, "exception_type": type(exc).__name__},
                sort_keys=True,
            )
            + "\n"
        )
        traceback.print_exc(file=sys.stderr)
        raise SystemExit(2)
