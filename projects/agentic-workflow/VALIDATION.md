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
5. The attributed completion returned to the same Owner Session. The Owner read the real Result, wrote `runs/trace-004/DECISION.md`, appended `SIGNALS.md`, and updated `STATE.md`.
6. Independent verification passed with zero blockers on Kanban task `t_954eef46`.

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

`PASS` — the same persistent Product Owner Session received multiple Signals, selected and invoked an isolated specialist through native Agent messaging, received a real Result, and changed the next product Action from that evidence. The next QuantResearch product action is a separately governed no-network reference-loop Spike; this validation did not execute it.
