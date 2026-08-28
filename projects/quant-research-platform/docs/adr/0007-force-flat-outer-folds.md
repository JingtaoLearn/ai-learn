# ADR-0007: Force flat with cost at outer fold boundaries

- Status: Accepted
- Date: 2026-08-28

## Context

Outer walk-forward segments need one honest account interpretation. Stitching returns while silently carrying positions or indicator state across independently reset folds would report a strategy that was never executed.

## Decision

v1 uses `FORCE_FLAT_WITH_COST`. Every outer OOS fold starts with the same normalized capital and no position, closes any terminal position at the frozen boundary with the declared transaction cost, and contributes its ordered net daily returns to the stitched selection-process evidence. Metric Document creation verifies artifact integrity and ledger/equity/cost reconciliation before Evaluation Policy sees the evidence.

## Consequences

The reported outer result evaluates periodic reselection with boundary liquidation. It does not claim continuous holdings across folds.

## Alternatives considered

Implicit position carry, cost-free synthetic liquidation, and state carry without versioned account/indicator artifacts were rejected as financially misleading.
