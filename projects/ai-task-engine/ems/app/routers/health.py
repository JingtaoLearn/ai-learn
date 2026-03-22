"""Health check endpoint."""

from __future__ import annotations

from fastapi import APIRouter

from app.models import ApiResponse

router = APIRouter(tags=["health"])


@router.get("/api/health")
async def health() -> ApiResponse:
    return ApiResponse.ok(data={"status": "ok"}, message="Experience Manager is running")
