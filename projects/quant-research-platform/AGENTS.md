# AGENTS.md

## Rules
- Research only; no broker and no live trading.
- Strict RED-GREEN-REFACTOR with deterministic synthetic fixtures.
- Every signal must use information available before execution (`shift(1)`).
- Preserve run reproducibility: config, data hashes, git state, stable run ID.
- Do not read, log, or commit `.env`, tokens, or SSH keys.
- Do not push from this host.
