# Goal

Validate a minimal but real Agentic Workflow in which AI Agents are the functional and decision-making nodes, simple shared files carry information between them, and scheduled Owner pulses proactively advance useful work on a real project.

The workflow should:

- let a Hermes Owner Agent read this fixed Goal and current information;
- gather live facts directly through existing tools when they can change the decision;
- choose the next bounded action and the Agent best suited to perform it;
- pass the same Goal plus one Handoff to that Agent;
- return the Result to the Owner;
- let the Result change the next action, next Agent, or a justified stop.

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

The workflow must complete at least one useful, evidence-backed action on a real project through `Owner → Specialist → Result → Owner decision`. A file round-trip against the superseded Agentic Workflow implementation alone is not sufficient.
