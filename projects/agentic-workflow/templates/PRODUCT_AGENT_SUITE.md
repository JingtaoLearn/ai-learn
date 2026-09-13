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
- User reports and discussion: `REPORTING.md`, instantiated in product runtime context
- Execution contract: `<path>`
- Canonical Owner: `ProductOwnerAgent-<Product>` / `<Profile ID>` / `<exact persistent Session>`
- Signal/result routes: `<verified native or narrow supported routes>`
- Feishu product-group desired state: `<public-safe desired-state path; live IDs remain protected>`
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
2. identifies the highest-value coherent Outcome and the Gaps preventing it;
3. chooses `DIRECT`, `DELEGATE`, `PARALLELIZE`, `WAIT`, or `STOP` from current evidence, uncertainty, risk and budget;
4. performs short, clear, low-risk and reversible work directly when delegation costs more than it adds;
5. delegates only when specialist judgment, isolation, parallelism, durable recovery or independent review materially improves the Outcome;
6. creates complete Handoffs with frozen Principle Context and Validation for formal delegated work;
7. reads returned evidence and any independent Review;
8. validates the actual effect in the owning system or user surface;
9. replans while retaining product responsibility.

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

## Feishu group configuration

Every user-facing suite declares and verifies:

- one existing-or-new private group, discovered before creation to prevent duplicates;
- versioned name, description and repository-owned avatar source;
- human ownership, bot membership/management and owner-controlled permissions;
- exactly one native Gateway route to the Product Owner Profile;
- a human-authored activation message that creates the canonical group-derived Owner Session;
- one report/root Thread per coherent Outcome, with native Owner continuity verified across follow-ups;
- reply routing, duplicate-signal behavior and restart continuity;
- read-back evidence for every externally changed group field.

The public repository stores placeholders and procedure, never live chat/user/app IDs or credentials.
Store no concrete runtime records in the framework source tree. This does not restrict product code changes or use of GitHub Issues/PRs. Instantiate these templates in the installed Agent environment and apply the real-product acceptance journey in `../VALIDATION.md`.
