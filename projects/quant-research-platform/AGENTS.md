# AGENTS.md

## Rules
- Research only; no broker and no live trading.
- Evidence first: prove an unvalidated research or product idea with the cheapest valid Spike using existing code/data before production implementation.
- Define the observable pass/fail threshold before the Spike. `INVALIDATED` is a successful result and stops implementation; `PARTIAL` must state the accepted constraint.
- After a validated idea, implement the smallest useful vertical slice, then add focused contract/regression tests for the selected behavior. Do not build broad test scaffolds for unproven ideas.
- Every signal must use information available before execution (`shift(1)`).
- Preserve run reproducibility: config, data hashes, git state, stable run ID.
- Do not read, log, or commit `.env`, tokens, or SSH keys.
- Do not push from this host.
