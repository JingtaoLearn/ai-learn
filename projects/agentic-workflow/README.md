# Agentic Workflow

This project is a functional prototype, not a workflow engine.

AI agents are the workflow nodes. Markdown files carry information between them. Hermes provides the scheduler and tools. There is no custom runtime, database, state machine, receipt protocol, migration system, or test suite.

## Live workflow home

The shared live directory is:

`/home/jingtao/.hermes/workflows/agentic-workflow`

Every agent reads `GOAL.md` when it needs the durable goal. The Owner also reads `SOURCES.md`, `STATE.md`, `INBOX.md`, and the latest files under `runs/`.

## Flow

1. A Hermes schedule wakes the Owner Agent.
2. The Owner reads the shared files and gathers only the live information needed for the current decision.
3. The Owner chooses one next action and one specialist Agent.
4. The Owner writes `runs/<id>/HANDOFF.md` and invokes that Agent.
5. The specialist reads the same Goal and the Handoff, then writes `RESULT.md`.
6. The Owner reads the result, writes `DECISION.md`, updates `STATE.md`, and stops.
7. The next scheduled pulse continues from the files.

There is no fixed research-to-plan-to-build chain. The Owner may inspect, delegate, act, wait, or stop according to the Goal and current information.

## Current boundary

- No tests are written or run during functional validation.
- Checks are optional tools selected by an Agent when useful.
- No automatic merge, deployment, production-signal change, or paid/public action.
- A high-risk external action still requires Jingtao's explicit approval.

The other files in this directory are versioned defaults for the live shared files.
