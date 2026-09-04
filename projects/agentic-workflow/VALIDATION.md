# Functional Validation

## Verified scope

This record covers the first product-level Agentic Workflow tracer for the QuantResearch Product Agent Suite. It validates native Agent information flow only. It does not validate the QuantResearch daily execution capability and does not authorize merge, deployment, production-signal changes, orders, paid actions, or public actions.

## Identities

- Agentic Workflow candidate: `0e3f43e3fd560a1e7734ad0b1a191e99028388d0`
- Product Owner Profile: `productowneragentquantresearch`
- Product Owner display name: `ProductOwnerAgent-QuantResearch`
- Canonical Owner Session: `20260903_075757_73a49f` (`Bot Chat`)
- Research Profile: `researchagentquantresearch`
- Research display name: `ResearchAgent-QuantResearch`
- Research canonical Session: `20260903_082058_d3fc23` (`Bot Chat`)
- Native Owner-to-Research delivery process: `proc_9397b83056f6`, exit code `0`

## Model policy

- Every Agent used `gpt-5.6-sol`.
- Product Owner reasoning: `max`.
- Research reasoning: `high`.
- Independent Reviewer reasoning: `xhigh`.
- No Claude model is part of the accepted configuration.

## Observed flow

1. A script-only Cron job delivered `QR-TRACE-001` to the Product Owner's canonical Bot Chat. The Owner wrote `runs/trace-001/DECISION.md` without invoking a specialist.
2. A second script-only Cron signal entered the same Owner Session and produced a bounded Handoff. Early terminal-based specialist launches failed because Agent-spawned subprocesses did not receive usable provider credentials; these failures were preserved rather than treated as success.
3. Bot Mode metadata enabled the native `message_agent` capability. A persistent interactive resume of the same Owner Session sent `QR-TRACE-007` to `ResearchAgent-QuantResearch` through `message_agent`.
4. The delivery process exited `0`. The Research Agent wrote `runs/trace-004/RESULT.md` from its own canonical Bot Chat.
5. The Research Agent produced a real Result, but native asynchronous Bot messaging did not automatically wake the dormant Owner. A separately injected Completion Signal caused the Owner to read the Result, write `runs/trace-004/DECISION.md`, append `SIGNALS.md`, and update `STATE.md`.
6. Independent verification passed for the bounded flow evidence, but later exact-session inspection identified the missing automatic terminal-result return as the next notification gap.

## Trigger cleanup

- The Product Owner Profile has zero Cron-sourced Sessions; scheduled Signals entered its canonical `Bot Chat` instead of creating replacement Owners.
- The superseded `awfowner` Profile, `awfscout` Profile, prototype job, recovery jobs, old workflow directory, and old worktrees were deleted after the latest product-level suite was accepted.
- No recurring tracer-only timer remains active.

## Session messenger acceptance

The final transport slice is a Skill scaffold with one standard-library script and no test suite. The communication research at `/home/jingtao/research/hermes-agent-communication-2026-09/report.md` now supplies its transport layering and envelope semantics.

### Native transport comparison

The canonical Owner invoked native `message_agent` from a headless one-shot CLI process. Research Session `20260903_082058_d3fc23` received the request and returned `NATIVE_DM_RETURN_OK`, but after the sender process exited no completion message entered Owner Session `20260903_075757_73a49f`; its message count remained unchanged. Native Bot DM remains correct for a live owning process, but it does not close this deployment's headless exact-Session callback gap.

Formal work therefore uses Kanban, scheduled Signals use Cron `bot-chat`, and `session-messenger` remains only the narrow headless callback adapter.

### Report-refined callback trace

1. Research Session `20260903_082058_d3fc23` dispatched `QUESTION` message `6b62da3a6f024a2c9aa85382185a1367` to Owner Session `20260903_075757_73a49f`.
2. Its `agent-message/v1` envelope carried stable correlation `comm-report-001`, idempotency key `comm-report-001-question`, the research report artifact, `requires_decision`, RFC3339 creation time, exact endpoints, and bounded hop metadata.
3. The exact Owner Session preserved correlation, set causation to the question ID, advanced the hop, and dispatched `REPLY` message `59c1983e2fdb4a0fb00b7d9a6d200174` with body `REPORT_REFINED_REPLY`.
4. The exact Research Session received that callback; its delivery result reports exit code `0`, `ok: true`, and observed Session equal to requested Session.

