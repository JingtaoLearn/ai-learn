"""Tests for POST /api/check."""

from __future__ import annotations

import math
import struct
from unittest.mock import AsyncMock, patch

import pytest

from tests.conftest import MOCK_EMBEDDING, insert_test_experience, make_mock_embedding


@pytest.mark.asyncio
async def test_check_returns_pass_when_empty(client):
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
    response = await client.post(
        "/api/check",
        json={"action": "send an email with credentials"},
    )
    body = response.json()
    assert "verdict" in body["data"]
    assert "matches" in body["data"]
    assert "message" in body["data"]
    assert body["data"]["verdict"] in ("block", "warn", "pass")


@pytest.mark.asyncio
async def test_check_with_context(client):
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
async def test_check_returns_block_for_very_similar_block_experience(client, tmp_db):
    """Insert a block experience and query with the SAME embedding to trigger block verdict."""
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

    # Mock embedding to return the EXACT same vector as stored
    with patch("app.embedding.get_embedding", new=AsyncMock(return_value=MOCK_EMBEDDING)):
        response = await client.post(
            "/api/check",
            json={"action": "merge this PR now"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    # Similarity of identical normalized vectors = 1.0, so should be block
    assert body["data"]["verdict"] == "block"
