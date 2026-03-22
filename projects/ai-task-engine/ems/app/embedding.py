"""Embedding service client using OpenAI-compatible API via LiteLLM proxy."""

from __future__ import annotations

import math
import os
from functools import lru_cache
from typing import Optional

from openai import AsyncOpenAI

_DEFAULT_BASE_URL = "https://litellm.us.jingtao.fun/v1"
_DEFAULT_MODEL = "text-embedding-3-large"


@lru_cache(maxsize=1)
def _get_client() -> AsyncOpenAI:
    return AsyncOpenAI(
        api_key=os.getenv("EMS_EMBEDDING_API_KEY", ""),
        base_url=os.getenv("EMS_EMBEDDING_BASE_URL", _DEFAULT_BASE_URL),
    )


def _normalize(v: list[float]) -> list[float]:
    """L2-normalize a vector so that L2 distance equals cosine distance."""
    magnitude = math.sqrt(sum(x * x for x in v))
    if magnitude == 0.0:
        return v
    return [x / magnitude for x in v]


async def get_embedding(text: str) -> list[float]:
    """Return a normalized embedding vector for *text*."""
    client = _get_client()
    model = os.getenv("EMS_EMBEDDING_MODEL", _DEFAULT_MODEL)
    response = await client.embeddings.create(model=model, input=text)
    raw = response.data[0].embedding
    return _normalize(raw)


def l2_distance_to_cosine_similarity(distance: float) -> float:
    """Convert L2 distance (of normalized vectors) to cosine similarity.

    For normalized vectors: ||a-b||^2 = 2 - 2*cos(a,b)
    Therefore: cos(a,b) = 1 - distance^2 / 2
    """
    similarity = 1.0 - (distance ** 2) / 2.0
    return max(0.0, min(1.0, similarity))
