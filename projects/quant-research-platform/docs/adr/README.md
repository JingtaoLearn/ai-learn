# Parameter Study Architecture Decisions

These ADRs record the hard-to-reverse decisions behind trustworthy parameter studies:

1. Reuse Experiment and Attempt execution.
2. Bind physical Execution Dataset Slices into Experiment identity.
3. Accept only canonical verified Experiment evidence.
4. Separate holdout access, outcome, and freshness.
5. Treat search-budget changes as Study forks.
6. Fail closed on Study execution-identity drift.
7. Force flat with cost at outer fold boundaries.

Read the project [`CONTEXT.md`](../../CONTEXT.md) first so ADR terms retain their canonical meanings.
