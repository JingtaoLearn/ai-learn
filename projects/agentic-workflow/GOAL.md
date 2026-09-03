# Goal

Validate a minimal but real Agentic Workflow in which AI Agents are the functional and decision-making nodes, and simple shared files carry information between them.

The workflow should proactively advance useful work:

- a Hermes Owner Agent reads the current goal and information;
- it decides the next action and which Agent should perform it;
- the selected Agent reads the same goal plus a bounded handoff;
- the result returns to the Owner;
- the Owner decides what happens next.

## Current phase

Functional validation only.

## Principles

- Prefer real Agent calls over framework code.
- Keep shared context in Markdown files.
- Let Agents choose Skills and Tools when needed.
- Do not build a database, kernel, state machine, receipt system, replay engine, or connector framework.
- Do not write or run tests.
- Keep one bounded action per pulse.
- Do not merge, deploy, change production signals, or perform high-risk external actions without Jingtao's explicit approval.

## Success

The workflow repeatedly completes `Owner → Specialist → Result → Owner decision` using live information, and the returned result can change the next action or next Agent.
