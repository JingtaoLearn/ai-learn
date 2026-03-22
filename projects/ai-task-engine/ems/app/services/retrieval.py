"""Hybrid search logic: vector similarity + tag filtering + keyword matching."""

from __future__ import annotations

import re
import sqlite3
from typing import Any, Optional

from app.database import (
    get_experience_by_id,
    list_experiences,
    vector_search,
)
from app.embedding import get_embedding, l2_distance_to_cosine_similarity
from app.models import Experience, ExperienceMatch, SearchResult


async def hybrid_search(
    conn: sqlite3.Connection,
    query: str,
    tags: Optional[list[str]] = None,
    category: Optional[str] = None,
    severity: Optional[str] = None,
    status: Optional[str] = "active",
    limit: int = 20,
    similarity_threshold: float = 0.5,
) -> list[SearchResult]:
    """Perform hybrid vector + tag + keyword search."""
    # Step 1: generate query embedding
    query_embedding = await get_embedding(query)

    # Step 2: pre-filter candidates by metadata
    candidates = list_experiences(
        conn,
        category=category,
        severity=severity,
        tags=tags,
        status=status,
        limit=500,  # broad candidates pool
    )

    if not candidates:
        return []

    candidate_ids = [c["id"] for c in candidates]

    # Step 3: vector search over candidates
    vec_results = vector_search(conn, query_embedding, k=limit * 2, candidate_ids=candidate_ids)

    if not vec_results:
        return []

    # Step 4: score and filter
    keyword_tokens = set(re.findall(r"\w+", query.lower()))
    id_to_candidate = {c["id"]: c for c in candidates}
    results: list[SearchResult] = []

    for exp_id, distance in vec_results:
        similarity = l2_distance_to_cosine_similarity(distance)
        if similarity < similarity_threshold:
            continue

        exp_data = id_to_candidate.get(exp_id)
        if exp_data is None:
            exp_data = get_experience_by_id(conn, exp_id)
        if exp_data is None:
            continue

        # Keyword boost
        content_tokens = set(re.findall(r"\w+", exp_data["content"].lower()))
        overlap = keyword_tokens & content_tokens
        if overlap:
            # Small boost for keyword overlap
            similarity = min(1.0, similarity + 0.02 * len(overlap))

        matched_tags = [t for t in (tags or []) if t in exp_data["tags"]]

        results.append(
            SearchResult(
                experience=Experience(**exp_data),
                similarity=round(similarity, 4),
                matched_tags=matched_tags,
            )
        )

    # Sort by similarity descending, take top `limit`
    results.sort(key=lambda r: r.similarity, reverse=True)
    return results[:limit]


async def check_action(
    conn: sqlite3.Connection,
    action: str,
    context: Optional[dict[str, Any]] = None,
    tags: Optional[list[str]] = None,
) -> tuple[str, list[ExperienceMatch], dict[str, Any]]:
    """Return (verdict, matches, llm_judgment) for a given action description.

    Two-stage pipeline:
    1. Vector retrieval: find candidate experiences via hybrid search
    2. LLM judgment: ask LLM to precisely judge whether each candidate applies
    """
    from app.services.llm import judge_action

    # Stage 1: Retrieve candidates
    results = await hybrid_search(
        conn,
        query=action,
        tags=tags,
        status="active",
        limit=10,
        similarity_threshold=0.6,
    )

    matches: list[ExperienceMatch] = []
    candidates_for_llm: list[dict[str, Any]] = []

    for r in results:
        matched_tags = [t for t in (tags or []) if t in r.experience.tags]
        match = ExperienceMatch(
            experience=r.experience,
            similarity=r.similarity,
            matched_tags=matched_tags,
        )
        matches.append(match)
        candidates_for_llm.append({
            "experience": r.experience.model_dump(),
            "similarity": r.similarity,
        })

    # Stage 2: LLM judgment
    if candidates_for_llm:
        llm_judgment = await judge_action(action, context, candidates_for_llm)
        verdict = llm_judgment.get("verdict", "pass")
    else:
        llm_judgment = {
            "judgments": [],
            "verdict": "pass",
            "summary": "No relevant experiences found.",
        }
        verdict = "pass"

    return verdict, matches, llm_judgment
