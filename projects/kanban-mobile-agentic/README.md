# Kanban Mobile Snapshot

A mobile-first, read-only view of a frozen sanitized Agentic Workflow Kanban snapshot. The deliverable is a single self-contained HTML file with no backend, network requests, mutation controls, or runtime dependencies.

## Build

The repository includes the exact sanitized fixture used for this comparison.

```bash
python3 build.py
```

To rebuild from another compatible frozen fixture:

```bash
python3 build.py --snapshot /path/to/board-snapshot.json --output preview.html
```

The builder validates the input schema, board/status totals, unique task IDs, and the fixture's exact twelve-field task allowlist. It emits only the eleven fields needed by the public viewer, redacts path-, URL-, and branch-shaped text from titles and summaries, and renders recorded times explicitly in `Asia/Shanghai`.

## Validate

```bash
python3 tests/validate_preview.py
python3 tests/validate_browser.py
python3 tests/validate_browser.py --screenshots  # refresh visual evidence
```

The first validation rebuilds into a temporary file and checks HTML structure, the embedded data contract and totals, exact input/output field allowlists, forbidden fields, repository paths and branch references (including an `origin/main` output regression), read-only/network-free JavaScript, accessible controls, required status vocabulary, and JavaScript syntax when Node.js is available. The optional real-browser validation uses installed Playwright and Chromium to exercise search, board/status filters, reset, task detail, keyboard close, explicit `Asia/Shanghai` time rendering, mobile overflow, and the capped desktop grid; it writes visual evidence under `validation/`.

## Preview

Open [`preview.html`](preview.html) directly in a browser. It is intentionally static and can be placed on a private-link static host without a server application.

The task list is the fixture's curated task subset. The overview distinguishes its listed task cards from the fixture's complete board-level record counts.

The frozen Principle Context, Handoff identity, artifact hashes, and validation receipt for the additive privacy revision are recorded in [`RESULT.md`](RESULT.md).
