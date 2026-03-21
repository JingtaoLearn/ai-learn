"""POST /api/learn and POST /api/learn/confirm endpoints."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone, timedelta
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
from app.services.llm import summarize_experience

router = APIRouter(tags=["learn"])

# In-memory draft store: draft_id -> DraftExperience
_drafts: dict[str, DraftExperience] = {}

DRAFT_TTL_HOURS = 1


def _purge_expired() -> None:
    now = datetime.now(timezone.utc)
    expired = [k for k, v in _drafts.items() if v.expires_at < now]
    for k in expired:
        del _drafts[k]


def get_db_conn() -> sqlite3.Connection:
    conn = get_connection(DB_PATH)
    try:
        yield conn
    finally:
        conn.close()


@router.post("/api/learn")
async def learn(
    request: LearnRequest,
    conn: sqlite3.Connection = Depends(get_db_conn),
) -> ApiResponse:
    """Submit a new experience. AI summarizes, classifies, and checks for duplicates."""
    _purge_expired()

    # Stage 1: AI summarization — LLM refines content, infers category/severity/tags
    try:
        ai_result = await summarize_experience(
            raw_content=request.content,
            category_hint=request.category,
            severity_hint=request.severity,
            tags_hint=request.tags,
            source=request.source,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"AI summarization failed: {exc}") from exc

    # Build the experience from AI output, respecting user overrides
    experience = ExperienceCreate(
        content=ai_result["content"],
        category=request.category or ai_result["category"],
        severity=request.severity or ai_result["severity"],
        tags=request.tags if request.tags else ai_result.get("tags", []),
        scope=request.scope or {},
        source=request.source or "",
    )

    # Stage 2: Dedup/conflict check
    try:
        duplicates, conflicts = await find_duplicates_and_conflicts(conn, experience)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Dedup check failed: {exc}") from exc

    draft_id = str(uuid.uuid4())
    expires_at = datetime.now(timezone.utc) + timedelta(hours=DRAFT_TTL_HOURS)

    draft = DraftExperience(
        draft_id=draft_id,
        experience=experience,
        duplicates=duplicates,
        conflicts=conflicts,
        expires_at=expires_at,
    )
    _drafts[draft_id] = draft

    message = "AI summarized and classified the experience."
    if duplicates:
        message += f" {len(duplicates)} potential duplicate(s) found."
    if conflicts:
        message += f" {len(conflicts)} potential conflict(s) found."

    return ApiResponse.ok(
        data={
            **draft.model_dump(),
            "ai_reasoning": ai_result.get("reasoning", ""),
            "original_content": request.content,
        },
        message=message,
    )


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

    if datetime.now(timezone.utc) > draft.expires_at:
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

    now = datetime.now(timezone.utc)
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
