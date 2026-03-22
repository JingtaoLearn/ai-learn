"""LLM-based judgment service for experience matching."""

from __future__ import annotations

import json
import os
from typing import Any, Optional

from openai import AsyncOpenAI

_DEFAULT_BASE_URL = "https://litellm.us.jingtao.fun/v1"
_DEFAULT_MODEL = "github-copilot/claude-sonnet-4.5"

_client: Optional[AsyncOpenAI] = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            api_key=os.getenv("EMS_LLM_API_KEY", os.getenv("EMS_EMBEDDING_API_KEY", "")),
            base_url=os.getenv("EMS_LLM_BASE_URL", os.getenv("EMS_EMBEDDING_BASE_URL", _DEFAULT_BASE_URL)),
        )
    return _client


JUDGE_SYSTEM_PROMPT = """\
You are an Experience Judgment System for an AI agent framework. Your job is to determine whether a proposed action violates, conflicts with, or is warned against by a set of experience rules.

You will receive:
1. **Action**: A description of what the AI agent intends to do.
2. **Context**: Optional metadata about the action (repo, agent name, environment, etc.).
3. **Candidate Experiences**: A list of potentially relevant experience rules retrieved by semantic search, each with a category (guardrail/practice/lesson), severity (block/warn/info), and tags.

For each candidate experience, you must judge:
- **applies**: Does this experience actually apply to the described action? Consider the specific context, not just surface-level keyword similarity. Be precise — an experience about "TeamClaw PRs" does NOT apply to a different repo's PRs.
- **relevance**: How relevant is this experience? ("high", "medium", "low")
- **reasoning**: A brief explanation of why it does or doesn't apply.

Then provide an overall verdict:
- **"block"**: If ANY applicable experience has severity "block" and high relevance.
- **"warn"**: If any applicable experience has severity "block" with medium relevance, OR severity "warn" with high relevance.
- **"pass"**: If no experiences apply, or all applicable ones are informational.

Respond ONLY with valid JSON in this exact format:
{
  "judgments": [
    {
      "experience_id": "<id>",
      "applies": true/false,
      "relevance": "high" | "medium" | "low",
      "reasoning": "<brief explanation>"
    }
  ],
  "verdict": "block" | "warn" | "pass",
  "summary": "<one-sentence summary of the overall judgment>"
}"""


def _build_judge_prompt(
    action: str,
    context: Optional[dict[str, Any]],
    candidates: list[dict[str, Any]],
) -> str:
    """Build the user prompt for the LLM judge."""
    parts = [f"## Action\n{action}"]

    if context:
        parts.append(f"## Context\n```json\n{json.dumps(context, indent=2)}\n```")

    parts.append("## Candidate Experiences")
    for i, c in enumerate(candidates, 1):
        exp = c["experience"]
        parts.append(
            f"### Experience {i}\n"
            f"- **ID**: {exp['id']}\n"
            f"- **Content**: {exp['content']}\n"
            f"- **Category**: {exp['category']}\n"
            f"- **Severity**: {exp['severity']}\n"
            f"- **Tags**: {', '.join(exp['tags'])}\n"
            f"- **Similarity Score**: {c['similarity']}"
        )

    return "\n\n".join(parts)


async def judge_action(
    action: str,
    context: Optional[dict[str, Any]],
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    """Use LLM to judge whether candidates apply to the action.

    Args:
        action: Description of the intended action.
        context: Optional context metadata.
        candidates: List of dicts with "experience" (dict) and "similarity" (float).

    Returns:
        Dict with "judgments", "verdict", and "summary" keys.
    """
    if not candidates:
        return {
            "judgments": [],
            "verdict": "pass",
            "summary": "No relevant experiences found.",
        }

    client = _get_client()
    model = os.getenv("EMS_LLM_MODEL", _DEFAULT_MODEL)
    user_prompt = _build_judge_prompt(action, context, candidates)

    response = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.0,
        max_tokens=2000,
    )

    raw = response.choices[0].message.content.strip()

    # Parse JSON from response (handle markdown code blocks)
    if raw.startswith("```"):
        # Strip ```json ... ``` wrapper
        lines = raw.split("\n")
        json_lines = []
        in_block = False
        for line in lines:
            if line.startswith("```") and not in_block:
                in_block = True
                continue
            elif line.startswith("```") and in_block:
                break
            elif in_block:
                json_lines.append(line)
        raw = "\n".join(json_lines)

    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        # Fallback: if LLM returns garbage, use similarity-based verdict
        return {
            "judgments": [],
            "verdict": _fallback_verdict(candidates),
            "summary": "LLM judgment failed to parse; fell back to similarity-based verdict.",
            "raw_response": raw,
        }

    # Validate required fields
    if "verdict" not in result:
        result["verdict"] = _fallback_verdict(candidates)
    if "judgments" not in result:
        result["judgments"] = []
    if "summary" not in result:
        result["summary"] = ""

    return result


