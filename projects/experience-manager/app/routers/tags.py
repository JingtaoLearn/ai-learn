"""GET /api/tags — List all tags with counts."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends

from app.database import DB_PATH, get_all_tags, get_connection
from app.models import ApiResponse, TagCount

router = APIRouter(tags=["tags"])


def get_db_conn():
    conn = get_connection(DB_PATH)
    try:
        yield conn
    finally:
        conn.close()


@router.get("/api/tags")
async def list_tags(
    conn: sqlite3.Connection = Depends(get_db_conn),
) -> ApiResponse:
    tag_counts = get_all_tags(conn)
    data = [TagCount(tag=tag, count=count).model_dump() for tag, count in tag_counts]
    return ApiResponse.ok(data=data)
