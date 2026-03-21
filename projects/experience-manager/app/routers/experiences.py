"""CRUD endpoints for experiences."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.database import (
    DB_PATH,
    delete_experience_soft,
    get_connection,
    get_experience_by_id,
    insert_embedding,
    insert_experience,
    list_experiences,
    update_embedding,
    update_experience,
)
from app.embedding import get_embedding
from app.models import ApiResponse, Experience, ExperienceCreate, ExperienceUpdate

import uuid

router = APIRouter(tags=["experiences"])


def get_db_conn():
    conn = get_connection(DB_PATH)
    try:
        yield conn
    finally:
        conn.close()


@router.get("/api/experiences")
async def list_all(
    category: Optional[str] = Query(default=None),
    severity: Optional[str] = Query(default=None),
    tags: Optional[str] = Query(default=None, description="Comma-separated tags"),
    status: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    conn: sqlite3.Connection = Depends(get_db_conn),
) -> ApiResponse:
    tag_list = [t.strip() for t in tags.split(",")] if tags else None
    rows = list_experiences(
        conn,
        category=category,
        severity=severity,
        tags=tag_list,
        status=status,
        limit=limit,
        offset=offset,
    )
    return ApiResponse.ok(data=rows)


@router.get("/api/experiences/{experience_id}")
async def get_one(
    experience_id: str,
    conn: sqlite3.Connection = Depends(get_db_conn),
) -> ApiResponse:
    row = get_experience_by_id(conn, experience_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Experience not found")
    return ApiResponse.ok(data=row)


@router.put("/api/experiences/{experience_id}")
async def update_one(
    experience_id: str,
    update: ExperienceUpdate,
    conn: sqlite3.Connection = Depends(get_db_conn),
) -> ApiResponse:
    existing = get_experience_by_id(conn, experience_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Experience not found")

    fields: dict = {}
    update_data = update.model_dump(exclude_none=True)

    # Serialize JSON fields
    for key, value in update_data.items():
        if key in ("tags", "scope"):
            fields[key] = json.dumps(value)
        else:
            fields[key] = value

    if not fields:
        return ApiResponse.ok(data=existing, message="No fields to update")

    # If content changed, regenerate embedding
    if "content" in fields:
        try:
            new_embedding = await get_embedding(fields["content"])
            update_embedding(conn, experience_id, new_embedding)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Embedding failed: {exc}") from exc

    updated = update_experience(conn, experience_id, fields)
    if not updated:
        raise HTTPException(status_code=500, detail="Update failed")

    conn.commit()
    row = get_experience_by_id(conn, experience_id)
    return ApiResponse.ok(data=row, message="Experience updated")


@router.delete("/api/experiences/{experience_id}")
async def delete_one(
    experience_id: str,
    conn: sqlite3.Connection = Depends(get_db_conn),
) -> ApiResponse:
    existing = get_experience_by_id(conn, experience_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Experience not found")

    deleted = delete_experience_soft(conn, experience_id)
    conn.commit()
    if not deleted:
        raise HTTPException(status_code=500, detail="Delete failed")

    return ApiResponse.ok(message="Experience deprecated (soft delete)")


@router.post("/api/experiences")
async def create_one(
    body: ExperienceCreate,
    conn: sqlite3.Connection = Depends(get_db_conn),
) -> ApiResponse:
    """Direct creation endpoint (bypasses draft flow)."""
    try:
        embedding = await get_embedding(body.content)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Embedding failed: {exc}") from exc

    now = datetime.now(timezone.utc)
    exp_id = str(uuid.uuid4())

    try:
        insert_experience(
            conn,
            exp_id=exp_id,
            content=body.content,
            category=body.category,
            severity=body.severity,
            tags=body.tags,
            scope=body.scope,
            source=body.source,
            status="active",
            created_at=now,
            updated_at=now,
        )
        insert_embedding(conn, exp_id, embedding)
        conn.commit()
    except Exception as exc:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    row = get_experience_by_id(conn, exp_id)
    return ApiResponse.ok(data=row, message="Experience created")
