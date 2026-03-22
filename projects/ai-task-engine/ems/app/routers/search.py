"""POST /api/experiences/search — Semantic + keyword hybrid search."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException

from app.database import DB_PATH, get_connection
from app.models import ApiResponse, SearchRequest
from app.services.retrieval import hybrid_search

router = APIRouter(tags=["search"])


def get_db_conn():
    conn = get_connection(DB_PATH)
    try:
        yield conn
    finally:
        conn.close()


@router.post("/api/experiences/search")
async def search(
    request: SearchRequest,
    conn: sqlite3.Connection = Depends(get_db_conn),
) -> ApiResponse:
    try:
        results = await hybrid_search(
            conn,
            query=request.query,
            tags=request.tags,
            category=request.category,
            severity=request.severity,
            status=request.status,
            limit=request.limit,
            similarity_threshold=request.similarity_threshold,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return ApiResponse.ok(
        data=[r.model_dump() for r in results],
        message=f"{len(results)} result(s) found",
    )
