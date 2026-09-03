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

## Terminal-result notification acceptance

The first post-MVP notification slice removed the manual Completion Signal from one real Research result path:

- canonical Owner Session: `20260903_075757_73a49f`;
- source Agent: `ResearchAgent-QuantResearch`;
- durable event ID: `7aa3c011dff1ee6b0b54c689e80970517736b6033f042d5f9c22e631d03f337f`;
- run/action: `notification-link-002` / `exact-session-auto-return-002`;
- Result SHA-256: `1e7f84f80242c1d729f1ab431750c9c29eedb0b7998cc262d7a1699e93a87edf`;
- Owner Decision: `/home/jingtao/.hermes/workflows/quant-research/runs/notification-link-002/DECISION.md`.

The Research Agent wrote the Result and emitted `RESULT_READY`. The adapter persisted the event before calling the Owner profile, rebuilt a small secret-free OS-user environment across the nested Profile boundary, and targeted the exact existing Owner Session with `--resume`. Hermes reported the same Session ID through authoritative CLI metadata. The Owner independently verified the Result hash and wrote the Decision without a manually injected Completion Signal.

Re-emitting the identical event returned `deduplicated`; the Owner Session message count remained unchanged. A separate invalid-owner smoke retained `event.json` and a failed attempt while leaving `delivered.json` absent.

Assistant and Reviewer terminal outcomes must adopt this same envelope next. External Signals, human decisions, and watchdog/dead-letter escalation remain separate later slices.

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

`PASS` — the same persistent Product Owner Session received multiple Signals, selected and invoked an isolated specialist, and now receives a durable terminal-result event that automatically wakes the exact Session. The Owner verified the Result and changed the next product Action from that evidence. Assistant, Reviewer, external-event, human-decision, and reliability links remain later slices.
