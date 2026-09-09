#!/usr/bin/env python3
"""Validate the self-contained Kanban mobile preview."""

from __future__ import annotations

import html.parser
import importlib.util
import json
import re
import shutil
import subprocess
import tempfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "data" / "board-snapshot.json"
TEMPLATE = ROOT / "src" / "index.template.html"
PREVIEW = ROOT / "preview.html"
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
ALLOWED_TASK_KEYS = {
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
    "summary",
}
FIXTURE_TASK_KEYS = ALLOWED_TASK_KEYS | {"board_name"}
REQUIRED_IDS = {
    "app",
    "board-switcher",
    "search-input",
    "status-filters",
    "reset-filters",
    "task-list",
    "empty-state",
    "task-detail",
    "close-detail",
}
REQUIRED_STATUSES = {"running", "review", "ready", "todo", "blocked", "done", "archived"}
PATH_PATTERN = re.compile(r"(?:/home/|/tmp/|/var/|/etc/|/opt/|/srv/|/root/|[A-Za-z]:\\\\Users\\\\)")
FORBIDDEN_REFERENCE = re.compile(
    r"(?:"
    r"/home/|/tmp/|/var/|/etc/|/opt/|/srv/|/root/|[A-Za-z]:\\\\Users\\\\"
    r"|(?<![\w.-])(?:projects|expected-postimages|src|tests|vm|\.github)/"
    r"|(?<![\w.-])(?:feat|fix|chore|refactor|release|hotfix|origin)/"
    r"|(?<![\w.-])(?:current-main|exact-main|non-main|main)(?![\w.-])"
    r")",
    re.IGNORECASE,
)
NETWORK_OR_MUTATION = re.compile(
    r"\b(?:fetch|XMLHttpRequest|WebSocket|EventSource)\s*\(|\bmethod\s*:\s*['\"](?:POST|PUT|PATCH|DELETE)['\"]",
    re.IGNORECASE,
)


