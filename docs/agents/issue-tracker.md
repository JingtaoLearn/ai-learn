# Issue tracker: GitHub

Issues, specifications, decision maps, and implementation tickets for this repository live in GitHub Issues at `JingtaoLearn/ai-learn`. Use the `gh` CLI or GitHub API from this checkout.

## Conventions

- Read the complete issue and comments before acting.
- Create specifications and tracer-bullet tickets as GitHub issues.
- Use native sub-issues and blocked-by relationships when available.
- Apply `ready-for-agent` only to fully specified, unblocked implementation tickets.
- Treat GitHub Copilot cloud agent as an execution adapter for an unblocked Agent-ready ticket; independently verify its PR.
- Pull requests are not a general request/triage surface.

## Pull requests as a triage surface

**PRs as a request surface: no.**

## Wayfinding operations

- A map is one issue labelled `wayfinder:map`.
- Decision tickets are native sub-issues labelled `wayfinder:research`, `wayfinder:prototype`, `wayfinder:grilling`, or `wayfinder:task`.
- Claim a frontier ticket by assigning it before work.
- Express blocking through native issue dependencies; fall back to an explicit `Blocked by` line only when the API is unavailable.
- Resolve one decision per session, record the resolution as a comment, close the ticket, and append a one-line linked gist to the map.

## Common commands

- Create: `gh issue create --title "..." --body-file <path>`
- Read: `gh issue view <number> --comments`
- List: `gh issue list --state open --json number,title,body,labels,assignees,comments`
- Comment: `gh issue comment <number> --body-file <path>`
- Label: `gh issue edit <number> --add-label "..."`
- Close: `gh issue close <number>`

GitHub shares one number space between issues and pull requests; resolve ambiguous references before acting.
