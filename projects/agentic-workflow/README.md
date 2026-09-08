# Agentic Workflow

This project validates product-level suites of real Hermes Agents. AI Agents own judgments and bounded actions; shared context connects them. It does not build a workflow engine.

## Core model

Every decision combines three elements:

- **Goal** — where the product is trying to go;
- **Principles** — how every Agent chooses among otherwise legal actions;
- **Validation** — what observable evidence proves an Action had the intended effect.

The runtime loop remains dynamic:

`Signal -> Goal + effective Principles + Evidence -> Gap -> Action set -> Result/Review -> Validation -> new decision`

Plans, Issues, Skills, and task graphs may change when evidence changes. The stable parts are authority, Principle resolution, Role ownership, Hard Boundaries, and evidence-backed completion.

## Principle architecture

Read these before defining or changing a Product Agent Suite:

- [`principles/HARD_BOUNDARIES.md`](principles/HARD_BOUNDARIES.md) — non-overridable safety, truth, and authority rules;
- [`principles/DEFAULTS.md`](principles/DEFAULTS.md) — shared default judgment for every product and Role;
- [`principles/RESOLUTION.md`](principles/RESOLUTION.md) — inheritance, authority, conflict, and version-binding rules;
- [`principles/roles/`](principles/roles/) — professional judgment for each stable Role;
- [`templates/PRODUCT_PRINCIPLES.md`](templates/PRODUCT_PRINCIPLES.md) — one product's Jingtao-owned Principles;
- [`templates/HANDOFF.md`](templates/HANDOFF.md) — one Action's frozen Principle Context and Validation contract.

The effective stack is:

`Hard Boundaries -> Default Principles -> Product Principles -> Role Principles -> Task Constraints`

Lower layers may specialize or narrow higher layers. They do not silently weaken them. Formal Handoffs bind the exact Default, Product, and Role Principle revisions.

## Prototype-first environment

The entire environment is a validation and prototype environment, including services deployed on `zhlearn` or other hosts named as production machines. The default is to put a reversible user-visible capability in front of the user, observe its real effect, and then decide what engineering is justified.

Tests, broad hardening, framework completeness, abstraction, and operational ceremony are not default deliverables. Add only the minimum validation needed to falsify the claimed effect or protect a Hard Boundary. “Production host” describes placement; it does not by itself change the product into a production-engineering program.

## Hermes resource model

- **Profile** — one stable Agent identity, prompt, memory, and capability boundary for one Role. Different Roles never share a Profile.
- **Canonical Owner Session** — the one persistent Product Owner decision brain.
- **Task Session** — one isolated bounded Specialist execution. A Role Profile may host up to three concurrent task Sessions when their workspaces and seams do not conflict.
- **SOUL.md** — concise identity, responsibility, authority, and mandatory Principle pointers; not a copy of operational contracts.
- **AGENTS.md** — the project context map and authority pointers.
- **Skill** — an optional professional method selected for the observed Gap; never a mandatory stage.
- **Tool** — a bounded capability; never the product decision maker.
- **Goal/Product Principles/State** — shared product context with distinct authority.
- **Handoff/Result/Review** — immutable Action contracts and evidence.
- **Kanban** — durable formal work lifecycle when acceptance, artifacts, blocking, review, or recovery matter.
- **Cron/Webhook/message route** — Signals and wake mechanisms, never the product plan.

Profile-private memory is learned context, not shared product truth.

## Product-level topology

`AgenticWorkflow-Assistant` maintains suite form and Default/Role templates. It never owns a product Goal or decision.

Each product has:

1. one `ProductOwnerAgent-<Product>` Profile with one canonical persistent Owner Session;
2. one Profile per stable Specialist Role actually required by observed work;
3. zero to three isolated task Sessions per Specialist Profile for safe concurrency;
4. one shared product space containing Goal, Product Principles, State, contracts, and run evidence.

Display names use exactly `<RoleAgent>-<Product>` with two UpperCamelCase segments. The default user-facing Profile is not reused as a Specialist.

## Context stack

Keep sources small and non-duplicative:

1. Hard Boundaries from the canonical file, system policy, and product authorization layer;
2. Default Principles;
3. Product Goal and Product Principles;
4. Role Principles;
5. task Handoff and Validation;
6. live tool evidence.

SOUL and AGENTS files hold strong pointers into this hierarchy. Operational mechanics belong in Portfolio, Execution, routing, and communication contracts—not in Principles.

## Templates

- [`templates/AGENTIC_WORKFLOW_ASSISTANT.md`](templates/AGENTIC_WORKFLOW_ASSISTANT.md)
- [`templates/PRODUCT_AGENT_SUITE.md`](templates/PRODUCT_AGENT_SUITE.md)
- [`templates/PRODUCT_PRINCIPLES.md`](templates/PRODUCT_PRINCIPLES.md)
- [`templates/HANDOFF.md`](templates/HANDOFF.md)
- [`templates/VALIDATION.md`](templates/VALIDATION.md)
- [`templates/PORTFOLIO_STATE.md`](templates/PORTFOLIO_STATE.md)
- [`templates/EXECUTION_POOL.md`](templates/EXECUTION_POOL.md)

## Validation boundary

A principle-aware tracer is complete only when:

1. Owner and Specialist resolve the same exact Default/Product/Role revisions;
2. the selected Role makes a professional choice that is not merely the Owner's wording;
3. the Result contains observable effect evidence;
4. a Reviewer, when required, judges that effect against the same frozen context;
5. stale or conflicting Principle Context fails closed;
6. the Owner uses the Result to change or reaffirm the next decision.

Do not create a Principle engine, database, fixed pipeline, or large test framework for this validation. Use real Agent turns, a real bounded effect, and direct read-back.

Historical functional traces remain in [`VALIDATION.md`](VALIDATION.md). Canonical vocabulary is in [`CONTEXT.md`](CONTEXT.md).
