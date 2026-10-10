# Kanban Mobile Snapshot

A mobile-first, read-only view of a frozen sanitized Agentic Workflow Kanban snapshot. The deliverable is a single self-contained HTML file with no backend, network requests, mutation controls, or runtime dependencies.

## Build

The repository includes only a clearly synthetic fixture. By default, the builder writes its self-contained preview to the ignored `.private/` directory:

```bash
python3 build.py
```

To build the private comparison preview from the authorized fixture without copying it into Git:

```bash
python3 build.py \
  --snapshot "$PRIVATE_FIXTURE" \
  --output .private/preview.html
```

The builder validates the input schema, board/status totals, unique task IDs, and the fixture's exact twelve-field task allowlist. It emits only the eleven fields needed by the public viewer, redacts path-, URL-, and branch-shaped text from titles and summaries, and renders recorded times explicitly in `Asia/Shanghai`.

The real fixture, generated previews, and browser screenshots are deliberately ignored. They remain private runtime artifacts and must not be added to the repository.

## Validate

```bash
python3 tests/validate_preview.py
python3 tests/validate_tracked_privacy.py

python3 tests/validate_preview.py \
  --snapshot "$PRIVATE_FIXTURE" \
  --preview .private/preview.html \
  --expect-listed-tasks 89 \
  --expect-board-records 410
python3 tests/validate_tracked_privacy.py \
  --private-file "$PRIVATE_FIXTURE" \
  --private-file .private/preview.html
python3 tests/validate_browser.py --preview .private/preview.html --screenshots
```

The static validation rebuilds into a temporary file and checks HTML structure, the embedded data contract and totals, exact input/output field allowlists, forbidden fields, repository paths and branch references (including an `origin/main` output regression), read-only/network-free JavaScript, accessible controls, required status vocabulary, and JavaScript syntax when Node.js is available. The tracked-privacy check proves that Git contains only the synthetic fixture and no generated preview or visual evidence. The real-browser validation uses installed Playwright and Chromium to exercise search, board/status filters, reset, task detail, keyboard close, explicit `Asia/Shanghai` time rendering, mobile overflow, and the capped desktop grid; optional screenshots stay under ignored `.private/validation/`.

## Preview

Open `.private/preview.html` directly in a browser after building it. It is intentionally static and can be placed on a private-link static host without a server application.

The task list is the fixture's curated task subset. The overview distinguishes its listed task cards from the fixture's complete board-level record counts.

The frozen Principle Context, Handoff identity, private artifact hashes, and validation receipt for the additive privacy revision are recorded in [`RESULT.md`](RESULT.md) without including private board content.