def _fallback_verdict(candidates: list[dict[str, Any]]) -> str:
    """Simple similarity-based fallback if LLM fails."""
    for c in candidates:
        exp = c["experience"]
        sim = c["similarity"]
        if sim >= 0.9 and exp.get("severity") == "block":
            return "block"
        if sim >= 0.8:
            return "warn"
    return "pass"


# ---------------------------------------------------------------------------
# Experience Summarizer — AI-driven learn flow
# ---------------------------------------------------------------------------

SUMMARIZE_SYSTEM_PROMPT = """\
You are an Experience Summarizer for an AI agent framework. Your job is to take raw, informal descriptions of lessons learned, rules, or best practices, and produce a structured experience entry.

You will receive:
1. **Raw Input**: A user's description of an experience, lesson, or rule (may be informal, verbose, or vague).
2. **Hints**: Optional category, severity, tags, and source provided by the user.

Your job:
1. **Summarize** the content into a clear, concise, actionable statement. Keep it under 2 sentences. Write it as a rule or guideline, not a story.
2. **Classify** the category if not provided:
   - "guardrail": Something that MUST NOT be done (a hard rule, a prohibition)
   - "practice": A recommended way of doing things (best practice)
   - "lesson": Something learned from experience (a takeaway, not necessarily a rule)
3. **Assess severity** if not provided:
   - "block": Violating this should stop the action entirely
   - "warn": Violating this deserves a warning but can proceed
   - "info": Informational, no enforcement needed
4. **Suggest tags**: Extract relevant keywords as tags (lowercase, short). Aim for 2-5 tags.

Respond ONLY with valid JSON:
{
  "content": "<summarized experience statement>",
  "category": "guardrail" | "practice" | "lesson",
  "severity": "block" | "warn" | "info",
  "tags": ["tag1", "tag2", ...],
  "reasoning": "<brief explanation of your classification choices>"
}"""


async def summarize_experience(
    raw_content: str,
    category_hint: Optional[str] = None,
    severity_hint: Optional[str] = None,
    tags_hint: Optional[list[str]] = None,
    source: Optional[str] = None,
) -> dict[str, Any]:
    """Use LLM to summarize and classify a raw experience description.

    Returns dict with "content", "category", "severity", "tags", "reasoning".
    """
    client = _get_client()
    model = os.getenv("EMS_LLM_MODEL", _DEFAULT_MODEL)

    parts = [f"## Raw Input\n{raw_content}"]

    hints = []
    if category_hint:
        hints.append(f"- Category hint: {category_hint}")
    if severity_hint:
        hints.append(f"- Severity hint: {severity_hint}")
    if tags_hint:
        hints.append(f"- Tag hints: {', '.join(tags_hint)}")
    if source:
        hints.append(f"- Source: {source}")

    if hints:
        parts.append("## Hints\n" + "\n".join(hints))

    user_prompt = "\n\n".join(parts)

    response = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SUMMARIZE_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.0,
        max_tokens=1000,
    )

    raw = response.choices[0].message.content.strip()

    # Parse JSON (handle markdown code blocks)
    if raw.startswith("```"):
        lines = raw.split("\n")
        json_lines = []
        in_block = False
        for line in lines:
            if line.startswith("```") and not in_block:
                in_block = True
                continue
            elif line.startswith("```") and in_block:
                break
            elif in_block:
                json_lines.append(line)
        raw = "\n".join(json_lines)

    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        # Fallback: return the raw content with defaults
        return {
            "content": raw_content,
            "category": category_hint or "lesson",
            "severity": severity_hint or "info",
            "tags": tags_hint or [],
            "reasoning": "LLM summarization failed; using raw input as-is.",
        }

    # Validate and fill defaults
    if "content" not in result or not result["content"]:
        result["content"] = raw_content
    if result.get("category") not in ("guardrail", "practice", "lesson"):
        result["category"] = category_hint or "lesson"
    if result.get("severity") not in ("block", "warn", "info"):
        result["severity"] = severity_hint or "info"
    if not isinstance(result.get("tags"), list):
        result["tags"] = tags_hint or []

    return result
