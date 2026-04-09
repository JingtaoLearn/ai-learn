#!/usr/bin/env python3
"""Seed script — populates the EMS database with initial experiences."""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from datetime import datetime, timezone

# Ensure the project root is on the path when run directly
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.database import DB_PATH, get_db, init_db, insert_embedding, insert_experience
from app.embedding import get_embedding

SEED_DATA = [
    {
        "content": "TeamClaw PRs: Zhang NEVER merges — only mark as ready for review",
        "category": "guardrail",
        "severity": "block",
        "tags": ["teamclaw", "pr", "git"],
        "scope": {"project": "teamclaw"},
        "source": "operational-policy",
    },
    {
        "content": "NEVER send sensitive information via email — no tokens, passwords, API keys",
        "category": "guardrail",
        "severity": "block",
        "tags": ["security", "email"],
        "scope": {"operation": "communication"},
        "source": "security-policy",
    },
    {
        "content": "NEVER put sensitive information in GitHub issues or PRs",
        "category": "guardrail",
        "severity": "block",
        "tags": ["security", "github"],
        "scope": {"tool": "github"},
        "source": "security-policy",
    },
    {
        "content": "PR merges require Jingtao approval — except todo-list project",
        "category": "guardrail",
        "severity": "warn",
        "tags": ["pr", "git"],
        "scope": {"operation": "merge"},
        "source": "team-policy",
    },
    {
        "content": "Never let secrets pass through agent context — always pipe directly",
        "category": "guardrail",
        "severity": "block",
        "tags": ["security", "secrets"],
        "scope": {"operation": "secrets-handling"},
        "source": "security-policy",
    },
    {
        "content": "Do the right things, not the fast things — follow proper standards",
        "category": "practice",
        "severity": "info",
        "tags": ["development", "standards"],
        "scope": {},
        "source": "engineering-principles",
    },
    {
        "content": "Never checkout fix branches in main repo — use git worktree",
        "category": "lesson",
        "severity": "warn",
        "tags": ["git", "deployment"],
        "scope": {"operation": "branching"},
        "source": "incident-retrospective",
    },
    {
        "content": "docker-compose same-name images overwrite each other — use COMPOSE_PROJECT_NAME",
        "category": "lesson",
        "severity": "warn",
        "tags": ["docker", "deployment"],
        "scope": {"tool": "docker"},
        "source": "incident-retrospective",
    },
]


async def seed(db_path: str = DB_PATH) -> None:
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    init_db(db_path)

    with get_db(db_path) as conn:
        # Check existing count
        existing_count = conn.execute(
            "SELECT COUNT(*) FROM experiences"
        ).fetchone()[0]

        if existing_count > 0:
            print(f"Database already has {existing_count} experience(s). Skipping seed.")
            return

    print(f"Seeding {len(SEED_DATA)} experience(s) into {db_path} ...")

    for i, item in enumerate(SEED_DATA, start=1):
        exp_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)

        print(f"  [{i}/{len(SEED_DATA)}] Embedding: {item['content'][:60]}...")
        embedding = await get_embedding(item["content"])

        with get_db(db_path) as conn:
            insert_experience(
                conn,
                exp_id=exp_id,
                content=item["content"],
                category=item["category"],
                severity=item["severity"],
                tags=item["tags"],
                scope=item["scope"],
                source=item["source"],
                status="active",
                created_at=now,
                updated_at=now,
            )
            insert_embedding(conn, exp_id, embedding)

        print(f"     -> Stored with id={exp_id}")

    print("Seed complete.")


if __name__ == "__main__":
    asyncio.run(seed())
