"""FastAPI application entry point for the Experience Management System."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

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

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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
