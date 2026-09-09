from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

import quant_platform.production_package_authority as authority_module
from quant_platform.production_jobs import ProductionInput, ProductionJobs
from quant_platform.production_package_authority import (
    FilesystemPackageIdentityAuthority,
    PackageIdentityAuthorityError,
    create_package_identity_authority_app,
)


RUN_ID = "a" * 64
PROJECT = Path(__file__).parents[1]


def _stage_input(work_root: Path, payload: bytes = b"old") -> Path:
    target = work_root / RUN_ID / "acquisition"
    target.mkdir(parents=True)
    for name, value in ProductionJobs.input_payloads(
        ProductionInput(
            "provider-get",
            {"method": "GET", "provider_url": "fixture://authority"},
            payload,
        )
    ).items():
        member = target / name
        member.write_bytes(value)
        member.chmod(0o440)
    target.chmod(0o550)
    return target


def test_authority_create_once_replay_and_conflict_are_observable(tmp_path: Path) -> None:
    work_root = tmp_path / "work"
    identity_root = tmp_path / "identities"
    target = _stage_input(work_root)
    authority = FilesystemPackageIdentityAuthority(work_root, identity_root)

    retained = authority.seal(RUN_ID, "acquisition")
    assert authority.seal(RUN_ID, "acquisition") == retained

    identity_path = identity_root / RUN_ID / "acquisition.sha256"
    assert identity_path.read_text() == retained + "\n"
    assert stat.S_IMODE(identity_root.stat().st_mode) == 0o700
    assert stat.S_IMODE(identity_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(identity_path.stat().st_mode) == 0o400
    assert identity_path.stat().st_uid == os.geteuid()

    target.chmod(0o750)
    raw = target / "raw.bin"
    raw.chmod(0o640)
    raw.write_bytes(b"new")
    raw.chmod(0o440)
    target.chmod(0o550)

    with pytest.raises(PackageIdentityAuthorityError, match="conflicts"):
        authority.seal(RUN_ID, "acquisition")
    assert identity_path.read_text() == retained + "\n"


def test_authority_interruption_before_atomic_publication_can_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work_root = tmp_path / "work"
    identity_root = tmp_path / "identities"
    target = _stage_input(work_root)
    authority = FilesystemPackageIdentityAuthority(work_root, identity_root)
    identity_path = identity_root / RUN_ID / "acquisition.sha256"

    def interrupt_before_publication(source: Path, destination: Path) -> None:
        assert source.read_bytes().endswith(b"\n")
        assert stat.S_IMODE(source.stat().st_mode) == 0o400
        assert not destination.exists()
        raise SystemExit(91)

    monkeypatch.setattr(authority_module, "_rename_noreplace", interrupt_before_publication)
    with pytest.raises(SystemExit, match="91"):
        authority.seal(RUN_ID, "acquisition")
    assert not identity_path.exists()

    monkeypatch.undo()
    retained = FilesystemPackageIdentityAuthority(work_root, identity_root).seal(
        RUN_ID, "acquisition"
    )
    assert FilesystemPackageIdentityAuthority(work_root, identity_root).seal(
        RUN_ID, "acquisition"
    ) == retained

    target.chmod(0o750)
    raw = target / "raw.bin"
    raw.chmod(0o640)
    raw.write_bytes(b"new")
    raw.chmod(0o440)
    target.chmod(0o550)
    with pytest.raises(PackageIdentityAuthorityError, match="conflicts"):
        FilesystemPackageIdentityAuthority(work_root, identity_root).seal(
            RUN_ID, "acquisition"
        )


def test_authority_http_interface_has_no_identity_selection_or_reset_operation(tmp_path: Path) -> None:
    work_root = tmp_path / "work"
    _stage_input(work_root)
    authority = FilesystemPackageIdentityAuthority(work_root, tmp_path / "identities")
    client = TestClient(create_package_identity_authority_app(authority))

    response = client.post(f"/v1/generations/{RUN_ID}/acquisition/seal", content=b"")
    assert response.status_code == 200
    assert set(response.json()) == {"package_identity"}
    chosen = client.post(
        f"/v1/generations/{RUN_ID}/acquisition/seal", content=b"0" * 64
    )
    assert chosen.status_code == 200
    assert chosen.json() == response.json()
    assert client.put(f"/v1/generations/{RUN_ID}/acquisition/seal", content=b"0" * 64).status_code == 405
    assert client.delete(f"/v1/generations/{RUN_ID}/acquisition/seal").status_code == 405
    assert client.post(f"/v1/generations/{RUN_ID}/other/seal", content=b"").status_code == 409


def test_compose_places_authority_under_a_distinct_uid_and_private_mount() -> None:
    compose = yaml.safe_load((PROJECT / "production" / "compose.yaml").read_text())
    api = compose["services"]["production-api"]
    authority = compose["services"]["package-identity-authority"]

    assert api["user"].split(":", 1)[0] == "10001"
    assert authority["user"].split(":", 1)[0] == "10002"
    assert authority["cap_drop"] == ["ALL"]
    assert authority["security_opt"] == ["no-new-privileges:true"]
    assert authority["networks"] == ["package_identity_internal"]
    assert "package_identity_internal" in api["networks"]

    api_mounts = {item["target"]: item for item in api["volumes"]}
    authority_mounts = {item["target"]: item for item in authority["volumes"]}
    assert api_mounts["/run/quantresearch/work"]["read_only"] is False
    assert "/var/lib/quantresearch/package-identities" not in api_mounts
    assert authority_mounts["/run/quantresearch/work"]["read_only"] is True
    assert authority_mounts["/var/lib/quantresearch/package-identities"]["read_only"] is False
    assert (
        authority_mounts["/run/quantresearch/work"]["source"]
        == api_mounts["/run/quantresearch/work"]["source"]
    )
