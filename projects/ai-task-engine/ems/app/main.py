"""FastAPI application entry point for the Experience Management System."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from app.database import DB_PATH, init_db
from app.models import ApiResponse
from app.routers import check, experiences, health, learn, search, tags


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncGenerator[None, None]:
    """Initialize the database on startup."""
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    init_db(DB_PATH)
    yield


app = FastAPI(
    title="Experience Management System",
    description=(
        "A FastAPI service for storing and retrieving AI agent experiences "
        "with semantic search powered by sqlite-vec."
    ),
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Enforce Bearer token auth on all routes except /api/health."""

    async def dispatch(self, request: Request, call_next: object) -> Response:
        token = os.environ.get("EMS_AUTH_TOKEN")
        if not token or request.url.path == "/api/health":
            return await call_next(request)  # type: ignore[arg-type]
        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {token}":
            return JSONResponse(status_code=401, content={"error": "Unauthorized"})
        return await call_next(request)  # type: ignore[arg-type]


# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_middleware(BearerAuthMiddleware)

# Register routers
app.include_router(health.router)
app.include_router(check.router)
app.include_router(learn.router)
app.include_router(search.router)      # must come before experiences to match /search first
app.include_router(experiences.router)
app.include_router(tags.router)


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content=ApiResponse.fail(
            error="Internal server error", detail=str(exc)
        ).model_dump(),
    )
