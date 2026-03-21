"""POST /api/learn and POST /api/learn/confirm endpoints."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from app.database import DB_PATH, get_connection, insert_experience, insert_embedding
from app.embedding import get_embedding
from app.models import (
    ApiResponse,
    ConfirmRequest,
    DraftExperience,
    ExperienceCreate,
    LearnRequest,
)
from app.services.dedup import find_duplicates_and_conflicts

router = APIRouter(tags=["learn"])

# In-memory draft store: draft_id -> DraftExperience
_drafts: dict[str, DraftExperience] = {}

DRAFT_TTL_HOURS = 1


def _purge_expired() -> None:
    now = datetime.utcnow()
    expired = [k for k, v in _drafts.items() if v.expires_at < now]
    for k in expired:
        del _drafts[k]


def get_db_conn() -> sqlite3.Connection:
    conn = get_connection(DB_PATH)
    try:
        yield conn
    finally:
        conn.close()


def _infer_experience(request: LearnRequest) -> ExperienceCreate:
    """Build an ExperienceCreate from a LearnRequest, inferring defaults."""
    category = request.category or "lesson"
    severity = request.severity or "info"
    tags = request.tags or []
    scope = request.scope or {}
    source = request.source or ""

    return ExperienceCreate(
        content=request.content,
        category=category,
        severity=severity,
        tags=tags,
        scope=scope,
        source=source,
    )


@router.post("/api/learn")
async def learn(
    request: LearnRequest,
    conn: sqlite3.Connection = Depends(get_db_conn),
) -> ApiResponse:
    """Submit a new experience draft. AI checks for duplicates/conflicts."""
    _purge_expired()

    experience = _infer_experience(request)

    try:
        duplicates, conflicts = await find_duplicates_and_conflicts(conn, experience)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Dedup check failed: {exc}") from exc

    draft_id = str(uuid.uuid4())
    expires_at = datetime.utcnow() + timedelta(hours=DRAFT_TTL_HOURS)

    draft = DraftExperience(
        draft_id=draft_id,
        experience=experience,
        duplicates=duplicates,
        conflicts=conflicts,
        expires_at=expires_at,
    )
    _drafts[draft_id] = draft

    message = "Draft created."
    if duplicates:
        message += f" {len(duplicates)} potential duplicate(s) found."
    if conflicts:
        message += f" {len(conflicts)} potential conflict(s) found."

    return ApiResponse.ok(data=draft.model_dump(), message=message)


@router.post("/api/learn/confirm")
async def confirm(
    request: ConfirmRequest,
    conn: sqlite3.Connection = Depends(get_db_conn),
) -> ApiResponse:
    """Confirm and persist a draft experience."""
    _purge_expired()

    draft = _drafts.get(request.draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="Draft not found or expired")

    if datetime.utcnow() > draft.expires_at:
        del _drafts[request.draft_id]
        raise HTTPException(status_code=410, detail="Draft has expired")

    # Apply optional overrides
    experience_data: ExperienceCreate = draft.experience
    if request.overrides:
        experience_data = request.overrides

    # Generate embedding
    try:
        embedding = await get_embedding(experience_data.content)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Embedding failed: {exc}") from exc

    now = datetime.utcnow()
    exp_id = str(uuid.uuid4())

    try:
        insert_experience(
            conn,
            exp_id=exp_id,
            content=experience_data.content,
            category=experience_data.category,
            severity=experience_data.severity,
            tags=experience_data.tags,
            scope=experience_data.scope,
            source=experience_data.source,
            status="active",
            created_at=now,
            updated_at=now,
        )
        insert_embedding(conn, exp_id, embedding)
        conn.commit()
    except Exception as exc:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to persist experience: {exc}") from exc

    del _drafts[request.draft_id]

    return ApiResponse.ok(
        data={"id": exp_id, "content": experience_data.content},
        message="Experience persisted successfully.",
    )
