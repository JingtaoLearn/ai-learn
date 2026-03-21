"""Tests for POST /api/learn and POST /api/learn/confirm."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


MOCK_SUMMARIZE_RESULT = {
    "content": "Always run tests before pushing to the main branch.",
    "category": "practice",
    "severity": "warn",
    "tags": ["testing", "git", "ci"],
    "reasoning": "This is a best practice for maintaining code quality.",
}

MOCK_SUMMARIZE_LESSON = {
    "content": "Generic lesson learned from experience.",
    "category": "lesson",
    "severity": "info",
    "tags": ["general"],
    "reasoning": "No specific category or severity indicated.",
}

MOCK_SUMMARIZE_OVERRIDE = {
    "content": "AI-refined overridden content.",
    "category": "guardrail",
    "severity": "block",
    "tags": ["override"],
    "reasoning": "Classified as guardrail based on user hints.",
}


@pytest.mark.asyncio
async def test_learn_creates_draft(client):
    with patch("app.routers.learn.summarize_experience", new=AsyncMock(return_value=MOCK_SUMMARIZE_RESULT)):
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
    assert "ai_reasoning" in data
    assert "original_content" in data
    # AI should have refined the content
    assert data["experience"]["content"] == "Always run tests before pushing to the main branch."
    assert data["original_content"] == "Always run tests before pushing to main"


@pytest.mark.asyncio
async def test_learn_ai_infers_defaults(client):
    """When user provides no category/severity/tags, AI should infer them."""
    with patch("app.routers.learn.summarize_experience", new=AsyncMock(return_value=MOCK_SUMMARIZE_LESSON)):
        response = await client.post(
            "/api/learn",
            json={"content": "Some generic lesson learned"},
        )
    assert response.status_code == 200
    body = response.json()
    data = body["data"]
    # AI inferred defaults
    assert data["experience"]["category"] == "lesson"
    assert data["experience"]["severity"] == "info"
    assert data["experience"]["tags"] == ["general"]
    assert data["ai_reasoning"] != ""


@pytest.mark.asyncio
async def test_learn_user_hints_override_ai(client):
    """User-provided category/severity should take precedence over AI."""
    mock_result = {
        "content": "Refined: never send secrets via email",
        "category": "lesson",  # AI would say lesson
        "severity": "info",     # AI would say info
        "tags": ["security", "email"],
        "reasoning": "test",
    }
    with patch("app.routers.learn.summarize_experience", new=AsyncMock(return_value=mock_result)):
        response = await client.post(
            "/api/learn",
            json={
                "content": "don't send secrets via email",
                "category": "guardrail",  # User overrides
                "severity": "block",       # User overrides
            },
        )
    assert response.status_code == 200
    data = response.json()["data"]
    # User overrides should win
    assert data["experience"]["category"] == "guardrail"
    assert data["experience"]["severity"] == "block"
    # But AI refined the content
    assert data["experience"]["content"] == "Refined: never send secrets via email"


@pytest.mark.asyncio
async def test_confirm_draft(client):
    with patch("app.routers.learn.summarize_experience", new=AsyncMock(return_value=MOCK_SUMMARIZE_RESULT)):
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
    with patch("app.routers.learn.summarize_experience", new=AsyncMock(return_value=MOCK_SUMMARIZE_LESSON)):
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
    with patch("app.routers.learn.summarize_experience", new=AsyncMock(return_value=MOCK_SUMMARIZE_LESSON)):
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
