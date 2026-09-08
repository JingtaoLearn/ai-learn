# Product Agent Suite Template

Instantiate this document for one product. It defines authority and information flow; it does not authorize a live side effect by itself.

## Product contract

- Product: `<Product>`
- Product space: `<absolute shared path>`
- Goal: `<path and current revision>`
- Product Principles: `<path and current revision>`
- Default Principles: `<shared path and current revision>`
- Principle resolution: `<shared resolution contract>`
- Hard Boundaries: `<shared file, system policy, and explicit authorization sources with revisions>`
- State/Portfolio: `<paths>`
- Execution contract: `<path>`
- Canonical Owner: `ProductOwnerAgent-<Product>` / `<Profile ID>` / `<exact persistent Session>`
- Signal/result routes: `<verified native or narrow supported routes>`
- Temporary backstop: `<none or exact removal condition>`

The Owner Session is the living product decision brain. Goal, Principles, State, Handoffs, Results, and Reviews are inspectable shared sources; none replaces that Session.

## Effective Principle Context

Every Agent resolves and identity-binds:

`Hard Boundaries -> Default Principles -> Product Principles -> Role Principles -> Task Constraints`

Use [`../principles/RESOLUTION.md`](../principles/RESOLUTION.md). Formal work uses [`HANDOFF.md`](HANDOFF.md) and binds the exact Default/Product/Role SHA-256 identities. Workers return `PRINCIPLE_CONTEXT_STALE` or `PRINCIPLE_CONTEXT_CONFLICT` instead of guessing.

## Smallest suite

Start with the Owner. Add only Roles supported by observed recurring Gaps.

| Role | Stable Profile | Responsibility | Role Principles | Initial capabilities |
|---|---|---|---|---|
| Product Owner | `productowneragent<product>` | select Goal-aligned Action sets and absorb Results | `principles/roles/PRODUCT_OWNER.md` | product evidence, files, routing, Kanban |
| `<Specialist>` | `<profile id>` | close one recurring Gap class | `<role principle path>` | only capabilities required by that Role |

One Role has one Profile. Safe concurrency uses zero to three isolated task Sessions under that Profile. The Product Owner remains singleton with one canonical Session.

Display names use exactly `<RoleAgent>-<Product>` with two UpperCamelCase segments.

## Model policy

- every Agent uses `gpt-5.6-sol`;
- Product Owner uses `max` reasoning;
- Research/Experiment roles use `high`;
- Implementation, independent Reviewer, and AgenticWorkflow Assistant use `xhigh`;
- no task or scheduled override may silently change this policy.

## Role prompt contract

Every Role SOUL contains only:

1. identity and product responsibility;
2. authority and refusal boundary;
3. mandatory paths for Default, Product, and Role Principles;
4. required product and execution contracts;
5. result destination and broad communication boundary.

Do not copy complete Principle text, current Session IDs, host inventory, retry procedures, or incident repairs into every SOUL. Keep volatile identities in the product context and Handoff.

## Dynamic product loop

On every material Signal, Result, Review, Decision, or recovery Pulse, the Owner:

1. reads Goal, effective Principles, State, and decision-relevant evidence;
2. identifies the highest-value Gaps, including user-visible unmet effects;
3. selects the largest safe bounded Action set;
4. creates complete Handoffs with frozen Principle Context and Validation;
5. delegates to the exact Role Profile in isolated task Sessions;
6. reads returned evidence and any independent Review;
7. validates the actual effect in the owning system or user surface;
8. replans, stops, waits, or dispatches the next Action set.

This is a reasoning loop, not a fixed sequence of Skills or implementation stages.

## Prototype-first delivery

For all current product suites, including services placed on hosts called production machines:

- put the smallest reversible capability in front of the user early;
- validate actual usefulness and behavior before broad hardening;
- keep tests proportional to the claim and risk;
- do not require production-grade architecture merely because deployment reaches a production-named host;
- preserve Hard Boundaries, existing user data, rollback, and explicitly protected semantics.

A Reviewer evaluates the accepted prototype effect, not whether the implementation looks maximally formal.

## Work, review, and return

- Use Kanban for formal work with acceptance, artifacts, blocking, review, or recovery.
- Use short native Agent conversation only for lightweight consultation.
- Persist task evidence before wake/callback.
- a Reviewer reads the same frozen Hard Boundary, authorization, Resolution, Default, Product, Role, and Handoff identities plus the immutable candidate.
- `REVISE` creates additive correction evidence; it never rewrites the reviewed artifact.
- Completion returns to the exact canonical Owner Session, which reads the evidence and makes the next product decision.

## Product validation

A product iteration is complete only when:

- the user-visible or decision-relevant effect can be observed;
- the owning system is read back;
- the Result records effective Principle revisions and any exception;
- required Hard Boundary checks pass;
- the Owner uses the evidence to update the next decision.

Test count, document count, task completion, deployment status, and Agent confidence are not substitutes for this validation.
