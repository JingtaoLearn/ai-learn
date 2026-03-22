"""Tests for POST /api/check."""

from __future__ import annotations

import math
import struct
from unittest.mock import AsyncMock, patch

import pytest

from tests.conftest import MOCK_EMBEDDING, insert_test_experience, make_mock_embedding


MOCK_LLM_PASS = {
    "judgments": [],
    "verdict": "pass",
    "summary": "No relevant experiences apply to this action.",
}

MOCK_LLM_BLOCK = {
    "judgments": [
        {
            "experience_id": "test",
            "applies": True,
            "relevance": "high",
            "reasoning": "This action directly violates the guardrail.",
        }
    ],
    "verdict": "block",
    "summary": "Action is blocked: violates merge guardrail.",
}


@pytest.mark.asyncio
async def test_check_returns_pass_when_empty(client):
    with patch("app.services.llm.judge_action", new=AsyncMock(return_value=MOCK_LLM_PASS)):
        response = await client.post(
            "/api/check",
            json={"action": "do something completely unrelated"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"]["verdict"] == "pass"
    assert body["data"]["matches"] == []


@pytest.mark.asyncio
async def test_check_with_tags(client):
    with patch("app.services.llm.judge_action", new=AsyncMock(return_value=MOCK_LLM_PASS)):
        response = await client.post(
            "/api/check",
            json={
                "action": "merge a PR without review",
                "tags": ["git", "pr"],
            },
        )
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    # With empty DB, verdict must be pass
    assert body["data"]["verdict"] == "pass"


@pytest.mark.asyncio
async def test_check_verdict_structure(client):
    with patch("app.services.llm.judge_action", new=AsyncMock(return_value=MOCK_LLM_PASS)):
        response = await client.post(
            "/api/check",
            json={"action": "send an email with credentials"},
        )
    body = response.json()
    assert "verdict" in body["data"]
    assert "matches" in body["data"]
    assert "message" in body["data"]
    assert "llm_judgment" in body["data"]
    assert body["data"]["verdict"] in ("block", "warn", "pass")


@pytest.mark.asyncio
async def test_check_with_context(client):
    with patch("app.services.llm.judge_action", new=AsyncMock(return_value=MOCK_LLM_PASS)):
        response = await client.post(
            "/api/check",
            json={
                "action": "deploy to production",
                "context": {"environment": "prod"},
                "tags": ["deployment"],
            },
        )
    assert response.status_code == 200
    assert response.json()["success"] is True


@pytest.mark.asyncio
async def test_check_returns_block_for_matching_experience(client, tmp_db):
    """Insert a block experience and verify LLM-based block verdict."""
    from app.database import get_connection, insert_embedding, insert_experience
    import uuid
    from datetime import datetime, timezone

    exp_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    conn = get_connection(tmp_db)
    try:
        insert_experience(
            conn,
            exp_id=exp_id,
            content="Never merge without approval",
            category="guardrail",
            severity="block",
            tags=["git"],
            scope={},
            source="test",
            status="active",
            created_at=now,
            updated_at=now,
        )
        insert_embedding(conn, exp_id, MOCK_EMBEDDING)
        conn.commit()
    finally:
        conn.close()

    # Mock embedding to return the EXACT same vector as stored (triggers retrieval)
    # Mock LLM judge to return block verdict
    with patch("app.embedding.get_embedding", new=AsyncMock(return_value=MOCK_EMBEDDING)), \
         patch("app.services.llm.judge_action", new=AsyncMock(return_value=MOCK_LLM_BLOCK)):
        response = await client.post(
            "/api/check",
            json={"action": "merge this PR now"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"]["verdict"] == "block"
    assert body["data"]["llm_judgment"]["verdict"] == "block"
    assert len(body["data"]["llm_judgment"]["judgments"]) > 0


@pytest.mark.asyncio
async def test_check_llm_judgment_included_in_response(client, tmp_db):
    """Verify that LLM judgment details are included in the response."""
    from app.database import get_connection, insert_embedding, insert_experience
    import uuid
    from datetime import datetime, timezone

    exp_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    conn = get_connection(tmp_db)
    try:
        insert_experience(
            conn,
            exp_id=exp_id,
            content="Always use git worktree for fix branches",
            category="lesson",
            severity="warn",
            tags=["git"],
            scope={},
            source="test",
            status="active",
            created_at=now,
            updated_at=now,
        )
        insert_embedding(conn, exp_id, MOCK_EMBEDDING)
        conn.commit()
    finally:
        conn.close()

    mock_warn = {
        "judgments": [
            {
                "experience_id": exp_id,
                "applies": True,
                "relevance": "medium",
                "reasoning": "The action involves checking out a branch which relates to the worktree lesson.",
            }
        ],
        "verdict": "warn",
        "summary": "Consider using git worktree instead of direct checkout.",
    }

    with patch("app.embedding.get_embedding", new=AsyncMock(return_value=MOCK_EMBEDDING)), \
         patch("app.services.llm.judge_action", new=AsyncMock(return_value=mock_warn)):
        response = await client.post(
            "/api/check",
            json={"action": "checkout fix branch in main repo"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["data"]["verdict"] == "warn"
    assert body["data"]["message"] == "Consider using git worktree instead of direct checkout."
    assert body["data"]["llm_judgment"]["judgments"][0]["applies"] is True
