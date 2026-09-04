# Goal

Validate a minimal but real Agentic Workflow in which AI Agents are the functional and decision-making nodes, each product has one canonical persistent Product Owner Session as its decision brain, and simple shared files support evidence and handoffs without replacing that Session.

The workflow should:

- let a Hermes Product Owner Agent retain this fixed Goal and current information in its canonical persistent Session;
- let real-time information events enter that existing Owner Session as Signals;
- gather live facts directly through existing tools when they can change the decision;
- reconcile all non-Done Workstreams and choose a capacity-bounded set of safe independent Actions with the Agents best suited to perform them;
- pass the same Goal plus one Workstream-specific Handoff to each selected Agent;
- return the Result to the Owner;
- let each Result change the affected Workstream, the next Action set, Agent allocation, or a justified lane/Portfolio wait.

## Current phase

Functional validation only.

## Principles

- Prefer real Agent calls over framework code.
- Keep shared context in Markdown files.
- Let Agents choose Skills and Tools when needed.
- Do not build a database, kernel, state machine, receipt system, replay engine, or connector framework.
- Do not write or run tests.
- Keep every individual Action bounded; one Signal may produce several independent Workstream Actions when compatible execution slots exist.
- Do not merge, deploy, change production signals, or perform high-risk external actions without Jingtao's explicit approval.

## Success

The workflow must complete at least one useful, evidence-backed action on a real project through `Product Owner Session → Specialist → Result → Product Owner decision`. A file round-trip against the superseded Agentic Workflow implementation alone is not sufficient.
