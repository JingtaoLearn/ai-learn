# Issue tracker: GitHub

Issues, specs, and implementation tickets for this repository live in GitHub Issues at `JingtaoLearn/ai-learn`. Use the `gh` CLI from the repository so it resolves the remote automatically.

## Conventions

- Create issues with `gh issue create` and a body file for multi-line content.
- Read the complete issue and comments with `gh issue view <number> --comments`.
- Apply and remove workflow labels with `gh issue edit`.
- Close work only after the implementation and verification evidence are recorded.
- Publish specs and tracer-bullet tickets as GitHub issues.
- Use GitHub native issue dependencies when available; otherwise record `Blocked by` in the issue body.

## Copilot cloud-agent execution

For an unblocked `ready-for-agent` ticket, GitHub Copilot cloud agent is an available Issue-to-PR executor:

- Push the exact accepted dependency branch before assignment and select it as the base branch.
- Put complete constraints and file ownership in the assignment prompt before starting; later Issue comments are not consumed by the running agent.
- Do not run a second writing agent against the same ticket or files.
- Monitor with `gh agent-task list/view` or the created PR.
- Independently run tests, security checks, and Matt Standards/Spec review before integration.

## Pull requests as a triage surface

**PRs as a request surface: no.** Pull requests are delivery artifacts, not incoming feature requests for the triage queue.
