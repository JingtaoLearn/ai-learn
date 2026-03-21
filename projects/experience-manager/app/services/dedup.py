"""Duplicate and conflict detection for new experience drafts."""

from __future__ import annotations

import sqlite3
from typing import Optional

from app.database import list_experiences, vector_search
from app.embedding import get_embedding, l2_distance_to_cosine_similarity
from app.models import Experience, ExperienceCreate, ExperienceMatch

# Thresholds
DUPLICATE_THRESHOLD = 0.92   # very similar — likely a duplicate
CONFLICT_THRESHOLD = 0.80    # similar but may conflict


async def find_duplicates_and_conflicts(
    conn: sqlite3.Connection,
    draft: ExperienceCreate,
) -> tuple[list[ExperienceMatch], list[ExperienceMatch]]:
    """Return (duplicates, conflicts) for a draft experience.

    Duplicates: similarity >= DUPLICATE_THRESHOLD (same idea)
    Conflicts:  similarity >= CONFLICT_THRESHOLD but not a duplicate,
                and the severity or category differ
    """
    embedding = await get_embedding(draft.content)

    vec_results = vector_search(conn, embedding, k=20)

    if not vec_results:
        return [], []

    duplicates: list[ExperienceMatch] = []
    conflicts: list[ExperienceMatch] = []

    # Fetch existing active experiences for comparison
    existing = {
        e["id"]: e
        for e in list_experiences(conn, status="active", limit=1000)
    }

    for exp_id, distance in vec_results:
        similarity = l2_distance_to_cosine_similarity(distance)
        if similarity < CONFLICT_THRESHOLD:
            continue

        exp_data = existing.get(exp_id)
        if exp_data is None:
            continue

        exp = Experience(**exp_data)
        matched_tags = [t for t in draft.tags if t in exp.tags]
        match = ExperienceMatch(
            experience=exp,
            similarity=round(similarity, 4),
            matched_tags=matched_tags,
        )

        if similarity >= DUPLICATE_THRESHOLD:
            duplicates.append(match)
        else:
            # Check for conflict: same topic but different verdict/category
            same_category = exp.category == draft.category
            same_severity = exp.severity == draft.severity
            if not (same_category and same_severity):
                conflicts.append(match)

    return duplicates, conflicts
