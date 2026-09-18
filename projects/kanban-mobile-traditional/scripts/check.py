#!/usr/bin/env python3
"""Static acceptance checks for the built preview."""
from __future__ import annotations

import argparse
import json
import re
from html.parser import HTMLParser
from pathlib import Path

FORBIDDEN_FIELDS = ("workspace_path", "branch_name", "worker_pid", "claim_lock", "last_run_at", '"body"', '"comments"', '"logs"')
FORBIDDEN_PATHS = ("/home/", "/home....md", "/data/", "/tmp/", "/var/", "projects/", "expected-postimages/", "feat/", "origin/", "src/", "tests/", "vm/", ".github/")
ALLOWED_TASK_FIELDS = {
    "id", "title", "board", "board_name", "status", "assignee", "priority",
    "created_at", "started_at", "completed_at", "heartbeat_at", "summary",
}


class Parser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.scripts: list[str] = []
        self._script_type = None
        self._buffer: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get("id"):
            self.ids.add(attrs["id"])
        if tag == "script":
            self._script_type = attrs.get("type", "text/javascript")
            self._buffer = []

    def handle_data(self, data):
        if self._script_type is not None:
            self._buffer.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self._script_type is not None:
            if self._script_type != "application/json":
                self.scripts.append("".join(self._buffer))
            self._script_type = None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("html", type=Path)
    parser.add_argument("snapshot", type=Path)
    args = parser.parse_args()
    html = args.html.read_text(encoding="utf-8")
    snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
    parsed = Parser(); parsed.feed(html)
    required_ids = {"task-list", "board-tabs", "search-input", "status-select", "task-dialog", "snapshot-data"}
    missing = required_ids - parsed.ids
    if missing: raise SystemExit(f"missing required IDs: {sorted(missing)}")
    for token in (*FORBIDDEN_FIELDS, *FORBIDDEN_PATHS):
        if token in html: raise SystemExit(f"forbidden token in preview: {token}")
    match = re.search(r'<script id="snapshot-data" type="application/json">(.*?)</script>', html, re.S)
    if not match: raise SystemExit("embedded snapshot missing")
    embedded = json.loads(match.group(1))
    if embedded != snapshot: raise SystemExit("embedded snapshot does not match fixture")
    for task in embedded["tasks"]:
        if set(task) != ALLOWED_TASK_FIELDS:
            raise SystemExit(f"task field allowlist mismatch for {task.get('id')}")
        if task["summary"]:
            raise SystemExit(f"free-text summary is not empty for {task.get('id')}")
    if len(parsed.scripts) != 1: raise SystemExit("unexpected executable script count")
    js_path = args.html.with_suffix(".check.js"); js_path.write_text(parsed.scripts[0], encoding="utf-8")
    print(json.dumps({"tasks": len(embedded["tasks"]), "boards": len(embedded["boards"]), "js_check": str(js_path)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
