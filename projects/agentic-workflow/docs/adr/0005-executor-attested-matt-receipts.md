# ADR-0005: Require executor-attested Matt method receipts

- Status: Accepted
- Date: 2026-09-01

## Context

The user requires task decomposition, design, specification, development, testing, and review to follow Matt Skills. A worker-written field claiming a Skill name is self-attestation and does not prove that the digest-pinned method, gates, or completion criteria were followed.

## Decision

Require trusted, executor-attested proof for every cognitive Matt method and validate it independently before authorizing the next cognitive stage. Preserve per-Skill completion semantics rather than creating a global Matt state machine. The authoritative invocation and receipt contract lives in [`../solution-design.md`](../solution-design.md#6-matt-assurance).

## Consequences

- Matt becomes execution governance rather than prompt decoration.
- Skill availability and digest checks are mandatory preflight requirements.
- Cloud execution needs a versioned custom agent or equivalent handoff that can produce trusted receipts.
- Mechanical collectors and frozen test commands do not pay an unnecessary LLM or Skill cost.

## Alternatives considered

- Store only `skill_name` in task metadata: rejected as unverifiable self-report.
- Encode one fixed sequence of Matt Skills: rejected because the correct method depends on current uncertainty and evidence.
