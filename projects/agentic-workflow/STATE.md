# State

## Phase

`V2_NATIVE_RUNTIME_READY / WAITING_HUMAN_GROUP_ACTIVATION`

## Canonical design

- Agent decisions are driven by Goal, current evidence, uncertainty, risk, budget, Prompt/SOUL/Skills and supported Hermes native capabilities.
- There is no custom workflow engine, rules-based planner, callback service, second task store or custom Kanban UI.
- The Product Owner is the persistent accountable product brain. It chooses coherent Outcomes, completes short clear low-risk work directly, and delegates only when specialist value exceeds coordination cost.
- Stable Roles use one Profile each; compatible concurrent work uses isolated Sessions rather than numbered Profile replicas.
- Formal durable work uses native Kanban. Lightweight specialist consultation may use native `message_agent`.

## Live V2 instance

- Hermes release: `v0.21.2 / v2026.9.11`.
- Feishu group desired state: [`deployment/feishu-group.desired.yaml`](deployment/feishu-group.desired.yaml).
- Live group name and avatar: `Agentic Workflow V2` plus [`assets/group-avatar-v2.png`](assets/group-avatar-v2.png).
- Existing private group is reused; the public repository intentionally omits its live ID.
- Default Gateway is the sole Feishu credential owner.
- The single product route targets `productowneragentagenticworkflow`.
- Native same-card Implementation → independent Review canary: `PASS`.
- Native Dashboard was validated as the preferred Kanban surface; the one-shot validation process is not a persistent service.

## Superseded runtime

The legacy QuantResearch Agentic orchestration suite was sealed on 2026-09-13 rather than upgraded in parallel:

- five legacy Profiles removed from active runtime after verified sanitized export;
- 238 legacy QuantResearch boards archived;
- legacy workflow home removed from active discovery;
- QuantResearch Owner route, standalone Gateway and custom Owner Pulse removed;
- production, market-signal and monitoring jobs left unchanged.

The archive remains audit evidence, not a second executable workflow system.

## Current gate

The human owner must send `AW-SUITE-ACTIVATE-002` in the V2 group. Acceptance then requires:

1. one group-derived canonical Owner Session;
2. reply to the same group;
3. duplicate-signal safety;
4. continuity across Gateway restart;
5. one coherent user-visible Outcome with direct read-back;
6. a bounded seven-day observation period.

Until that event occurs, the honest verdict is `NATIVE_RUNTIME_READY / END_TO_END_NOT_YET_ACCEPTED`.
