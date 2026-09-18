#!/usr/bin/env python3
"""Build a self-contained read-only Kanban preview."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path, default=ROOT / "fixtures" / "board-snapshot.json")
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / "index.html")
    args = parser.parse_args()

    snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
    required = {"schema", "generated_at", "timezone", "boards", "tasks"}
    if set(snapshot) != required or snapshot["schema"] != "kanban-mobile-snapshot/v1":
        raise SystemExit("snapshot schema mismatch")

    allowed = {
        "id", "title", "board", "board_name", "status", "assignee", "priority",
        "created_at", "started_at", "completed_at", "heartbeat_at", "summary",
    }
    for task in snapshot["tasks"]:
        unexpected = set(task) - allowed
        missing = allowed - set(task)
        if unexpected or missing:
            raise SystemExit(f"task allowlist mismatch: unexpected={sorted(unexpected)} missing={sorted(missing)}")
        if task["summary"]:
            raise SystemExit("traditional privacy posture requires empty free-text summaries")

    template = (ROOT / "src" / "index.template.html").read_text(encoding="utf-8")
    style = (ROOT / "src" / "style.css").read_text(encoding="utf-8")
    script = (ROOT / "src" / "app.js").read_text(encoding="utf-8")
    data = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    html = template.replace("@@STYLE@@", style).replace("@@DATA@@", data).replace("@@SCRIPT@@", script)
    if "@@" in html:
        raise SystemExit("unresolved template marker")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(html, encoding="utf-8")
    print(f"built {args.output} with {len(snapshot['tasks'])} tasks")


if __name__ == "__main__":
    main()