This proves Research→Owner question and Owner→the same Research Session answer without keeping the initiating Session process blocked.

### Non-Session Signal trace

Source `acceptance-monitor` dispatched non-replyable `SIGNAL` message `0329c1c68b6b4acba18b028be747ee5f` to the same Owner Session. Its final `agent-message/v1` envelope carried correlation `comm-report-signal-001`, stable idempotency key, RFC3339 creation time, report artifact, source label, decision flag, and bounded hop metadata. The delivery record reports exit code `0`, `ok: true`, and observed Session `20260903_075757_73a49f`; the Owner replied `FINAL_SIGNAL_ENVELOPE_OK` without changing product files or external state.

The same business envelope can therefore carry immediate Agent callbacks and Signals while native Hermes mechanisms keep their proper responsibilities. `type` is open routing context rather than a hard-coded workflow state machine.

## Artifact checksums

```text
cf0e00338546397d74a57cfcb75dfa061af33dd4c420c511010403be7c857fba  runs/trace-001/DECISION.md
47e0a33a6359ad5b5b2ea156168b1cab2b0e3fc001f111b30c0348339fa6c813  runs/trace-004/HANDOFF.md
4b34365f78f7b67e87c194cfd88fd590512e28a223e302f35ea98216108e05a0  runs/trace-004/RESULT.md
4a49a410bc18449b43a46e4730177e8b27b081192c33e33fee076c956594b9e4  runs/trace-004/DECISION.md
4e0f468245f004624e5b82e1ed07cab65d31819d31314e8b272c20d859281751  SIGNALS.md
99107a284a5d274e2290808c5737cc0eab874ce2026d57f806ce7439f2e2c4e9  STATE.md
```

The files above live under `/home/jingtao/.hermes/workflows/quant-research/`. Their checksums identify the exact reviewed live artifacts; they are not copied into the repository because the live product workspace remains the authority for active Agent state.

## Verdict

`PASS` — after incorporating the communication report, formal work is routed to Kanban, scheduled Signals to Cron `bot-chat`, short live Bot conversation to `message_agent`, and only the observed headless exact-Session callback gap to `session-messenger`. The refined `agent-message/v1` callback reached the same Research Session, and the existing non-Session Signal route reached the canonical Owner. The final adapter remains one Skill, one script, and no test suite or messaging service.

## First real formal loop — Gold overfit concern

Signal `gold-overfit-flow-001` entered canonical Owner Session `20260903_075757_73a49f`. The Owner created QuantResearch board task `t_6d2e50c0`; Research returned immutable `RESULT.md`; a product-matched `ReviewerAgent-QuantResearch` required one additive correction and then returned `PASS`; an explicit terminal envelope woke the exact Owner, which wrote `DECISION.md`.

The run exposed and repaired six workflow defects:

1. The Research Kanban worker spawned a duplicate nested researcher for the same Handoff. Future Research workers apply the method directly.
2. Headless role creation waited on protected-file approval and confused the requested Reviewer with an existing Experiment role. Future Assistant runs return a complete `BLOCKED_APPLY` package for supervised application.
3. The headless Owner requested that role through `message_agent`; after the one-shot sender exited, the Assistant completion could not wake it. Headless role-package returns now use `session-messenger` with the requester's exact callback Session.
4. The first review incorrectly substituted `ReviewerAgent-AgenticWorkflow` and crashed. Role routing now forbids cross-product substitutes; the dedicated QuantResearch Reviewer passed an isolated probe.
5. `max_retries=1` turned one reviewer crash into a blocked card. Formal cards now retain at least two transient attempts unless fail-fast behavior is justified.
6. The CLI-owned Owner was not automatically subscribed to Kanban wake. One idempotent exact-Session REVIEW envelope delivered the terminal state.

Independent review caught an unsupported interpretation of `1,201` as the exact pre-selection family size. Research preserved the original Result, added a separately hashed correction, and passed re-review. The accepted evidence supports `OVERFIT_RISK_SUBSTANTIATED` from 1,200 adaptive trials on reused exposed history without multiplicity correction or untouched final evaluation; it does not prove production harm or a superior replacement.

