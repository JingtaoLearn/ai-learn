#!/usr/bin/env python3
"""Verify that Git contains only synthetic Kanban data and source artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
REPOSITORY = PROJECT.parents[1]
PROJECT_RELATIVE = PROJECT.relative_to(REPOSITORY)
EXAMPLE_FIXTURE = PROJECT / "data" / "example-board-snapshot.json"
IGNORED_PRIVATE_PATHS = (
    PROJECT / ".private" / "preview.html",
    PROJECT / ".private" / "validation" / "mobile-390x844.png",
    PROJECT / "data" / "board-snapshot.json",
    PROJECT / "preview.html",
    PROJECT / "validation" / "mobile-390x844.png",
)


def git(*args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(REPOSITORY), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--private-file",
        action="append",
        default=[],
        type=Path,
        help="Assert that this private fixture or generated artifact is not tracked",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    tracked = {
        REPOSITORY / path
        for path in git("ls-files", "--", str(PROJECT_RELATIVE)).splitlines()
    }
    assert EXAMPLE_FIXTURE in tracked, "Synthetic example fixture must be tracked"

    tracked_snapshots = {
        path for path in tracked if path.name.endswith("board-snapshot.json")
    }
    assert tracked_snapshots == {EXAMPLE_FIXTURE}, (
        f"Only the synthetic example fixture may be tracked: {sorted(tracked_snapshots)}"
    )
    assert not any(path.name == "preview.html" for path in tracked)
    assert not any("validation" in path.relative_to(PROJECT).parts for path in tracked)
    assert not any(".private" in path.relative_to(PROJECT).parts for path in tracked)

    example = json.loads(EXAMPLE_FIXTURE.read_text(encoding="utf-8"))
    assert example["tasks"], "Synthetic fixture must exercise the viewer"
    for task in example["tasks"]:
        identity = f"{task['id']} {task['title']} {task['summary']}".lower()
        assert "example" in identity or "synthetic" in identity

    for path in IGNORED_PRIVATE_PATHS:
        relative = path.relative_to(REPOSITORY)
        result = subprocess.run(
            ["git", "-C", str(REPOSITORY), "check-ignore", "--quiet", str(relative)],
            check=False,
        )
        assert result.returncode == 0, f"Private output path is not ignored: {relative}"

    tracked_hashes = {digest(path): path for path in tracked if path.is_file()}
    for private_file in args.private_file:
        assert private_file.exists(), f"Private file does not exist: {private_file}"
        assert private_file.resolve() not in {path.resolve() for path in tracked}
        private_hash = digest(private_file)
        assert private_hash not in tracked_hashes, (
            f"Private file is duplicated in tracked file {tracked_hashes[private_hash]}"
        )

    print(
        "PASS: Git tracks one clearly synthetic fixture and no real snapshot, "
        "generated preview, private output, or visual evidence"
    )


if __name__ == "__main__":
    main()
