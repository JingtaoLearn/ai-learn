"""Shared pytest fixtures for EMS tests."""

from __future__ import annotations

import asyncio
import math
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import AsyncGenerator
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.database import init_db, insert_embedding, insert_experience
from app.main import app

# Use a fixed 3072-dim normalized vector for mocking
MOCK_EMBEDDING_DIM = 3072


def make_mock_embedding(seed: int = 0) -> list[float]:
    """Return a deterministic normalized vector."""
    raw = [math.sin(i + seed * 0.1) for i in range(MOCK_EMBEDDING_DIM)]
    magnitude = math.sqrt(sum(x * x for x in raw))
    return [x / magnitude for x in raw]


MOCK_EMBEDDING = make_mock_embedding(0)

# Targets to patch — every module that does `from app.embedding import get_embedding`
_EMBEDDING_PATCH_TARGETS = [
    "app.embedding.get_embedding",
    "app.routers.experiences.get_embedding",
    "app.routers.learn.get_embedding",
    "app.services.retrieval.get_embedding",
    "app.services.dedup.get_embedding",
]


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def tmp_db(tmp_path) -> str:
    db_path = str(tmp_path / "test_ems.db")
    init_db(db_path)
    return db_path


@pytest.fixture
def db_conn(tmp_db) -> sqlite3.Connection:
    from app.database import get_connection
    conn = get_connection(tmp_db)
    yield conn
    conn.close()


@pytest.fixture
def sample_experience_data() -> dict:
    return {
        "content": "Never merge PRs without review",
        "category": "guardrail",
        "severity": "block",
        "tags": ["git", "pr"],
        "scope": {"project": "all"},
        "source": "test",
    }


def insert_test_experience(
    conn: sqlite3.Connection,
    content: str = "Test experience",
    category: str = "guardrail",
    severity: str = "warn",
    tags: list[str] | None = None,
    source: str = "test",
    embedding_seed: int = 0,
) -> str:
    """Helper to insert a test experience with a mock embedding."""
    exp_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    insert_experience(
        conn,
        exp_id=exp_id,
        content=content,
        category=category,
        severity=severity,
        tags=tags or [],
        scope={},
        source=source,
        status="active",
        created_at=now,
        updated_at=now,
    )
    insert_embedding(conn, exp_id, make_mock_embedding(embedding_seed))
    conn.commit()
    return exp_id


def _make_embedding_patches(return_value=None):
    """Return a list of patch context managers for all embedding call sites."""
    rv = return_value if return_value is not None else MOCK_EMBEDDING
    return [patch(target, new=AsyncMock(return_value=rv)) for target in _EMBEDDING_PATCH_TARGETS]


@pytest_asyncio.fixture
async def client(tmp_db) -> AsyncGenerator[AsyncClient, None]:
    """AsyncClient wired to the FastAPI app with a temporary database."""
    import app.database as db_module

    patches = [
        patch.object(db_module, "DB_PATH", tmp_db),
        patch("app.routers.check.DB_PATH", tmp_db),
        patch("app.routers.experiences.DB_PATH", tmp_db),
        patch("app.routers.learn.DB_PATH", tmp_db),
        patch("app.routers.search.DB_PATH", tmp_db),
        patch("app.routers.tags.DB_PATH", tmp_db),
    ] + _make_embedding_patches()

    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], \
         patches[6], patches[7], patches[8], patches[9], patches[10]:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            yield ac
