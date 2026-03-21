"""Tests for POST /api/experiences/search and GET /api/tags."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from tests.conftest import MOCK_EMBEDDING


@pytest.mark.asyncio
async def test_search_empty_db(client):
    response = await client.post(
        "/api/experiences/search",
        json={"query": "anything"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"] == []


@pytest.mark.asyncio
async def test_search_returns_results(client, tmp_db):
    """Insert an experience then search for it by returning the same embedding."""
    from app.database import get_connection, insert_embedding, insert_experience
    import uuid
    from datetime import datetime

    exp_id = str(uuid.uuid4())
    now = datetime.utcnow()
    conn = get_connection(tmp_db)
    try:
        insert_experience(
            conn,
            exp_id=exp_id,
            content="Never expose credentials in logs",
            category="guardrail",
            severity="block",
            tags=["security", "logging"],
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

    with patch("app.embedding.get_embedding", new=AsyncMock(return_value=MOCK_EMBEDDING)):
        response = await client.post(
            "/api/experiences/search",
            json={"query": "logging credentials", "similarity_threshold": 0.0},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert len(body["data"]) >= 1
    assert body["data"][0]["experience"]["id"] == exp_id


@pytest.mark.asyncio
async def test_search_with_tag_filter(client, tmp_db):
    from app.database import get_connection, insert_embedding, insert_experience
    import uuid
    from datetime import datetime
    from tests.conftest import make_mock_embedding

    conn = get_connection(tmp_db)
    try:
        for i, tag in enumerate(["security", "deployment"]):
            exp_id = str(uuid.uuid4())
            now = datetime.utcnow()
            insert_experience(
                conn,
                exp_id=exp_id,
                content=f"Experience about {tag}",
                category="lesson",
                severity="info",
                tags=[tag],
                scope={},
                source="test",
                status="active",
                created_at=now,
                updated_at=now,
            )
            insert_embedding(conn, exp_id, make_mock_embedding(i))
        conn.commit()
    finally:
        conn.close()

    with patch("app.embedding.get_embedding", new=AsyncMock(return_value=MOCK_EMBEDDING)):
        response = await client.post(
            "/api/experiences/search",
            json={"query": "test", "tags": ["security"], "similarity_threshold": 0.0},
        )

    body = response.json()
    assert body["success"] is True
    for result in body["data"]:
        assert "security" in result["experience"]["tags"]


@pytest.mark.asyncio
async def test_search_validates_limit(client):
    response = await client.post(
        "/api/experiences/search",
        json={"query": "test", "limit": 0},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_tags_empty(client):
    response = await client.get("/api/tags")
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"] == []


@pytest.mark.asyncio
async def test_tags_with_experiences(client):
    await client.post(
        "/api/experiences",
        json={
            "content": "Tag test experience",
            "category": "lesson",
            "severity": "info",
            "tags": ["git", "testing"],
            "scope": {},
            "source": "test",
        },
    )

    response = await client.get("/api/tags")
    body = response.json()
    assert body["success"] is True
    tags = {item["tag"]: item["count"] for item in body["data"]}
    assert "git" in tags
    assert "testing" in tags
