# Product Group Communication Template

Use one private Feishu group as the human communication surface for one Product Agent Suite. The group is not a task queue, state database, or second Product Owner.

## Binding

- Product: `<Product>`
- Feishu group name: `<Product>`
- Group avatar: `A-<ProductAbbrev>` in centered white text on a solid deep-blue background; no border, frame, gradient, icon, or decoration
- Feishu `chat_id`: `<oc_...>`
- Product Owner display name: `ProductOwnerAgent-<Product>`
- Product Owner Profile: `<profile-id>`
- Canonical Owner Session: `<session-id>`
- Authorized human: `<one owner open_id by default>`
- Gateway slash principal: `<adapter-normalized private principal; may differ from open_id>`
- Native route: `gateway.profile_routes` entry matching `platform=feishu` and the exact `chat_id`
- Report policy: `<event-driven alerts/milestones; any daily report is triggered only by REPORT_DUE>`

The native multiplexing gateway routes the group's inbound messages to the Product Owner Profile. The shared platform credential belongs to one gateway-owning Profile only; the routed Owner Profile must not start a duplicate adapter with that credential. Verified `lark-cli --as bot` access remains an independent outbound atomic tool. Bind the group to the existing canonical Owner Session with a supported session resume/handoff operation before declaring the route active. Never create a replacement Owner Session when the recorded Session is missing.

Before group creation, inspect the product communication record and list/search visible groups. Reuse only an exact recorded binding; never infer identity from a same-name group. Create one private group with Jingtao as the sole human member and owner by default, the existing Hermes bot as manager, and no unapproved members. Hermes isolates shared-group sessions per user by default; do not add another human until a supported shared-session policy binds every authorized participant to the one canonical Owner Session. Its square avatar uses `A-<ProductAbbrev>` such as `A-QR` in centered white text on a solid deep-blue background, with no border, frame, gradient, icon, or decoration. When adding the route, preserve every unrelated route and restrict the multiplexer allowlist to intended served Profiles. Restart only at a quiescent boundary and verify every affected messaging platform afterward.

## Prompt-first responsibility

Keep decisions and behavioral constraints in Agent instructions:

- `AgenticWorkflow-Assistant` decides when a product group is required, invokes existing atomic group/profile/config tools with explicit bot identity, records the binding, and verifies the route.
- `ProductOwnerAgent-<Product>` interprets human messages, decides whether they change the Goal, Principles, Portfolio, authorization state, or no product state, and chooses any resulting Action set.
- The Owner decides whether a scheduled `REPORT_DUE` signal has enough material change to justify a report and composes the report from verified evidence.

Do not encode these decisions in a shell/Python workflow, event-class state machine, daemon, message bus, or fixed report generator. A necessary script may exist only as a narrow Agent-callable tool that performs one atomic transport or platform action.

## Inbound behavior

Treat each authorized human message as information for the Owner, not as an automatic Kanban card. Classify it semantically as one or more of:

- `SIGNAL` — new evidence, idea, concern, or request;
- `QUESTION` — asks for a product-grounded answer;
- `GOAL_CHANGE` or `PRINCIPLE_CHANGE` — changes the user-owned contract;
- `CONTROL` — pause, resume, stop, or constrain a lane;
- `AUTHORIZATION` — explicitly grants or rejects a protected effect.

Acknowledge the interpretation, affected Workstreams, resulting Action set or legal wait, and any authorization boundary. Ask only for a genuine product trade-off that the Owner cannot resolve from evidence and existing Principles.

## Outbound behavior

The Owner sends to the product group only when the user-facing meaning warrants it:

- `ACK` — interpretation and resulting Portfolio effect for a user message;
- `DAILY_REPORT` — compact business-level changes, active direction, material risks, and decisions needed;
- `DECISION_REQUEST` — evidence, recommendation, alternatives, consequences, and the exact decision required;
- `ALERT` — production, evidence-integrity, security, budget, or convergence risk needing timely attention;
- `MILESTONE` — a verified user journey, stage gate, or Goal outcome.

Do not forward routine worker starts, tool traces, test chatter, every Review cycle, or unchanged status. Internal recovery Pulses never become user notifications.

## Daily report trigger

A schedule supplies only a `REPORT_DUE` Signal to the existing canonical Owner Session. It does not gather facts, choose content, or render the report. The Owner reads live sources, decides whether anything material changed, and either sends one concise report to the bound group or remains intentionally silent.

Prefer a native in-session Heartbeat/Loop when the report requires that Session's context. If a durable Cron is required, use it only as a wake/signal transport into the same Owner; do not let a fresh Cron session impersonate the Owner.

## Deterministic safeguards

Keep only non-negotiable transport and security invariants outside the prompt:

- exact `chat_id` → Profile routing;
- exact Session identity for resume/callback;
- authorized sender checks;
- platform message deduplication and bot self-message suppression;
- credential isolation;
- tool-level approval for irreversible or external high-risk effects.

These safeguards execute no product policy and select no Workstream or Action.

## Provisioning acceptance

Provisioning is complete only when all are true:

1. The private group exists with the intended avatar, user, and bot membership.
2. The exact `chat_id` has one native route to the intended Product Owner Profile.
3. An authorized user message executes in that Profile and the response returns to the same group.
4. `/whoami` confirms the intended user is an admin under the adapter-normalized group-slash principal before any admin-only bind command is requested.
5. The route is bound to the recorded canonical Owner Session; no second Owner brain remains active.
6. A `REPORT_DUE` tracer reaches that Session and either produces a group report or an explicit intentional-silence decision.
7. The binding and evidence are recorded without credentials.
8. Production, deployment, paid/public, and destructive authority remains unchanged.

## Lifecycle

- `PROVISIONING` — the group may exist, but route/session acceptance is incomplete; do not advertise it as active.
- `ACTIVE` — group, route, canonical Session binding, and real round trip are verified.
- `PAUSED` — retain group and evidence, but stop autonomous reports and product mutations; user questions may still receive status answers.
- `RETIRED` — remove the exact native route and scheduled triggers, preserve the group and audit record by default, and never reuse its `chat_id` for another product.

Rename only the display name; identity remains the recorded `chat_id`. On any partial failure, resume from the recorded state instead of creating another group.
