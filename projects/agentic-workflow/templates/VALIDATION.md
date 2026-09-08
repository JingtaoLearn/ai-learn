# Product Validation Template

Validation is a first-class product contract, not a synonym for tests.

## Intended effect

- User-visible or decision-relevant effect: `<what becomes possible or observable>`
- Owning surface/system: `<where the effect is observed>`
- Current baseline: `<what happens before the change>`

## Success evidence

- Direct observation: `<user surface, API response, artifact, state read-back, or real Agent behavior>`
- Identity: `<version, commit, artifact, request, or content hash when needed>`
- Freshness: `<when the observation must be made>`
- Negative control: `<what would falsify the claim>`

## Hard Boundary evidence

List only the checks required to protect safety, secrets, authorization, irreversible effects, or preserved product semantics.

- `<boundary>` -> `<minimum evidence>`

## Optional regression evidence

Add a test only when it materially protects the accepted effect or a previously observed failure.

- `<specific risk>` -> `<smallest test/check>`

Use `none` when direct effect observation is sufficient.

## Verdict

- `PASS` — the intended effect is observed and required boundaries hold.
- `NOT_SUPPORTED` — the effect was not observed, with honest evidence.
- `INVALID` — the method cannot support the claim.
- `BLOCKED` — required observation is currently unavailable.

Deployment, task completion, test count, document count, and Agent confidence are not verdicts.
