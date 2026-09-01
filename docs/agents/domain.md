# Domain Docs

`ai-learn` is multi-context. Read [`CONTEXT-MAP.md`](../../CONTEXT-MAP.md) first, then load only the `CONTEXT.md` and ADRs for the project or infrastructure domain being changed.

## Before exploring

1. Read the relevant row in `CONTEXT-MAP.md`.
2. Read that context's `CONTEXT.md` glossary.
3. Read ADRs in that context's declared decisions directory.
4. For cross-context changes, read every affected context plus system-wide ADRs under `docs/adr/` when present.

If a context or ADR directory does not yet exist, proceed silently. The `domain-modeling` skill creates them lazily when vocabulary or a hard-to-reverse decision is resolved.

## Rules

- Use glossary terms exactly in issues, specifications, interfaces, tests, and reviews.
- Keep implementation details out of `CONTEXT.md`.
- Record only hard-to-reverse, surprising trade-offs as ADRs.
- Surface an ADR conflict instead of silently overriding it.
- Add a `CONTEXT-MAP.md` row when a project develops stable independent vocabulary.
