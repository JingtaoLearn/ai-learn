#!/usr/bin/env python3
"""Build a self-contained, read-only Kanban snapshot preview."""

from __future__ import annotations

import argparse
import html
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
DEFAULT_SNAPSHOT = ROOT / "data" / "board-snapshot.json"
DEFAULT_TEMPLATE = ROOT / "src" / "index.template.html"
DEFAULT_OUTPUT = ROOT / "preview.html"
SCHEMA = "kanban-mobile-snapshot/v1"
DISPLAY_TIMEZONE = "Asia/Shanghai"
STATUS_ORDER = ("running", "review", "ready", "todo", "blocked", "done", "archived")
PUBLIC_TASK_FIELDS = (
    "id",
    "title",
    "board",
    "status",
    "assignee",
    "priority",
    "created_at",
    "started_at",
    "completed_at",
    "heartbeat_at",
)
FIXTURE_TASK_FIELDS = PUBLIC_TASK_FIELDS + ("board_name", "summary")
FORBIDDEN_KEYS = {
    "body",
    "workspace_path",
    "branch_name",
    "worker_pid",
    "claim_lock",
    "payload",
    "comments",
    "logs",
    "events",
    "last_run_at",
    "board_name",
}
ABSOLUTE_PATH = re.compile(
    r"(?<![\w.])/(?:home|tmp|var|etc|opt|srv|root|Users)(?:/[^\s,;:)\]}`]+)+"
)
BRANCH_NAME = re.compile(
    r"(?<![\w.-])(?:"
    r"(?:feat|fix|chore|refactor|release|hotfix|origin)/[A-Za-z0-9._/-]+"
    r"|current-main|exact-main|non-main|main"
    r")(?![\w.-])",
    re.IGNORECASE,
)
REPOSITORY_PATH = re.compile(
    r"(?<![\w.-])(?:projects|expected-postimages|src|tests|vm|\.github)/"
    r"[^\s,;:)\]}`]+",
    re.IGNORECASE,
)
URL = re.compile(r"https?://\S+", re.IGNORECASE)
WHITESPACE = re.compile(r"\s+")


def sanitize_text(value: Any) -> str:
    """Remove path-, branch-, and URL-shaped values from display text."""
    text = "" if value is None else str(value)
    text = ABSOLUTE_PATH.sub("[redacted path]", text)
    text = REPOSITORY_PATH.sub("[redacted path]", text)
    text = BRANCH_NAME.sub("[redacted branch]", text)
    text = URL.sub("[link]", text)
    return WHITESPACE.sub(" ", text).strip()


def validate_fixture(data: dict[str, Any]) -> None:
    if data.get("schema") != SCHEMA:
        raise ValueError(f"Expected schema {SCHEMA!r}")
    if not isinstance(data.get("generated_at"), str) or not data["generated_at"]:
        raise ValueError("generated_at must be a non-empty string")
    if data.get("timezone") != DISPLAY_TIMEZONE:
        raise ValueError(f"timezone must be {DISPLAY_TIMEZONE!r}")
    if not isinstance(data.get("boards"), list) or not data["boards"]:
        raise ValueError("boards must be a non-empty list")
    if not isinstance(data.get("tasks"), list):
        raise ValueError("tasks must be a list")

    board_slugs: set[str] = set()
    declared: dict[str, Counter[str]] = {}
    for board in data["boards"]:
        if set(board) != {"slug", "name", "counts"}:
            raise ValueError(f"Unexpected board fields for {board.get('slug', '<unknown>')}")
        slug = board["slug"]
        if slug in board_slugs:
            raise ValueError(f"Duplicate board slug: {slug}")
        board_slugs.add(slug)
        counts = board["counts"]
        if not isinstance(counts, dict) or any(
            status not in STATUS_ORDER or not isinstance(count, int) or count < 0
            for status, count in counts.items()
        ):
            raise ValueError(f"Invalid counts for board {slug}")
        declared[slug] = Counter(counts)

    listed: dict[str, Counter[str]] = {slug: Counter() for slug in board_slugs}
    task_ids: set[str] = set()
    required = set(FIXTURE_TASK_FIELDS)
    for task in data["tasks"]:
        keys = set(task)
        if keys != required:
            raise ValueError(f"Unexpected task contract for {task.get('id', '<unknown>')}: {sorted(keys)}")
        task_id = task["id"]
        if task_id in task_ids:
            raise ValueError(f"Duplicate task id: {task_id}")
        task_ids.add(task_id)
        if task["board"] not in board_slugs:
            raise ValueError(f"Unknown board on task {task_id}")
        if task["status"] not in STATUS_ORDER:
            raise ValueError(f"Unknown status on task {task_id}")
        listed[task["board"]][task["status"]] += 1

    for slug, status_counts in listed.items():
        for status, count in status_counts.items():
            if count > declared[slug][status]:
                raise ValueError(
                    f"Listed {slug}/{status} count {count} exceeds declared count {declared[slug][status]}"
                )


def build_public_snapshot(data: dict[str, Any]) -> dict[str, Any]:
    validate_fixture(data)
    boards = [
        {
            "slug": sanitize_text(board["slug"]),
            "name": sanitize_text(board["name"]),
            "counts": {status: board["counts"].get(status, 0) for status in STATUS_ORDER},
        }
        for board in data["boards"]
    ]
    tasks = []
    for source in data["tasks"]:
        task = {field: source[field] for field in PUBLIC_TASK_FIELDS}
        for field in ("id", "title", "board", "status", "assignee"):
            task[field] = sanitize_text(task[field])
        task["summary"] = sanitize_text(source["summary"])
        tasks.append(task)

    public = {
        "schema": data["schema"],
        "generated_at": data["generated_at"],
        "timezone": DISPLAY_TIMEZONE,
        "boards": boards,
        "tasks": tasks,
        "totals": {
            "listed_tasks": len(tasks),
            "board_records": sum(sum(board["counts"].values()) for board in boards),
        },
    }
    assert_forbidden_fields_absent(public)
    return public


def assert_forbidden_fields_absent(value: Any) -> None:
    if isinstance(value, dict):
        overlap = FORBIDDEN_KEYS.intersection(value)
        if overlap:
            raise ValueError(f"Forbidden output fields: {sorted(overlap)}")
        for nested in value.values():
            assert_forbidden_fields_absent(nested)
    elif isinstance(value, list):
        for nested in value:
            assert_forbidden_fields_absent(nested)


def serialize_for_script(data: dict[str, Any]) -> str:
    serialized = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return serialized.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def build(snapshot_path: Path, template_path: Path, output_path: Path) -> dict[str, Any]:
    source = json.loads(snapshot_path.read_text(encoding="utf-8"))
    public = build_public_snapshot(source)
    template = template_path.read_text(encoding="utf-8")
    marker = "__SNAPSHOT_JSON__"
    if template.count(marker) != 1:
        raise ValueError(f"Template must contain exactly one {marker} marker")
    rendered = template.replace(marker, serialize_for_script(public))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(rendered, encoding="utf-8")
    return public


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    public = build(args.snapshot, args.template, args.output)
    print(
        f"Built {args.output} from {args.snapshot}: "
        f"{public['totals']['listed_tasks']} listed tasks, "
        f"{public['totals']['board_records']} board records"
    )


if __name__ == "__main__":
    main()
