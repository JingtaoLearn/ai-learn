from __future__ import annotations

import json

import pytest

from quant_platform.production_contract import (
    ProductionContractError,
    ProductionRelease,
    ProductionRequest,
    canonical_json_bytes,
    production_run_id,
    production_run_preimage,
)


BOCOM_MANIFEST = "6f9f10ed235c6229582ca2843c8b983a887dbdc0ac289170ca834e580bcae969"


def release() -> ProductionRelease:
    return ProductionRelease.from_mapping(
        {
            "schema": "quantresearch-production-release/v1",
            "production_api_image_digest": "a" * 64,
            "source_commit": "b" * 40,
            "source_tree_sha256": "c" * 64,
            "dependency_lock_sha256": "d" * 64,
            "effective_compose_config_sha256": "e" * 64,
            "provider_contract_sha256": "f" * 64,
            "api_contract_sha256": "0" * 64,
        }
    )


def test_request_and_release_identities_are_canonical() -> None:
    request = ProductionRequest.build(
        job_id="297c11cad0dc",
        scheduled_for="2026-03-09T00:40:00Z",
        production_manifest_sha256=BOCOM_MANIFEST,
    )

    assert ProductionRequest.from_bytes(request.canonical_body) == request
    assert request.request_id == request.expected_request_id
    assert len(request.canonical_body) == len(canonical_json_bytes(json.loads(request.canonical_body)))
    assert production_run_id(request.request_id, release().production_release_id) == production_run_id(
        request.request_id, release().production_release_id
    )


def test_normative_165_byte_run_id_vector() -> None:
    preimage = production_run_preimage("0" * 64, "1" * 64)

    assert len(preimage) == 165
    assert production_run_id("0" * 64, "1" * 64) == (
        "60ed25c043967205f1ee989bc814fdd23541a825a32091017908b4d973a95a90"
    )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda body: body | {"extra": True},
        lambda body: body | {"schema_version": True},
        lambda body: body | {"scheduled_for": "2026-03-09T08:40:00+08:00"},
        lambda body: body | {"request_id": "A" * 64},
    ],
)
def test_request_rejects_noncanonical_or_extra_fields(mutation) -> None:
    request = ProductionRequest.build(
        job_id="297c11cad0dc",
        scheduled_for="2026-03-09T00:40:00Z",
        production_manifest_sha256=BOCOM_MANIFEST,
    )

    with pytest.raises(ProductionContractError):
        ProductionRequest.from_mapping(mutation(request.body))


def test_strict_json_rejects_duplicate_keys_and_oversize() -> None:
    duplicate = (
        b'{"schema_version":1,"schema_version":1,"job_id":"297c11cad0dc",'
        b'"scheduled_for":"2026-03-09T00:40:00Z","production_manifest_sha256":"'
        + BOCOM_MANIFEST.encode()
        + b'","request_id":"'
        + b"0" * 64
        + b'"}'
    )
    with pytest.raises(ProductionContractError, match="duplicate"):
        ProductionRequest.from_bytes(duplicate)
    with pytest.raises(ProductionContractError, match="size"):
        ProductionRequest.from_bytes(b"{}" * 9000)
