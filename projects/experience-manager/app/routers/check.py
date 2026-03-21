"""POST /api/check — Query if an action matches any experiences."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException

from app.database import DB_PATH, get_connection
from app.models import ApiResponse, CheckRequest, CheckResponse
from app.services.retrieval import check_action

router = APIRouter(tags=["check"])


def get_db_conn() -> sqlite3.Connection:
    conn = get_connection(DB_PATH)
    try:
        yield conn
    finally:
        conn.close()


@router.post("/api/check")
async def check(
    request: CheckRequest,
    conn: sqlite3.Connection = Depends(get_db_conn),
) -> ApiResponse:
    try:
        verdict, matches, llm_judgment = await check_action(
            conn,
            action=request.action,
            context=request.context,
            tags=request.tags,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    verdict_messages = {
        "block": "Action is blocked by one or more guardrail experiences.",
        "warn": "Action triggered warning-level experiences. Proceed with caution.",
        "pass": "No blocking experiences found for this action.",
    }

    response = CheckResponse(
        verdict=verdict,
        matches=matches,
        message=llm_judgment.get("summary", verdict_messages.get(verdict, "")),
    )
    return ApiResponse.ok(data={
        **response.model_dump(),
        "llm_judgment": llm_judgment,
    })
