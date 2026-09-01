# Spike 002: Requested and Actual Route Receipts

- Status: `PARTIAL`
- Type: Logic and live-evidence prototype
- Prototype branch: `prototype/agentic-route-receipts`
- Prototype commit: `00dfaac63fa06f996d016833bf84359acd7140da`

## Question

Can the workflow distinguish requested routing from actual main/subagent execution and enforce exact versus capability-class policy without trusting the worker's summary?

## Cheapest valid prototype

Define canonical Route Plan, Capability Snapshot, and Route Receipt payloads. Validate recorded real Copilot SDK evidence where a pinned parent session produced internal calls with other models/effort, plus synthetic missing/provider/tool/budget cases.

## Predeclared verdicts

- `VALIDATED`: exact routes reject every mismatched, unattested, `soft`, or `none` control/budget dimension and never fall back; an explicitly approved digest-bound external watchdog or hard cap is required; capability-class routes accept only digest-pinned candidates with per-candidate hard limits or approved watchdogs; receipts bind provider, model, effort, context, tools, usage, and parent/subagent identities.
- `PARTIAL`: main-session validation works but subagent or budget attestation remains unavailable and affected routes stay disabled.
- `INVALIDATED`: an exact mismatch passes, fallback can widen authority/cost, or worker prose can substitute for telemetry.
- `INCONCLUSIVE`: no trustworthy actual-call evidence is available.

## Evidence required

- canonical payload schemas and digests;
- real requested/actual route fixture provenance;
- exact mismatch matrix;
- capability-class allowlist matrix;
- Feng execution receipt with exact source hash and exit code.

## Verdict evidence

The prototype implements canonical Route Plan, Capability Snapshot, and Route Receipt payloads behind one `validate_route` seam. It includes a sanitized real-evidence-shaped parent/child drift fixture and never accepts worker summary prose as telemetry.

A Git bundle containing exact commit `00dfaac63fa06f996d016833bf84359acd7140da` was cloned into a new Feng `/tmp` workspace and executed with system Python. All `25` unittest cases passed. They cover every exact-route dimension mismatch, missing control/attestation, identity drift, fallback, hard/watchdog budget eligibility and overruns, capability-class candidate pinning/tampering, digest drift, malformed payloads, and requested-parent versus actual-child separation. The temporary Feng workspace and bundle were removed after execution.

The Route Receipt policy itself meets its predeclared `VALIDATED` criteria, but the broader solution design subsequently introduced two upstream contracts that this prototype does not exercise: projecting accepted Capability Snapshots into a Capability Matrix and freezing an accepted Route Plan as a Route Envelope. The overall Spike is therefore classified `PARTIAL`. Production still requires those transition tests, authenticated telemetry provenance, and independent proof that an external watchdog actually enforced its declared limits.

## Boundary

This does not enable any venue whose control, attestation, or budget-enforcement claim remains unproven.
