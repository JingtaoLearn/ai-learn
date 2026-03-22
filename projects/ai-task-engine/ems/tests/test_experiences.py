"""Tests for CRUD experience endpoints."""

from __future__ import annotations

import pytest

from tests.conftest import insert_test_experience, MOCK_EMBEDDING


@pytest.mark.asyncio
async def test_list_experiences_empty(client):
    response = await client.get("/api/experiences")
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"] == []


@pytest.mark.asyncio
async def test_create_experience(client):
    payload = {
        "content": "Always write tests before merging",
        "category": "practice",
        "severity": "info",
        "tags": ["testing", "git"],
        "scope": {},
        "source": "test",
    }
    response = await client.post("/api/experiences", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"]["content"] == payload["content"]
    assert body["data"]["category"] == "practice"


@pytest.mark.asyncio
async def test_get_experience_by_id(client):
    # Create first
    payload = {
        "content": "Use semantic versioning for all releases",
        "category": "practice",
        "severity": "info",
        "tags": ["versioning"],
        "scope": {},
        "source": "test",
    }
    create_response = await client.post("/api/experiences", json=payload)
    exp_id = create_response.json()["data"]["id"]

    # Retrieve
    response = await client.get(f"/api/experiences/{exp_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"]["id"] == exp_id


@pytest.mark.asyncio
async def test_get_experience_not_found(client):
    response = await client.get("/api/experiences/nonexistent-id")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_update_experience(client):
    payload = {
        "content": "Document all public APIs",
        "category": "practice",
        "severity": "info",
        "tags": ["docs"],
        "scope": {},
        "source": "test",
    }
    create_response = await client.post("/api/experiences", json=payload)
    exp_id = create_response.json()["data"]["id"]

    # Update severity
    update_payload = {"severity": "warn"}
    response = await client.put(f"/api/experiences/{exp_id}", json=update_payload)
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"]["severity"] == "warn"


@pytest.mark.asyncio
async def test_update_nonexistent(client):
    response = await client.put(
        "/api/experiences/no-such-id", json={"severity": "warn"}
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_delete_experience_soft(client):
    payload = {
        "content": "Rotate API keys every 90 days",
        "category": "guardrail",
        "severity": "warn",
        "tags": ["security"],
        "scope": {},
        "source": "test",
    }
    create_response = await client.post("/api/experiences", json=payload)
    exp_id = create_response.json()["data"]["id"]

    # Soft delete
    response = await client.delete(f"/api/experiences/{exp_id}")
    assert response.status_code == 200
    assert response.json()["success"] is True

    # Should still be retrievable but deprecated
    get_response = await client.get(f"/api/experiences/{exp_id}")
    assert get_response.json()["data"]["status"] == "deprecated"


@pytest.mark.asyncio
async def test_delete_nonexistent(client):
    response = await client.delete("/api/experiences/ghost-id")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_list_with_filters(client):
    # Create two experiences with different categories
    for i, (cat, sev) in enumerate([("guardrail", "block"), ("lesson", "info")]):
        await client.post(
            "/api/experiences",
            json={
                "content": f"Experience {i}",
                "category": cat,
                "severity": sev,
                "tags": [],
                "scope": {},
                "source": "test",
            },
        )

    response = await client.get("/api/experiences?category=guardrail")
    assert response.status_code == 200
    data = response.json()["data"]
    assert all(e["category"] == "guardrail" for e in data)


@pytest.mark.asyncio
async def test_create_invalid_category(client):
    payload = {
        "content": "Bad category",
        "category": "unknown",
        "severity": "info",
        "tags": [],
        "scope": {},
        "source": "test",
    }
    response = await client.post("/api/experiences", json=payload)
    assert response.status_code == 422
