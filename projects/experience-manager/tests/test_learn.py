"""Tests for POST /api/learn and POST /api/learn/confirm."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_learn_creates_draft(client):
    response = await client.post(
        "/api/learn",
        json={
            "content": "Always run tests before pushing to main",
            "category": "practice",
            "severity": "warn",
            "tags": ["testing", "git"],
            "source": "test-learn",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    data = body["data"]
    assert "draft_id" in data
    assert "experience" in data
    assert "duplicates" in data
    assert "conflicts" in data
    assert "expires_at" in data


@pytest.mark.asyncio
async def test_learn_defaults(client):
    response = await client.post(
        "/api/learn",
        json={"content": "Some generic lesson learned"},
    )
    assert response.status_code == 200
    body = response.json()
    data = body["data"]
    # Defaults should be applied
    assert data["experience"]["category"] == "lesson"
    assert data["experience"]["severity"] == "info"


@pytest.mark.asyncio
async def test_confirm_draft(client):
    # Learn
    learn_response = await client.post(
        "/api/learn",
        json={
            "content": "Use meaningful commit messages",
            "category": "practice",
            "severity": "info",
            "tags": ["git"],
            "source": "confirm-test",
        },
    )
    draft_id = learn_response.json()["data"]["draft_id"]

    # Confirm
    confirm_response = await client.post(
        "/api/learn/confirm",
        json={"draft_id": draft_id},
    )
    assert confirm_response.status_code == 200
    body = confirm_response.json()
    assert body["success"] is True
    assert "id" in body["data"]


@pytest.mark.asyncio
async def test_confirm_with_overrides(client):
    learn_response = await client.post(
        "/api/learn",
        json={"content": "Original content", "source": "test"},
    )
    draft_id = learn_response.json()["data"]["draft_id"]

    confirm_response = await client.post(
        "/api/learn/confirm",
        json={
            "draft_id": draft_id,
            "overrides": {
                "content": "Overridden content",
                "category": "guardrail",
                "severity": "block",
                "tags": ["override"],
                "scope": {},
                "source": "overridden-source",
            },
        },
    )
    assert confirm_response.status_code == 200
    body = confirm_response.json()
    assert body["success"] is True


@pytest.mark.asyncio
async def test_confirm_nonexistent_draft(client):
    response = await client.post(
        "/api/learn/confirm",
        json={"draft_id": "no-such-draft-id"},
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_confirm_twice_fails(client):
    """Confirming the same draft twice should fail on the second attempt."""
    learn_response = await client.post(
        "/api/learn",
        json={"content": "One-time confirmation test"},
    )
    draft_id = learn_response.json()["data"]["draft_id"]

    await client.post("/api/learn/confirm", json={"draft_id": draft_id})

    second_response = await client.post(
        "/api/learn/confirm", json={"draft_id": draft_id}
    )
    assert second_response.status_code == 404
