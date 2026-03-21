"""Pydantic models for the Experience Management System."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional
from pydantic import BaseModel, Field, field_validator
import uuid


# ---------------------------------------------------------------------------
# Core domain models
# ---------------------------------------------------------------------------

class Experience(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    content: str
    category: str  # guardrail | practice | lesson
    severity: str  # block | warn | info
    tags: list[str] = Field(default_factory=list)
    scope: dict[str, Any] = Field(default_factory=dict)
    source: str = ""
    status: str = "active"  # active | deprecated | merged
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("category")
    @classmethod
    def validate_category(cls, v: str) -> str:
        allowed = {"guardrail", "practice", "lesson"}
        if v not in allowed:
            raise ValueError(f"category must be one of {allowed}")
        return v

    @field_validator("severity")
    @classmethod
    def validate_severity(cls, v: str) -> str:
        allowed = {"block", "warn", "info"}
        if v not in allowed:
            raise ValueError(f"severity must be one of {allowed}")
        return v

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str) -> str:
        allowed = {"active", "deprecated", "merged"}
        if v not in allowed:
            raise ValueError(f"status must be one of {allowed}")
        return v


class ExperienceCreate(BaseModel):
    content: str
    category: str
    severity: str
    tags: list[str] = Field(default_factory=list)
    scope: dict[str, Any] = Field(default_factory=dict)
    source: str = ""

    @field_validator("category")
    @classmethod
    def validate_category(cls, v: str) -> str:
        allowed = {"guardrail", "practice", "lesson"}
        if v not in allowed:
            raise ValueError(f"category must be one of {allowed}")
        return v

    @field_validator("severity")
    @classmethod
    def validate_severity(cls, v: str) -> str:
        allowed = {"block", "warn", "info"}
        if v not in allowed:
            raise ValueError(f"severity must be one of {allowed}")
        return v


class ExperienceUpdate(BaseModel):
    content: Optional[str] = None
    category: Optional[str] = None
    severity: Optional[str] = None
    tags: Optional[list[str]] = None
    scope: Optional[dict[str, Any]] = None
    source: Optional[str] = None
    status: Optional[str] = None

    @field_validator("category")
    @classmethod
    def validate_category(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in {"guardrail", "practice", "lesson"}:
            raise ValueError("category must be one of guardrail, practice, lesson")
        return v

    @field_validator("severity")
    @classmethod
    def validate_severity(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in {"block", "warn", "info"}:
            raise ValueError("severity must be one of block, warn, info")
        return v

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in {"active", "deprecated", "merged"}:
            raise ValueError("status must be one of active, deprecated, merged")
        return v


# ---------------------------------------------------------------------------
# Request/Response models
# ---------------------------------------------------------------------------

class CheckRequest(BaseModel):
    action: str = Field(..., description="Natural language description of the action to check")
    context: Optional[dict[str, Any]] = Field(default=None, description="Optional context object")
    tags: Optional[list[str]] = Field(default=None, description="Optional tag hints for filtering")


class ExperienceMatch(BaseModel):
    experience: Experience
    similarity: float
    matched_tags: list[str] = Field(default_factory=list)


class CheckResponse(BaseModel):
    verdict: str  # block | warn | pass
    matches: list[ExperienceMatch]
    message: str


class LearnRequest(BaseModel):
    content: str = Field(..., description="Raw description of the experience to learn")
    category: Optional[str] = Field(default=None, description="Optional category hint")
    severity: Optional[str] = Field(default=None, description="Optional severity hint")
    tags: Optional[list[str]] = Field(default=None, description="Optional tags")
    scope: Optional[dict[str, Any]] = Field(default=None, description="Optional scope")
    source: Optional[str] = Field(default=None, description="Origin of this experience")


class DraftExperience(BaseModel):
    draft_id: str
    experience: ExperienceCreate
    duplicates: list[ExperienceMatch] = Field(default_factory=list)
    conflicts: list[ExperienceMatch] = Field(default_factory=list)
    expires_at: datetime


class ConfirmRequest(BaseModel):
    draft_id: str
    overrides: Optional[ExperienceCreate] = Field(
        default=None,
        description="Optional field overrides before persisting"
    )


class SearchRequest(BaseModel):
    query: str
    tags: Optional[list[str]] = None
    category: Optional[str] = None
    severity: Optional[str] = None
    status: Optional[str] = "active"
    limit: int = Field(default=20, ge=1, le=100)
    similarity_threshold: float = Field(default=0.5, ge=0.0, le=1.0)


class SearchResult(BaseModel):
    experience: Experience
    similarity: float
    matched_tags: list[str] = Field(default_factory=list)


class TagCount(BaseModel):
    tag: str
    count: int


# ---------------------------------------------------------------------------
# Generic API response wrapper
# ---------------------------------------------------------------------------

class ApiResponse(BaseModel):
    success: bool
    data: Optional[Any] = None
    message: Optional[str] = None
    error: Optional[str] = None
    detail: Optional[str] = None

    @classmethod
    def ok(cls, data: Any = None, message: Optional[str] = None) -> "ApiResponse":
        return cls(success=True, data=data, message=message)

    @classmethod
    def fail(cls, error: str, detail: Optional[str] = None) -> "ApiResponse":
        return cls(success=False, error=error, detail=detail)