No Gold production manifest, script, Cron, schedule, signal, deployment, GitHub state, trading behavior, order capability, or paid/public state changed. This accepts the repaired Agentic Workflow path, not the model's future economic performance.

## Product-group communication provisioning

Issue `#235` adopts one private Feishu product group as the human surface for one Product Agent Suite without adding a workflow engine or scripted business pipeline.

Verified provisioning evidence:

- A private Feishu group named `QuantResearch` exists with a readable `A-QR` avatar on a solid deep-blue background with no frame; its exact chat identifier remains only in the live private binding.
- Read-back shows Jingtao and the existing Hermes Agent bot as the only members; Jingtao is the owner.
- The default Hermes configuration enables native profile multiplexing only for `productowneragentquantresearch` and contains one exact Feishu `profile_routes` match for the private product-group chat.
- Parsing the live configuration with `gateway.config.load_gateway_config()` and resolving it with `gateway.profile_routing.match_profile_route()` returns route `quant-research-feishu-group` and target Profile `productowneragentquantresearch`.
- `AgenticWorkflow-Assistant` and `ProductOwnerAgent-QuantResearch` have `lark-im` plus `lark-shared` as Agent-callable platform procedures; the Owner also has `feishu-reply-style`. `lark-cli auth status --verify` under both Profile homes confirms bot identity is ready and verified; all product-group operations use explicit `--as bot` rather than the unavailable user-default identity.
- A first multiplex restart exposed that copying the shared Feishu credential into the routed Owner Profile starts a second websocket adapter that can receive unrelated chats before exact route selection. The Profile-scoped gateway credentials were removed from both Assistant and Owner while their independently bound `lark-cli --as bot` authentication remained verified. The default Profile is the sole Feishu connector owner; exact native routing selects the Owner execution Profile after ingress.
- The first group `/resume` tracer was correctly denied: the configuration authorized the Feishu `open_id`, while slash-command policy evaluated a distinct adapter-normalized principal. The private live binding now records and authorizes only the observed Jingtao slash principal. Reusable provisioning must verify `/whoami` before requesting an admin-only bind; no private principal is committed here.
- One prompt-only daily Cron sends a `REPORT_DUE` timing Signal to the existing Product Owner Bot Chat at 20:30 Asia/Shanghai; it has no script and does not compose the report.
- No new script, daemon, database, queue, report generator, or workflow state machine was added. Existing native `lark-cli`, profile routing, Session resume, Cron/Heartbeat, Kanban, and `session-messenger` remain atomic capabilities.

The exact group route and bidirectional Owner replies are proven, but canonical-session persistence is not yet accepted. An authorized `/resume` successfully bound the group to the recorded Owner Session; a later manual `REPORT_DUE` acceptance run exposed that the current release's one-shot `bot-chat` delivery marks the resumed canonical row `cli_close`, after which the next group message correctly stayed in the Owner Profile but created a replacement Session. Recovery Pulse and Daily Report jobs are paused fail-closed. Final acceptance requires one stable rebind plus a same-Session timing-signal transport that does not close the gateway-owned canonical Session. Live identifiers remain only in the private communication record.

## Continuous Owner-loop acceptance

Jingtao's correction entered canonical Owner Session `20260903_075757_73a49f` as Signal `9e13001cc00a47caa14f72e64e6912c9`. The supervising Hermes session supplied no product Action. The Owner independently read the accepted Gold Decision, verified that no equivalent work was active, and selected the next evidence Gap: absence of a sealed prospective-evaluation contract.

The Owner created `runs/gold-prospective-prereg-001/HANDOFF.md`, created Kanban task `t_66886d92`, and dispatched run `15` to `ResearchAgent-QuantResearch` with independent `ReviewerAgent-QuantResearch` review required. The observed task state was `running`. This proves that a completed Decision became the next bounded Action inside the same persistent Owner Session rather than returning product control to the supervisor.