class StructureParser(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.scripts: list[str] = []
        self._script_parts: list[str] | None = None
        self.has_main = False
        self.has_dialog = False
        self.has_viewport = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if values.get("id"):
            self.ids.add(values["id"] or "")
        if tag == "main":
            self.has_main = True
        if tag == "dialog":
            self.has_dialog = True
        if tag == "meta" and values.get("name") == "viewport":
            self.has_viewport = True
        if tag == "script" and values.get("type") != "application/json":
            self._script_parts = []

    def handle_data(self, data: str) -> None:
        if self._script_parts is not None:
            self._script_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._script_parts is not None:
            self.scripts.append("".join(self._script_parts))
            self._script_parts = None


def load_builder():
    spec = importlib.util.spec_from_file_location("kanban_mobile_build", ROOT / "build.py")
    if spec is None or spec.loader is None:
        raise AssertionError("Could not load build.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def walk_keys(value):
    if isinstance(value, dict):
        yield from value
        for nested in value.values():
            yield from walk_keys(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from walk_keys(nested)


def extract_snapshot(document: str) -> dict:
    match = re.search(
        r'<script id="snapshot-data" type="application/json">(.*?)</script>',
        document,
        flags=re.DOTALL,
    )
    if not match:
        raise AssertionError("Missing embedded snapshot data")
    return json.loads(match.group(1))


def main() -> None:
    builder = load_builder()
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert len(fixture["tasks"]) == 89
    assert all(set(task) == FIXTURE_TASK_KEYS for task in fixture["tasks"])
    assert fixture["timezone"] == "Asia/Shanghai"

    with tempfile.TemporaryDirectory() as directory:
        rebuilt_path = Path(directory) / "preview.html"
        expected = builder.build(FIXTURE, TEMPLATE, rebuilt_path)
        rebuilt = rebuilt_path.read_text(encoding="utf-8")

    committed = PREVIEW.read_text(encoding="utf-8")
    assert rebuilt == committed, "preview.html is stale; run python3 build.py"

    parser = StructureParser()
    parser.feed(committed)
    assert parser.has_main and parser.has_dialog and parser.has_viewport
    assert REQUIRED_IDS.issubset(parser.ids), f"Missing IDs: {sorted(REQUIRED_IDS - parser.ids)}"
    assert parser.scripts, "Missing executable JavaScript"

    snapshot = extract_snapshot(committed)
    assert snapshot == expected
    assert not (set(walk_keys(snapshot)) & FORBIDDEN_KEYS)
    assert all(set(task) == ALLOWED_TASK_KEYS for task in snapshot["tasks"])
    assert snapshot["totals"]["listed_tasks"] == len(fixture["tasks"])
    assert snapshot["totals"]["board_records"] == sum(
        sum(board["counts"].values()) for board in fixture["boards"]
    )
    assert [board["slug"] for board in snapshot["boards"]] == [
        board["slug"] for board in fixture["boards"]
    ]
    assert [board["counts"] for board in snapshot["boards"]] == [
        {status: board["counts"].get(status, 0) for status in builder.STATUS_ORDER}
        for board in fixture["boards"]
    ]

    listed_by_board_status = {
        slug: Counter(task["status"] for task in snapshot["tasks"] if task["board"] == slug)
        for slug in (board["slug"] for board in snapshot["boards"])
    }
    for board in snapshot["boards"]:
        for status, count in listed_by_board_status[board["slug"]].items():
            assert count <= board["counts"][status]

    public_text = json.dumps(snapshot, ensure_ascii=False)
    assert not PATH_PATTERN.search(public_text), "Local filesystem path leaked into public data"
    assert not FORBIDDEN_REFERENCE.search(public_text), "Repository path or branch reference leaked into public data"

    regression_fixture = json.loads(json.dumps(fixture))
    regression_fixture["tasks"][0]["title"] = "Inspect projects/private on origin/main"
    regression_fixture["tasks"][0]["summary"] = (
        "Compare current-main, exact-main, non-main, feat/private, src/module, "
        "tests/check, vm/service, .github/workflow, and /home/private/report.md"
    )
    with tempfile.TemporaryDirectory() as directory:
        regression_path = Path(directory) / "preview.html"
        regression_source_path = Path(directory) / "fixture.json"
        regression_source_path.write_text(json.dumps(regression_fixture), encoding="utf-8")
        builder.build(regression_source_path, TEMPLATE, regression_path)
        regression_snapshot = extract_snapshot(regression_path.read_text(encoding="utf-8"))
    regression_text = json.dumps(regression_snapshot, ensure_ascii=False)
    assert not FORBIDDEN_REFERENCE.search(regression_text), "Synthetic forbidden reference survived output build"
    assert "origin/main" not in regression_text, "Observed branch leak regression survived output build"
    javascript = "\n".join(parser.scripts)
    assert not NETWORK_OR_MUTATION.search(javascript), "Network or mutation API found"
    assert "innerHTML" not in javascript, "Avoid unsanitized HTML insertion"
    assert REQUIRED_STATUSES.issubset(set(re.findall(r'\b(?:running|review|ready|todo|blocked|done|archived)\b', committed)))
    assert "min-height: 44px" in committed
    assert "overflow-x: hidden" in committed
    assert "max-width: 1180px" in committed
    assert "snapshot updated" in committed.lower()
    assert "frozen snapshot" in committed.lower()
    assert "Asia/Shanghai" in committed

    node = shutil.which("node")
    if node:
        with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8") as script_file:
            script_file.write(javascript)
            script_file.flush()
            subprocess.run([node, "--check", script_file.name], check=True, capture_output=True, text=True)

    print(
        "PASS: structure, embedded contract, exact fixture totals, privacy, read-only JS, "
        "responsive/a11y markers, and JavaScript syntax"
    )
    print(
        f"PASS: {snapshot['totals']['listed_tasks']} listed tasks; "
        f"{snapshot['totals']['board_records']} total board records"
    )


if __name__ == "__main__":
    main()
