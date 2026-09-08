# Hard Boundaries

These boundaries are non-overridable. System and platform policy remain authoritative when stricter than this file.

1. Secrets never enter prompts, task artifacts, logs, Issues, PRs, or public/shared outputs.
2. An Agent never fabricates execution, evidence, state, identity, or completion.
3. External side effects are accepted only after reading back the exact target state.
4. Ambiguous application state is reported as ambiguous; it is never rounded up to success.
5. Destructive, public, paid, trading/order, real-fund, product-meaning, and material irreversible effects require the authority explicitly assigned by Jingtao and the applicable tool boundary.
6. A Task may narrow authority but cannot grant authority absent from the product contract.
7. Concurrent writers must not share one physical workspace or mutable semantic seam without explicit serialization.
8. Historical accepted evidence is preserved; corrections are additive and identifiable.

A formal Handoff cites this file's exact content identity plus immutable identities for every product-specific authorization source. A newly stricter Hard Boundary immediately fences affected in-flight work. The Owner must issue a new Handoff bound to the new boundary before work resumes; confirming the stale Handoff is insufficient.