That first autonomously selected card later exposed two conformance defects: it used `max_retries=1`, and its terminal Reviewer PASS did not wake the CLI Owner because no notification subscription or exact-Session callback was attached. Recovery message `d767ce4c794f47afa201089c525333fb` woke the same Owner Session without selecting a product Action. The Owner verified the corrected package, accepted it as `READY_TO_IMPLEMENT` while leaving the evaluation `NOT_STARTED` and `NOT_AUTHORIZED`, then independently selected the next prerequisite Gap.

The Owner created and dispatched `t_7f23e23a` to resolve authoritative SGE calendar and Au99.99 opening-boundary provenance. That card used `max_retries=2`. Its original Handoff named the exact Owner callback endpoint but left message type and idempotency identity underspecified. Additive `CALLBACK-CORRECTION.md` preserved the Handoff and fixed the terminal mapping to `PASS/done -> REVIEW` or attributed `blocked -> BLOCKED`, with literal stable keys.

After independent Reviewer PASS, the final Reviewer emitted message `cb24222ebf43450380e69d542de78283` with idempotency key `gold-calendar-source-001-terminal-pass-v1`. Its delivery result reports exit code `0`, `ok: true`, and observed Session `20260903_075757_73a49f`. The Owner verified all 35 checksum entries, accepted `SOURCE_READY`, re-evaluated the Goal and Principles, identified the missing implementation-role Gap, and independently created and dispatched `t_b4ba4247` with `max_retries=2`. This is the first qualifying automatic terminal continuation toward the three-event recovery-Pulse removal condition.

Recovery Pulse `fd0dd85ef5c1` delivers only to `bot-chat:productowneragentquantresearch` and is configured every two hours from 08:00 through 22:00 Asia/Shanghai. The stored script was revised after the last recorded execution to instruct the Owner to inspect equivalent active work and to restrict stopping to the four legal waits with an exact wake condition. No scheduled execution has yet validated those revised instructions, and non-duplication under a Pulse concurrent with active work is not claimed by this trace.

Result/Review callbacks remain the primary continuation mechanism. Remove this recovery Pulse after three consecutive formal terminal events independently reach the exact canonical Owner Session, each causes the next assessment or a valid wait, and no missed callback, stale Action, or duplicate dispatch requires timer recovery.

## Multi-Workstream Portfolio acceptance

Jingtao's correction entered canonical Owner Session `20260903_075757_73a49f` as Signal `97671c6a09dd4f3683974ddca22efcd4`. The Signal supplied no product Action; it required the Owner to reconcile multiple Workstreams and fill independent capacity.

At reconciliation time, Gold implementation task `t_2779c2b2`, run `41`, occupied `implementationagentquantresearch` and isolated worktree `gold-prospective-kernel`. The Owner inspected live GitHub, Kanban, process, production, Profile, and host-capacity evidence, classified the remaining Workstreams, and found one additional safe heavy-worker slot on the 2-vCPU / 3.8-GiB host.

The Owner independently selected `WS-DAILY-DATA` rather than waiting for Gold. It created task `t_dc1f07d8`, run `42`, under `researchagentquantresearch` for a read-only `#196` readiness audit against accepted `#201`, current main, and `#140`'s ownership boundary. The Research worker writes only `runs/daily-data-lineage-001/`, so it does not overlap the Gold implementation worktree. Kanban read-back showed both tasks `running` concurrently under distinct Profiles.

The Owner also recorded ready-but-unstaffed `WS-GOLD-PROVENANCE` and `WS-REFERENCE-RUNTIME`, blocked `WS-DECISION-EXPERIENCE` and `WS-REPORT-OPERATOR`, and waiting `WS-PRODUCTION-OPS`, each with a wake or reconsider condition. This proves Portfolio reconciliation, capacity-aware Action-set selection, blocked-lane isolation, and non-overlapping concurrent dispatch without a workflow engine.

The first recovery Pulse mislabeled a fully occupied Portfolio with READY lanes as a "Legal portfolio wait." Additive `runs/portfolio-reconcile-001/PULSE-CORRECTION.md` preserves that observation but reclassifies it as `CAPACITY_SATURATED`: READY work remained, no compatible slot existed, and the exact wake condition was a slot release. The reusable contract now keeps saturation distinct from a genuine Portfolio wait.
