# Domain Docs

This repository uses a multi-context domain layout.

## Before exploring

1. Read root `CONTEXT-MAP.md`.
2. Read the `CONTEXT.md` for every context relevant to the task.
3. Read the ADRs referenced by that context and any system-wide ADRs under root `docs/adr/`.

If a referenced file does not yet exist, proceed silently. Domain modeling creates glossaries and ADRs only when real terms or hard-to-reverse decisions have been resolved.

## Layout

- Root `CONTEXT-MAP.md` indexes contexts.
- Each self-contained project may keep its glossary at `projects/<project>/CONTEXT.md`.
- Context-specific decisions live at `projects/<project>/docs/adr/`.
- System-wide decisions live at root `docs/adr/`.

A `CONTEXT.md` is a glossary only. It must not contain implementation plans, file layouts, task status, or mutable release details. Use glossary vocabulary in issues, tests, code, and reports. Surface ADR conflicts explicitly rather than silently overriding them.
