#!/usr/bin/env python3
"""Exercise the preview in a real Chromium browser at mobile and desktop sizes."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PREVIEW = ROOT / ".private" / "preview.html"
DEFAULT_SCREENSHOTS = ROOT / ".private" / "validation"


def assert_no_overflow(page, label: str) -> None:
    dimensions = page.evaluate(
        """() => ({
          documentWidth: document.documentElement.scrollWidth,
          viewportWidth: document.documentElement.clientWidth,
          bodyWidth: document.body.scrollWidth
        })"""
    )
    assert dimensions["documentWidth"] <= dimensions["viewportWidth"], (
        f"{label}: document overflow {dimensions}"
    )
    assert dimensions["bodyWidth"] <= dimensions["viewportWidth"], (
        f"{label}: body overflow {dimensions}"
    )


def box(locator) -> dict[str, float]:
    bounds = locator.bounding_box()
    assert bounds is not None, "Expected a visible element with a bounding box"
    return bounds


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preview", type=Path, default=DEFAULT_PREVIEW)
    parser.add_argument("--screenshots-dir", type=Path, default=DEFAULT_SCREENSHOTS)
    parser.add_argument(
        "--screenshots",
        action="store_true",
        help="Write private visual evidence under --screenshots-dir",
    )
    args = parser.parse_args()
    executable = shutil.which("chromium") or shutil.which("google-chrome")
    if not executable:
        raise SystemExit("Chromium is required for browser validation")

    if args.screenshots:
        args.screenshots_dir.mkdir(parents=True, exist_ok=True)
    console_errors: list[str] = []
    page_errors: list[str] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=executable,
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        page = browser.new_page(viewport={"width": 390, "height": 844})
        page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.goto(args.preview.resolve().as_uri(), wait_until="load")

        snapshot = page.evaluate(
            "JSON.parse(document.getElementById('snapshot-data').textContent)"
        )
        expected_tasks = snapshot["totals"]["listed_tasks"]
        expected_records = snapshot["totals"]["board_records"]
        assert page.locator(".task-card").count() == expected_tasks
        assert page.locator("[role=tab]").count() == len(snapshot["boards"]) + 1
        assert page.locator(".status-button").count() == 7
        assert page.locator("#results-meta").inner_text() == (
            f"{expected_tasks} shown · {expected_records} board records"
        )
        assert "Asia/Shanghai" in page.locator("#snapshot-time").inner_text()
        assert "Times shown in Asia/Shanghai" in page.locator("#footer-note").inner_text()
        assert_no_overflow(page, "390x844 initial")
        if args.screenshots:
            page.screenshot(
                path=args.screenshots_dir / "mobile-390x844.png", full_page=False
            )

        agentic_board = next(
            board for board in snapshot["boards"] if board["slug"] == "agentic-workflow"
        )
        agentic_tasks = [
            task for task in snapshot["tasks"] if task["board"] == "agentic-workflow"
        ]
        page.get_by_role("tab", name="Agentic Workflow").click()
        assert page.locator(".task-card").count() == len(agentic_tasks)
        agentic_records = sum(agentic_board["counts"].values())
        assert page.locator("#results-meta").inner_text() == (
            f"{len(agentic_tasks)} shown · {agentic_records} board records"
        )

        running_tasks = sum(task["status"] == "running" for task in agentic_tasks)
        page.get_by_role("button", name=f"Running, {running_tasks} tasks").click()
        assert page.locator(".task-card").count() == running_tasks
        page.locator("#search-input").fill("no such task in snapshot")
        assert page.locator(".task-card").count() == 0
        assert page.locator("#empty-state").get_attribute("aria-hidden") == "false"

        page.locator("#reset-filters").click()
        assert page.locator(".task-card").count() == expected_tasks
        page.locator(".task-card").first.click()
        assert page.locator("#task-detail").get_attribute("open") == ""
        detail_labels = page.locator("#detail-grid dt").all_text_contents()
        assert detail_labels == [
            "Task ID",
            "Board",
            "Assignee",
            "Priority",
            "Created",
            "Started",
            "Completed",
            "Heartbeat",
        ], f"Unexpected detail labels: {detail_labels}"
        detail_values = page.locator("#detail-grid dd").all_text_contents()
        for value in detail_values[4:]:
            if value != "Not recorded":
                assert "Asia/Shanghai" in value, f"Timestamp missing explicit timezone: {value}"
        if args.screenshots:
            page.screenshot(
                path=args.screenshots_dir / "mobile-detail-390x844.png",
                full_page=False,
            )
        page.keyboard.press("Escape")
        assert page.locator("#task-detail").get_attribute("open") is None
        assert_no_overflow(page, "390x844 interactions")

        page.set_viewport_size({"width": 1440, "height": 1000})
        page.reload(wait_until="load")
        page.evaluate("window.scrollTo(0, 0)")
        assert_no_overflow(page, "1440x1000")
        shell_width = box(page.locator("#app"))["width"]
        assert shell_width <= 1180
        cards = page.locator(".task-card")
        first_row_y = round(box(cards.nth(0))["y"])
        first_row_count = sum(
            round(box(cards.nth(index))["y"]) == first_row_y
            for index in range(min(cards.count(), 6))
        )
        assert first_row_count == 3
        if args.screenshots:
            page.screenshot(
                path=args.screenshots_dir / "desktop-1440x1000.png",
                full_page=False,
            )
        browser.close()

    assert not console_errors, f"Console errors: {console_errors}"
    assert not page_errors, f"Page errors: {page_errors}"
    print("PASS: 390x844 layout, filters, search, reset, bottom-sheet detail, keyboard close")
    print("PASS: 1440x1000 layout, 1180px content cap, three-column readable grid")
    if args.screenshots:
        print(f"Screenshots: {args.screenshots_dir / 'mobile-390x844.png'}")
        print(f"Screenshots: {args.screenshots_dir / 'mobile-detail-390x844.png'}")
        print(f"Screenshots: {args.screenshots_dir / 'desktop-1440x1000.png'}")


if __name__ == "__main__":
    main()
