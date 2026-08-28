# Issue tracker: GitHub

Issues, specs, and implementation tickets for this repository live in GitHub Issues at `JingtaoLearn/ai-learn`. Use the `gh` CLI from the repository so it resolves the remote automatically.

## Conventions

- Create issues with `gh issue create` and a body file for multi-line content.
- Read the complete issue and comments with `gh issue view <number> --comments`.
- Apply and remove workflow labels with `gh issue edit`.
- Close work only after the implementation and verification evidence are recorded.
- Publish specs and tracer-bullet tickets as GitHub issues.
- Use GitHub native issue dependencies when available; otherwise record `Blocked by` in the issue body.

## Pull requests as a triage surface

**PRs as a request surface: no.** Pull requests are delivery artifacts, not incoming feature requests for the triage queue.
