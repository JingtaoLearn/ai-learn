# State

## Phase

Principle-aware functional prototype validation.

## Current design

- Goal, Principles, and Validation are distinct first-class elements.
- Every Action resolves `Hard Boundaries -> Default Principles -> Product Principles -> Role Principles -> Task Constraints`.
- Default and Role Principles are canonical files; Product Principles remain Jingtao-owned.
- Formal Handoffs bind exact Default/Product/Role content identities and observable Validation.
- One stable Profile represents one Role. Safe concurrency uses up to three isolated task Sessions under that Profile; numbered duplicate Profiles are superseded.
- Product Owner remains singleton with one canonical persistent Session.
- Skills are optional professional methods and Tools are bounded capabilities; neither is a mandatory workflow stage.
- Operational mechanics live in Portfolio, Execution, communication, and task contracts rather than being copied into Principles or every SOUL.

## Prototype posture

The whole current environment is a validation environment, including services on hosts named as production machines. The preferred path is the smallest reversible user-visible prototype followed by direct observation of actual effect.

Architecture completeness, broad hardening, abstraction, and large test suites are deferred unless they are required to validate the claim or protect a Hard Boundary. Tests are evidence tools, not progress metrics.

## Verified history

The first product-level persistent Owner-to-Specialist-to-Owner tracer remains historical evidence in `VALIDATION.md`. Its original topology statements describe what was tested at that time and are superseded where the current README and templates now define one Profile per Role plus isolated task Sessions.

## Current frontier

Apply the principle hierarchy to the live `AgenticWorkflow-Assistant` and QuantResearch Product Agent Suite, then run one bounded real tracer proving:

1. common Principle revision resolution;
2. independent Role judgment;
3. user-visible or decision-relevant effect;
4. stale/conflicting context rejection;
5. Result-driven Owner replanning.

Do not add a Principle engine, database, fixed pipeline, or large test framework.
