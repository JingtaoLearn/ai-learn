# Model Promotion and Deployment Registry Implementation Plan

- Owner issue: `#189`
- Status: design ready for implementation
- Scope: audited signal promotion and deployment; never broker or order execution

## Problem Statement

The platform can freeze and verify research evidence through Dataset Snapshots, operator versions,
Experiments, Attempts, Metric Documents, and Parameter Studies. It cannot turn one exact verified
result into an explicitly reviewed operational signal configuration. The existing BOCOM and
Au99.99 predictors therefore use a second control plane outside the platform: immutable production
manifests, pinned SHA-256 checks in deterministic scheduled scripts, and a separate active-production
registry.

That bridge prevents an ordinary experiment from changing production parameters, but it leaves
promotion, approval, active selection, rollback, runtime verification, and operator visibility split
between systems. A Study champion can also be mistaken for an operational authorization even though
research selection and production approval answer different questions.

The platform needs first-class Model Release, Promotion Approval, Deployment, Active Deployment, and
Signal Invocation concepts. It must preserve immutable evidence, require human authorization, select
at most one authoritative Deployment across
all environments for each asset-and-purpose Deployment Channel, let the scheduler consume only that
selection, and fail closed without replacing the prior Confirmed Signal State. It must migrate the two
existing external predictors without pretending that pre-registry evidence has native Study,
Experiment, or Attempt lineage.

## Solution

Add two deep modules at the existing catalog and worker seams:

1. A promotion registry owns release preview and creation, exact-digest stage decisions, immutable
   deployment creation, active-selection transitions, idempotency, authorization, and audit history.
2. A signal runtime first records one idempotent Signal Invocation, then resolves only its Active
   Deployment. It creates a Signal Run only after that binding exists, verifies the complete immutable
   binding, produces a candidate through a trusted producer adapter, and atomically confirms the result
   and one logical delivery event only if the binding is still authoritative.

SQLite remains the transactional fact store and WAL database. Content-addressed release and legacy
evidence bytes remain immutable below the configured state root. FastAPI/Jinja2 and JSON routes are
adapters over the promotion registry rather than alternate implementations. The scheduler calls the
single signal-runtime interface; it never resolves a Study, Experiment, Attempt, semantic `latest`,
or a filesystem pointer itself.

A Model Release starts at `EXPERIMENTAL`. Its effective Release Stage is derived from append-only
Promotion Approvals against its exact Release Digest. The only authority-increasing path is
`EXPERIMENTAL -> PAPER_FROZEN -> PRODUCTION_FROZEN`; retirement is
`PRODUCTION_FROZEN -> RETIRED` and terminal. Creation, evidence eligibility, approval, Deployment
creation, and activation remain separate actions. Rollback is a new audited activation of a prior
immutable Deployment, not mutation of either Deployment or Model Release.

## User Stories

1. As a researcher, I want to preview a native release only from one Parameter Study-derived canonical
   successful Attempt carrying MetricDocumentFactory-issued evidence, so that standalone execution
   success cannot become a native promotion source.
2. As a researcher, I want a Study-derived preview to show the source Study, champion Trial,
   Experiment binding, canonical Attempt, and holdout evidence, so that selection provenance is not
   collapsed into one score.
3. As a reviewer, I want standalone Experiment/Attempt input rejected as ineligible for native
   creation while the eligible Study preview still shows Study, Experiment, and Attempt lineage, so
   that lineage display is not confused with source eligibility.
4. As a reviewer, I want the preview to show exact template and operator versions, content digests,
   parameters, data identity, evaluator identity, execution identity, evidence freshness, and all
   disqualifying warnings, so that `latest` or mutable defaults cannot enter review.
5. As a researcher, I want release creation to require the preview digest I reviewed, so that a
   changed source or catalog resolution cannot be submitted through a stale page.
6. As a researcher, I want duplicate release submissions to return the same Model Release without
   creating duplicate history, so that retries are safe.
7. As an approver, I want research eligibility and approval shown separately, so that passing a
   Study gate never auto-promotes a release.
8. As a paper approver, I want to approve only the exact Release Digest from `EXPERIMENTAL` to
   `PAPER_FROZEN`, so that approval cannot float to replacement content.
9. As the production owner, I want a second explicit decision from `PAPER_FROZEN` to
   `PRODUCTION_FROZEN`, so that paper review does not imply production authority.
10. As the production owner, I want a changed digest, stale expected stage, missing predecessor
    approval, or disqualifying evidence warning to reject the decision atomically.
11. As an auditor, I want the effective Release Stage reconstructed from append-only decisions, so
    that an editable stage field is not the authority source.
12. As an operator, I want paper, production, retired, and experimental releases clearly separated
    in list and detail views, so that operational authority is visible at a glance.
13. As an operator, I want to create an immutable Deployment that binds an approved Model Release to
    an exact channel, environment, schedule, and runtime identity, so that operational configuration
    is reviewable.
14. As an operator, I want a Deployment Channel to be the canonical combination of asset and signal
    purpose only, so that paper and production activations share one exclusivity scope.
15. As an operator, I want `latest` resolution forbidden in Model Releases and Deployments, so that
    a later operator publication cannot alter production behavior.
16. As the production owner, I want activation to name both the expected current Deployment and the
    target Deployment, so that concurrent changes cannot silently overwrite one another.
17. As an operator, I want duplicate deployment and activation requests to be idempotent, so that a
    timeout and retry cannot create duplicate records or transitions.
18. As an auditor, I want every activation, replacement, deactivation, retirement, and Rollback to
    include the authenticated actor, time, reason, action ID, request digest, and result.
19. As an operator, I want Rollback to select a prior immutable Deployment through the same guarded
    transition as replacement, so that rollback receives no safety bypass.
20. As a scheduler, I want one Signal Invocation identified by Deployment Channel, scheduled time,
    and invocation contract version, so that its identity exists even when no Deployment is active.
21. As a scheduler, I want a duplicate invocation to return the stored Signal Invocation outcome and,
    only when one was bound, its Signal Run outcome, so that retries cannot recompute a recommendation.
22. As a signal recipient, I want every confirmed result attributable to the exact active Deployment,
    Release Digest, parameters, evidence, runtime, schedule, and immutable input-data digest.
23. As a signal recipient, I want only completed market information available before the action time
    to enter Signal Production, so that operational signals retain the platform's causal contract.
24. As an operator, I want any manifest, parameter, source digest, evidence, approval, stage, runtime,
    input-data, schedule, or active-generation mismatch to fail closed.
25. As an operator, I want a failed or interrupted Signal Run to preserve the prior Confirmed Signal
    State, so that failure cannot publish an unverified replacement.
26. As an operator, I want a release to be non-retirable while it backs an Active Deployment, so that
    retirement cannot silently deactivate or strand a scheduler.
27. As an auditor, I want post-identity Signal Invocation failures, failed Signal Runs, and expected
    governance rejections recorded with bounded, non-secret reason codes, while malformed or
    unauthenticated requests create no domain fact.
28. As a migration operator, I want the existing BOCOM and Au99.99 manifests and hashes imported as
    Legacy Promotion Evidence, so that their external provenance is retained.
29. As a migration operator, I want missing legacy Study, Experiment, Attempt, holdout, approval, or
    evaluator facts represented as missing or `LEGACY_UNKNOWN`, so that migration does not invent
    native lineage.
30. As the current release reviewer and production owner, I want to issue separate new, current-time
    paper and production approvals after reviewing each imported digest, so that an old manifest is
    evidence rather than a fabricated platform approval.
31. As a migration operator, I want the old scheduled scripts to remain authoritative until import,
    shadow comparison, approval, activation, and readback gates pass, so that migration does not
    weaken the current protection.
32. As an authenticated API client, I want JSON errors and conflict codes to distinguish validation,
    authorization, stale preview, stage conflict, active conflict, and integrity failure.
33. As a keyboard or mobile user, I want the same release, approval, deployment, and rollback flows
    in server-rendered HTML at desktop and 390 px, with or without JavaScript.
34. As a security owner, I want production approval and activation limited to server-resolved owner
    identities, so that possession of an authenticated research session is insufficient.
35. As a platform maintainer, I want existing Experiment, Attempt, Study, evaluator, and report
    behavior unchanged, so that promotion is downstream of research rather than embedded in it.
36. As a platform owner, I want signal production explicitly unable to create, route, submit,
    simulate, or reconcile orders or fills, so that this feature cannot acquire trading authority.
37. As a signal recipient, I want one immutable logical outbox event per confirmed run and a stable
    idempotency key, while being told that external transport is at least once and may duplicate after
    a worker crash, so that delivery guarantees are accurate.
38. As an operator, I want simultaneous paper and production activation attempts for the same asset
    and purpose to yield exactly one winner, so that changing environment cannot bypass exclusivity.

## Current Seams and Placement

The implementation should deepen existing seams instead of distributing governance across route
handlers, templates, scripts, and raw SQL.

- The catalog already provides SQLite WAL, foreign keys, a 30-second busy timeout, immediate
  transactions, contiguous numbered migrations, immutable catalog records, and canonical finite JSON.
  Promotion tables belong in the same database so stage and active-selection changes can be atomic.
- Experiment resolution already freezes template/operator versions and digests, Dataset Snapshot
  identity, exact parameters, and Execution Identity. Release creation must consume its resolved
  values; it must not implement a second selector resolver.
- The Metric Document factory and Evaluation Policy are the highest existing seam for trustworthy
  Attempt evidence. Promotion should verify through that seam rather than parsing report files or
  accepting caller-authored metrics.
- Parameter Study already owns frozen plans, Trial and Experiment bindings, selection outcome,
  Holdout Ledger, holdout outcome/freshness, execution-drift checks, append-only events, and
  idempotent action IDs. Study-aware release preview should query that public behavior rather than
  infer a champion from database rows.
- FastAPI/Jinja2 already exposes the same domain behavior through authenticated JSON and semantic
  HTML, with CSRF, same-origin checks, bounded strict JSON/form parsing, escaped errors, and secure
  response headers. Promotion routes remain adapters over one module interface.
- The serial worker loop already isolates Study advancement and Attempt execution with independent
  exception handling. Signal scheduling/delivery should use a separate worker path so a failed
  signal cannot stop research workers and research work cannot imply signal authorization.
- The existing `Settings`/`AuthManager` seam proves identity and an allowlist, but it does not express
  promotion roles. Extend that seam with protected fail-closed role mapping; do not create a parallel
  authorization implementation in routes or the registry.
- Existing browser acceptance covers desktop, 390 px, keyboard, JavaScript, and no-JavaScript
  behavior. Promotion extends that harness rather than creating a second UI test stack.

## Implementation Decisions

### Deep-module public interfaces

The promotion registry is the only public governance-mutation seam. Its minimal interface is:

- preview a Study-derived native source, or separately preview a legacy bundle, and return a canonical
  preview, preview digest, eligibility by target stage, and warnings;
- create a Model Release from that preview digest and an idempotency action ID;
- decide one explicit Release Stage transition against the expected stage and Release Digest;
- create one immutable Deployment from an exact release, channel, schedule, runtime identity, and
  action ID;
- transition one channel's Active Deployment using expected-current compare-and-swap semantics;
- query release detail/list and channel detail/history for API, UI, audit, and runtime use.

This interface hides canonicalization, evidence validation, authorization, migration provenance,
state-machine rules, transactions, constraint translation, and audit writes. Route handlers must not
write promotion tables or calculate effective stage. Tests exercise the registry with a temporary
real catalog; an extra repository seam is not introduced for one SQLite adapter.

The signal runtime presents one scheduler interface: invoke a channel for a canonical scheduled
instant, invocation ID, and invocation contract version. It first returns or creates a Signal
Invocation, including terminal pre-run outcomes. Only after it binds the channel's Active Deployment
does it create a Signal Run and return its confirmed, failed, or already-completed outcome. Release
lookup, approval reconstruction, active-generation checks, input freezing, candidate production,
confirmation, and logical outbox-event creation stay behind this interface. The trusted producer,
clock, market calendar/data reader, and delivery adapter are accepted dependencies at internal seams;
they are not exposed to scheduler callers.

A one-time legacy importer is an adapter into the registry's legacy preview/creation branch, not a
second registry interface. It cannot insert release, approval, or Deployment rows directly.

### Canonical identities and digests

All digests use lower-case SHA-256 over UTF-8 canonical JSON: sorted object keys, no insignificant
whitespace, exact arrays, no duplicate keys, and finite JSON values only. Timestamps, display labels,
actors, comments, database IDs, effective stage, approvals, Deployments, and active state are excluded
from Release Digest so governance never changes release content.

A native Model Release payload freezes:

- schema and release-contract versions;
- source kind `NATIVE`;
- exact Strategy Configuration;
- template name, exact version, content digest, and normalized parameters;
- every operator slot's stable ID, exact version, content digest, and normalized parameters;
- immutable Dataset Snapshot identity, canonical data digest, lineage, and evaluation interval;
- required Study ID, champion Trial digest, Experiment Binding, selection outcome, Holdout Ledger
  identity, holdout outcome, and Holdout Freshness;
- the bound Experiment ID and canonical successful Attempt ID/result digest selected by that Study;
- the digest of pristine `VerifiedMetricDocument` evidence issued by `MetricDocumentFactory`, plus
  exact evaluator/policy/metric-engine identities;
- Execution Identity, including source, dependency, runner image, protocol, and metric semantics;
- an explicit `automatic_ordering: false` capability assertion.

A legacy Model Release payload freezes:

- source kind `LEGACY_EXTERNAL` and the exact external model ID;
- normalized strategy/runtime fields proven by the imported manifest;
- the recomputed digest of every imported manifest/evidence artifact and the digest value asserted by
  the external control plane;
- the external active-binding and scheduled-script identities that can be verified without importing
  credentials;
- explicit null or absent native Study, Experiment, Attempt, Trial, Holdout, and Metric Document
  lineage; Holdout Freshness is `LEGACY_UNKNOWN` unless stronger evidence is truly available;
- `automatic_ordering: false`.

The release ID is the content address of the canonical release payload in v1. The separately named
Release Digest equals that content address and is repeated at trust seams to make exact-digest checks
explicit. Human-readable names and migration notes are non-authorizing metadata.

A Deployment Channel identity is the digest of only its canonical asset identifier and signal-purpose
identifier. It excludes environment, model version, schedule, runtime, and delivery destination.
Paper and production Deployments for the same asset and purpose therefore contend on one Active
Deployment projection and activation generation.

A Deployment identity is the digest of its schema version, channel identity, environment (`PAPER` or
`PRODUCTION`), Release Digest, canonical schedule digest, input-data-policy digest,
RuntimeIdentity digest, SignalOutputContract digest, ProducerAdapterBinding digest, DeliveryContract
digest, DestinationAdapterBinding digest, and `automatic_ordering: false`. The Deployment stores each
closed canonical object beside its recomputed digest; a missing object, digest mismatch, unknown trusted
adapter ID, or config field outside the closed contract fails creation and runtime verification. It
contains no `latest`, secret, import path, mutable path, or credential. Any environment, schedule,
parameter, runtime, release, producer, output, delivery, destination adapter, or data-semantics change
creates a new Deployment without creating a new channel.

A Signal Invocation identity is the digest of channel identity, canonical scheduled instant, and
invocation contract version. The caller-supplied invocation ID is a globally unique idempotency key
bound to that canonical request. Reusing it for another request conflicts; supplying a different ID for
an existing channel/instant/version also conflicts instead of creating a second invocation. Identity
does not include Active Deployment, generation, or environment, so the closed v1 pre-run outcomes
`NO_ACTIVE_DEPLOYMENT` and `INVALID_SCHEDULE` are durable and replayable without fabricating a runtime
binding.

A Signal Run identity is derived from Signal Invocation identity plus the bound activation generation
and Deployment identity. A Signal Run row cannot exist without those non-null bindings. The run
records the immutable operational input snapshot/data digest; this identity is distinct from the
research Dataset Snapshot frozen by the Model Release.

### Canonical runtime contracts

The following v1 contracts are authoritative; routes and adapters accept no aliases or extra fields.
The examples use all-zero digest placeholders and are illustrative, not promotable artifacts. Every
named contract digest is lower-case SHA-256 over that contract's complete UTF-8 canonical JSON object
using the rules above. The digest itself is stored beside, not inside, the object it identifies.

A `RuntimeIdentity` is exactly this closed object:

```json
{
  "schema_version": 1,
  "runtime_id": "python-signal-runtime-v1",
  "source_digest": "0000000000000000000000000000000000000000000000000000000000000000",
  "dependency_lock_digest": "0000000000000000000000000000000000000000000000000000000000000000",
  "runner_image_digest": "0000000000000000000000000000000000000000000000000000000000000000",
  "protocol_digest": "0000000000000000000000000000000000000000000000000000000000000000",
  "automatic_ordering": false
}
```

`schema_version` is integer `1`; `runtime_id` is a stable allowlisted ID; all four digests are lower-case
64-hex content identities; and `automatic_ordering` is the literal `false`. Paths, package ranges,
mutable image tags, process arguments, environment values, and credentials are invalid fields.

A schedule is exactly this closed object:

```json
{
  "schema_version": 1,
  "kind": "CRON",
  "expression": "40 8 * * 1-5",
  "timezone": "Asia/Shanghai",
  "calendar_id": "XSHG",
  "action_time": "NEXT_SESSION_OPEN",
  "misfire_policy": "REJECT"
}
```

`schema_version` is integer `1`; `kind` is `CRON`; `expression` is the normalized five-field cron
expression; `timezone` is an IANA name; `calendar_id` and `action_time` are stable allowlisted IDs;
and v1 `misfire_policy` is `REJECT`. A Signal Invocation carries an RFC 3339 UTC scheduled instant
with whole-second precision. The schedule seam recomputes the occurrence and rejects a non-occurrence,
duplicate local time, missing calendar session, or delayed catch-up instead of coercing it.

An input-data policy is exactly this closed object:

```json
{
  "schema_version": 1,
  "reader_id": "yahoo-chart-v8",
  "reader_digest": "0000000000000000000000000000000000000000000000000000000000000000",
  "asset_id": "601328.SS",
  "bar_interval": "1d",
  "value_field": "adjusted_close",
  "calendar_id": "XSHG",
  "completion_lag_seconds": 600,
  "lookback_sessions": 22,
  "max_staleness_sessions": 1,
  "availability_rule": "BAR_CLOSE_PLUS_LAG_BEFORE_SCHEDULED_INSTANT"
}
```

The reader ID/digest identify the data-reader adapter; asset and calendar must match the channel and
schedule; counts are bounded positive integers; and the sole v1 availability rule requires strict
`bar_close + completion_lag < scheduled_instant`. The reader returns normalized finite rows, ordered
by unique session, plus source-byte digests and one canonical `input_digest`. Raw credentials, URLs
containing credentials, and mutable cache paths are outside the contract.

A `SignalOutputContract` is exactly this closed object:

```json
{
  "schema_version": 1,
  "contract_id": "target-position-crossing-v1",
  "signal_purpose": "TARGET_POSITION",
  "asset_id": "601328.SS",
  "recommendations": ["BUY", "HOLD", "SELL", "WAIT"],
  "target_state": {
    "kind": "INTEGER_ENUM",
    "allowed_values": [-1, 0, 1]
  },
  "reason_codes": ["CROSSED_DOWN", "CROSSED_UP", "INSUFFICIENT_DATA", "NO_CROSSING"],
  "measurements": {
    "next_slope_pct": {
      "value_type": "NUMBER",
      "unit": "PERCENT",
      "minimum": -1000,
      "maximum": 1000
    },
    "previous_slope_pct": {
      "value_type": "NUMBER",
      "unit": "PERCENT",
      "minimum": -1000,
      "maximum": 1000
    }
  }
}
```

`contract_id`, purpose, and asset are stable normalized IDs and must agree with the channel. The three
arrays are non-empty, duplicate-free, and in lower-codepoint order in canonical form. V1
`target_state.kind` is only `INTEGER_ENUM`; every allowed value is a JSON integer (booleans are
rejected). Each measurement key matches `^[a-z][a-z0-9_]{0,63}$`; the output must contain exactly the
contract's keys. V1 `value_type` is only `NUMBER`; each minimum/maximum is finite with
`minimum <= maximum`; and each unit is one of `BASIS_POINTS`, `COUNT`, `PERCENT`, or `RATIO`. A new
key, enum member, reason code, range, or unit creates a new contract and digest.

A candidate and confirmed signal output share this closed object:

```json
{
  "schema_version": 1,
  "signal_purpose": "TARGET_POSITION",
  "asset_id": "601328.SS",
  "scheduled_instant": "2026-09-01T00:40:00Z",
  "information_cutoff": "2026-08-31T07:10:00Z",
  "recommendation": "BUY",
  "target_state": 1,
  "reason_code": "CROSSED_UP",
  "measurements": {
    "next_slope_pct": {
      "value": 0.21,
      "unit": "PERCENT"
    },
    "previous_slope_pct": {
      "value": 0.19,
      "unit": "PERCENT"
    }
  }
}
```

`recommendation`, `target_state`, and `reason_code` must be exact members of the bound
SignalOutputContract. Every measurement is exactly `{value, unit}`: `value` is a finite JSON number
within the declared inclusive range and `unit` exactly equals the declared unit. Missing, additional,
non-finite, boolean, numeric-string, or unit-coerced values fail closed. Timestamps are canonical UTC;
asset, purpose, and scheduled instant equal the invocation binding; and `information_cutoff` is earlier
than the scheduled instant. The output digest covers this complete object. Explanatory prose, when
retained separately, is bounded non-authorizing metadata and is excluded from that digest.

A `ProducerAdapterBinding` is exactly this closed object:

```json
{
  "schema_version": 1,
  "adapter_id": "bocom-crossing-producer-v1",
  "adapter_contract_version": 1,
  "implementation_digest": "0000000000000000000000000000000000000000000000000000000000000000",
  "dependency_digest": "0000000000000000000000000000000000000000000000000000000000000000",
  "runtime_identity_digest": "0000000000000000000000000000000000000000000000000000000000000000"
}
```

The trusted producer registry maps each allowlisted `adapter_id` to a source-reviewed in-process
implementation constructed by application composition. Deployments cannot provide a module name,
class name, import path, executable path, URL, or plugin locator. The registry recomputes and compares
the implementation/dependency digests and requires `runtime_identity_digest` to equal the Deployment's
RuntimeIdentity digest before returning the adapter.

The trusted producer adapter satisfies one internal interface (types in braces are closed objects):

```text
produce({
  contract_version: 1,
  deployment_digest: LowerHex64,
  release_digest: LowerHex64,
  runtime_identity_digest: LowerHex64,
  producer_adapter_binding_digest: LowerHex64,
  signal_output_contract_digest: LowerHex64,
  scheduled_instant: Rfc3339UtcSecond,
  input_digest: LowerHex64,
  normalized_rows: [{session, available_at, values}]
}) -> {signal_output, producer_evidence}
```

The request is immutable and contains no delivery destination or credentials. Each row has one ISO date
`session`, one canonical UTC `available_at`, and exactly the finite value keys declared by the
input-data policy. `signal_output` must satisfy the bound SignalOutputContract.
`producer_evidence` is exactly `{schema_version: 1, producer_adapter_binding_digest: LowerHex64,
input_digest: LowerHex64, calculation_digest: LowerHex64}` and must repeat the request's binding and
input digests. The call performs no external delivery and no catalog write. Expected producer failures
return an allowlisted code; exceptions are internal failures.

A `DestinationAdapterBinding` is exactly this closed object:

```json
{
  "schema_version": 1,
  "adapter_id": "lark-message-v1",
  "adapter_contract_version": 1,
  "implementation_digest": "0000000000000000000000000000000000000000000000000000000000000000",
  "dependency_digest": "0000000000000000000000000000000000000000000000000000000000000000",
  "protocol_digest": "0000000000000000000000000000000000000000000000000000000000000000"
}
```

Destination adapter IDs use a separate source-reviewed trusted registry with the same no-import-path
rule as producer adapters. A `DeliveryContract` is exactly this closed object:

```json
{
  "schema_version": 1,
  "contract_id": "confirmed-signal-delivery-v1",
  "destination_ref": "quant-signals-bocom",
  "destination_adapter_binding_digest": "0000000000000000000000000000000000000000000000000000000000000000",
  "payload_contract": "CONFIRMED_SIGNAL_V1",
  "delivery_semantics": "AT_LEAST_ONCE",
  "provider_idempotency_mode": "PASS_WHEN_SUPPORTED"
}
```

`destination_ref` is a non-secret normalized logical ID matching `^[a-z][a-z0-9-]{0,63}$`; it is not a
chat ID, webhook, URL, account, token, or credential. The remaining string fields have exactly the
shown v1 values except for stable `contract_id`. At startup, the delivery worker resolves
`destination_ref` through this separate closed runtime config, which is a regular non-symlink UTF-8 JSON
file owned by the service account with mode `0600`:

```json
{
  "schema_version": 1,
  "destinations": [
    {
      "destination_ref": "quant-signals-bocom",
      "adapter_id": "lark-message-v1",
      "provider_destination": "opaque-provider-destination",
      "credential": "opaque-provider-credential"
    }
  ]
}
```

Destination refs are unique; each adapter ID must match the trusted DestinationAdapterBinding selected
by its DeliveryContract; and the two opaque provider strings are bounded non-empty values interpreted
only by that trusted adapter. Settings validates ownership, mode, strict JSON, exact fields, required
refs, and adapter matches fail closed before worker construction. Resolved destination material and
credentials never enter the catalog, audit, application logs, exceptions returned to callers,
canonical objects, or digests.

Every attempt sends the same canonical-serialized `DeliveryRequest` bytes; retries change no request
field:

```json
{
  "schema_version": 1,
  "event_id": "0000000000000000000000000000000000000000000000000000000000000000",
  "idempotency_key": "0000000000000000000000000000000000000000000000000000000000000000",
  "delivery_contract_digest": "0000000000000000000000000000000000000000000000000000000000000000",
  "destination_adapter_binding_digest": "0000000000000000000000000000000000000000000000000000000000000000",
  "destination_ref": "quant-signals-bocom",
  "payload_digest": "0000000000000000000000000000000000000000000000000000000000000000",
  "payload": {
    "schema_version": 1,
    "signal_invocation_id": "0000000000000000000000000000000000000000000000000000000000000000",
    "signal_run_id": "0000000000000000000000000000000000000000000000000000000000000000",
    "deployment_digest": "0000000000000000000000000000000000000000000000000000000000000000",
    "release_digest": "0000000000000000000000000000000000000000000000000000000000000000",
    "input_digest": "0000000000000000000000000000000000000000000000000000000000000000",
    "output_digest": "0000000000000000000000000000000000000000000000000000000000000000",
    "signal_output": {
      "schema_version": 1,
      "signal_purpose": "TARGET_POSITION",
      "asset_id": "601328.SS",
      "scheduled_instant": "2026-09-01T00:40:00Z",
      "information_cutoff": "2026-08-31T07:10:00Z",
      "recommendation": "BUY",
      "target_state": 1,
      "reason_code": "CROSSED_UP",
      "measurements": {
        "next_slope_pct": {
          "value": 0.21,
          "unit": "PERCENT"
        },
        "previous_slope_pct": {
          "value": 0.19,
          "unit": "PERCENT"
        }
      }
    }
  }
}
```

`event_id` is the logical outbox-event identity; `idempotency_key` is exactly equal to it and is passed
to the provider when supported. `payload_digest` identifies the complete closed `payload` object;
`output_digest` identifies its complete `signal_output`. All identity fields are lower-case 64-hex and
must match the confirmed run and Deployment bindings. The adapter returns exactly one closed
`DeliveryResult`:

```json
{
  "schema_version": 1,
  "status": "DELIVERED",
  "reason_code": "PROVIDER_ACCEPTED",
  "provider_receipt_id": "receipt-123",
  "retry_after_seconds": null
}
```

`status` is `DELIVERED`, `RETRY`, or `REJECTED`. `DELIVERED` requires reason
`PROVIDER_ACCEPTED`, optional bounded non-secret receipt ID, and null retry delay; it means only that
this call observed provider acceptance. `RETRY` requires one of `AMBIGUOUS_PROVIDER_RESULT`,
`PROVIDER_RATE_LIMITED`, `PROVIDER_TEMPORARY_FAILURE`, or `TRANSPORT_UNAVAILABLE`, an optional integer
delay from 0 through 86400, and null receipt. `REJECTED` requires one of `DESTINATION_NOT_FOUND`,
`PAYLOAD_REJECTED`, or `PROVIDER_AUTH_REJECTED`, with null receipt and delay. Reason/status combinations
outside those exact sets fail closed. Raw provider bodies and exception text are never result fields.
Each attempt/result is recorded separately; a crash after provider acceptance can leave no recorded
`DELIVERED` result and cause the same DeliveryRequest to be sent again. No result claims exactly-once
external delivery.

### Release preview and evidence eligibility

Release preview resolves the source and immediately re-verifies all immutable bytes and catalog
bindings. It never accepts caller-supplied resolved operators, metrics, lineage, stage, or digests as
facts. Native preview accepts a Parameter Study ID and champion Trial identity only. The Study must
have `CHAMPION_SELECTED`, the required holdout outcome under the versioned gate, one exact Experiment
Binding, one canonical successful Attempt, and pristine `VerifiedMetricDocument` evidence issued by
`MetricDocumentFactory` for that Attempt. The Study, Trial, Experiment, Attempt, Metric Document,
selection, Holdout Ledger, holdout outcome/freshness, and Execution Identity must agree exactly.
Standalone Experiment IDs or Attempt IDs are rejected as `NATIVE_SOURCE_REQUIRES_STUDY`; they may be
shown as lineage inside an eligible Study preview but cannot be submitted as native sources. Legacy
preview is the separate `LEGACY_EXTERNAL` branch defined below and never passes through native
eligibility by supplying null lineage.

Warnings are structured as code, severity, affected identity, explanation, and stage impact. Integrity
failures, unresolved `latest`, non-canonical/failed Attempts, result divergence, execution drift,
tampered evidence, mismatched champion bindings, automatic-ordering capability, and stale preview are
disqualifying. Missing Study/holdout lineage, prior holdout exposure, `LEGACY_UNKNOWN` freshness, old
evidence, and migration provenance remain visible and are evaluated by a versioned stage-gate policy;
they are never silently converted into approval.

The preview reports eligibility separately for release creation, paper approval, production approval,
and deployment activation. Explicit approval is still required when evidence is eligible. Approval
cannot override a disqualifying integrity or capability warning. Stage-gate policy identity and digest
are captured in each approval so a future policy change does not reinterpret an old decision.

Release creation re-runs resolution inside its transaction and requires the expected preview digest.
An exact duplicate source/payload returns the existing release; caller-ID replay/conflict behavior is
owned by **Transactionality and idempotency** below.

### Release Stage and approval state machine

The effective stage is a fold over a release's append-only Promotion Approvals:

1. A newly created release is `EXPERIMENTAL` without an approval row.
2. `EXPERIMENTAL -> PAPER_FROZEN` requires an authenticated paper approver, exact expected stage,
   exact Release Digest, eligible evidence under the recorded policy, a reason, and an action ID.
3. `PAPER_FROZEN -> PRODUCTION_FROZEN` requires the current production owner and a new decision. The
   paper approval cannot be reused as production approval.
4. `PRODUCTION_FROZEN -> RETIRED` requires the production owner, a reason, and proof in the same
   transaction that no channel currently selects a Deployment backed by the release.
5. `RETIRED` is terminal. Equivalent behavior must use a new Model Release and new approvals.

Direct experimental-to-production, skipped paper review, unretirement, approval reuse, digest
substitution, backdating, and stage inference from Study/Experiment status are invalid. Authenticated
wrong-role and expected policy rejections are action/audit outcomes but never Promotion Approvals;
unauthenticated requests follow the no-domain-fact rule below. Rollback changes Active Deployment only
and cannot change Release Stage.

### Deployment and active-selection state machine

Deployment creation requires the release to have the minimum stage for the Deployment's environment:
`PAPER_FROZEN` for `PAPER`, or `PRODUCTION_FROZEN` for `PRODUCTION`. It re-verifies the exact approval
chain, release artifact, schedule, runtime identity, signal-only capability, and asset-and-purpose
channel identity. Deployment rows are immutable. Duplicate canonical content converges to one
Deployment.

A channel has one integer activation generation and either zero or one Active Deployment across all
environments. Its permitted transitions are:

- inactive to active: `ACTIVATE`;
- active A to active B: `REPLACE`;
- active A to inactive: `DEACTIVATE`;
- active A to a previously active immutable B: `ROLLBACK`.

Every operation supplies expected current Deployment (or explicit empty state), target when applicable,
reason, action ID, and AuthorityContext. The immediate transaction loads the actual current/target rows,
applies request-intrinsic authorization from the operation and requested target environment, and then
compares the expected generation/current value. A stale compare-and-swap request records
`STALE_ACTIVE_SELECTION` before environment-dependent displacement authorization because it cannot
commit any transition. Only a still-current request proceeds to authorization against both actual
current and target environments, followed by target channel, required stage, non-retired release,
approval-chain, and target-integrity checks. It then appends the transition/audit event, updates the
projection, and increments the generation atomically. Rejection and replay follow the authoritative
**Transactionality and idempotency** protocol; there is no newest-wins, fallback, or implicit activation
after creation.

The channel-keyed unique projection and immediate transaction enforce at most one active Deployment
across PAPER and PRODUCTION. The required concurrency test uses two real connections but calls the
public transition seam with independently authorized reviewer/PAPER and owner/PRODUCTION contexts from
the same expected empty generation. Exactly one commits, the other stores the stale-selection outcome,
and history remains contiguous.

Rollback receives no special bypass: its target must still be immutable, intact, correctly staged,
and compatible with the same channel. Rollback to a retired, tampered, wrong-channel, or runtime-stale
Deployment fails. Ensemble selection is unsupported in v1.

### Schema and migrations

Add exactly catalog migration 10 after the current Parameter Study schema 9. Application composition
must accept and apply exactly the contiguous history `1..10` before constructing the registry or
runtime. Upgrade from a populated `1..9` catalog is the required path; startup fails closed on a gap,
on a future version, or when the post-startup history is anything other than `1..10`. Migration 10 is
additive and does not rewrite existing Experiment, Attempt, Study, evidence, or operator rows.

The migration adds:

- `model_releases`: release ID/digest, canonical payload, source kind, creator, creation time, and
  non-authorizing display metadata; immutable update/delete triggers and a unique digest;
- `legacy_promotion_evidence`: release, ordered artifact kind, recomputed SHA-256, claimed SHA-256,
  content-addressed locator, observation/import metadata, and immutable triggers;
- `promotion_actions`: globally unique caller action ID, operation, canonical request digest, actor,
  immutable canonical outcome bytes/result identity, and time for replay; one row represents only the
  first claim of that action ID;
- `idempotency_conflicts`: deterministic conflict ID, namespace, bounded reason code, actor, presented
  caller ID/request digest/canonical identity, ordered incumbent claim IDs/digests/identities, immutable
  canonical conflict-response bytes, and time; append-only triggers plus uniqueness on conflict ID
  make an identical conflict replay the stored row;
- `promotion_approvals`: release/digest, source/target stages, policy identity/digest, actor, reason,
  action, and time; append-only triggers and uniqueness preventing duplicate stage authority;
- `deployment_channels`: canonical asset/purpose tuple and channel digest; immutable and unique without
  environment;
- `deployments`: deployment ID/digest, channel, environment, release/digest, canonical schedule and
  exact schedule/input-policy/RuntimeIdentity/SignalOutputContract/ProducerAdapterBinding/
  DeliveryContract/DestinationAdapterBinding objects and digests, signal-only assertion, creator, and
  time; immutable;
- `deployment_transitions`: channel sequence/generation, kind, expected previous, target, actor,
  reason, action, and time; append-only and ordered uniquely per channel;
- `active_deployments`: one mutable projection row per channel containing selected deployment and
  generation; writable only through registry transactions;
- `signal_invocations`: invocation identity, globally unique caller invocation ID, channel, canonical
  scheduled instant, contract version, canonical request digest, immutable canonical outcome bytes, and
  required outcome code (`BOUND_TO_RUN`, `NO_ACTIVE_DEPLOYMENT`, or `INVALID_SCHEDULE`); a separate
  unique constraint owns canonical channel/instant/version, and `BOUND_TO_RUN` requires exactly one
  related run at transaction commit;
- `signal_runs`: run identity, required Signal Invocation, frozen channel generation, required
  Deployment/release identities, immutable input digest, bounded lifecycle status/reason, output
  digest, and times; a foreign-key/check constraint forbids an unbound run;
- `confirmed_signal_states`: one mutable projection per channel pointing to the latest confirmed run;
- `signal_delivery_outbox`: one immutable logical event and canonical DeliveryRequest per confirmed run,
  stable event ID/idempotency key, bound DeliveryContract and DestinationAdapterBinding digests, logical
  non-secret destination ref, and no resolved destination or credential material; claim state and each
  immutable DeliveryResult attempt/receipt are separate from the event payload;
- `promotion_audit_events`: ordered immutable actor/action/entity/event/outcome records with bounded
  redacted metadata.

Use foreign keys, strict status checks, lower-case 64-hex checks, canonical JSON validation, required
indexes for release/stage/channel/run lists, and immutable or append-only triggers. Mutable projections
and lifecycle rows receive guarded transition methods and tests; they are never caller-writable. The
migration test upgrades a populated schema 9 catalog to exactly `1..10`, proves every old row
unchanged, and proves repeated startup is a no-op. This implementation scope also requires changing
`deploy/deploy-release.sh` to set `EXPECTED_SCHEMA_VERSION=10`, changing `deploy/README.md` to promise
contiguous migrations 1 through 10, and updating the schema setup, fake restart migration, rollback
expectations, success expectation, and exact-version failure cases in `tests/test_deployment.py`.
Production verification must accept exactly `[1,2,3,4,5,6,7,8,9,10]`; it must reject `1..9`, any gap,
duplicate, reordered, or future version and restore the schema-9 backup on failure.

Content-addressed release/legacy evidence is staged privately, verified, fsynced, atomically renamed,
and sealed before a database row can reference it. Existing bytes at the target must compare exactly
or fail as corruption; no overwrite or silent repair is allowed. Database backup/restore remains the
production rollback for a failed schema migration.

### Transactionality and idempotency

Strict parsing and authentication precede idempotency. A caller ID is 16 through 128 ASCII
characters matching `^[A-Za-z0-9][A-Za-z0-9._:-]*$`: governance calls name it `action_id`; scheduler
calls name it `invocation_id`. For either namespace, the canonical request digest is SHA-256 over
canonical JSON containing `schema_version: 1`, the exact operation, authenticated `principal_id`, and
the complete normalized command payload excluding only the caller ID. Thus another operation, actor,
or payload produces another digest; credentials, CSRF material, transport headers, display fields, and
server time never enter it.

Transaction behavior is partitioned precisely:

1. **Before a canonical claim:** malformed, oversized, duplicate-key, non-finite, bad-origin, bad-CSRF,
   or unauthenticated requests create no action, idempotency conflict, audit event, Signal Invocation,
   Signal Run, authority fact, or projection write. Transport/security logs may record bounded metadata
   outside the domain catalog.
2. **First deterministic claim:** one immediate transaction finds neither incumbent claim, evaluates
   authorization and domain rules, and stores the caller ID, operation, canonical request digest,
   actor, and complete canonical outcome-envelope bytes. Success stores those bytes atomically with all
   authority facts/projections; an expected rejection such as wrong role, ineligible evidence, stale
   state, digest mismatch, `NO_ACTIVE_DEPLOYMENT`, or `INVALID_SCHEDULE` stores the rejection bytes and
   audit fact but no approval, deployment, transition, run, outbox event, or projection change. The
   closed Signal Invocation outcome set is exactly `BOUND_TO_RUN`, `NO_ACTIVE_DEPLOYMENT`, and
   `INVALID_SCHEDULE`.
3. **Internal failure:** unexpected exceptions, exhausted SQLite busy retry, storage/fsync failure,
   constraint bugs, and adapter crashes roll back every provisional claim, outcome, conflict, audit,
   authority fact, run, event, and projection. The unavailable/internal transport response is not an
   idempotency outcome; a later retry may become the first deterministic claim.

A stored action or invocation outcome is canonical finite JSON serialized with sorted keys, separators
`(',', ':')`, `ensure_ascii=False`, `allow_nan=False`, and no trailing newline. Its operation-specific
closed envelope contains `schema_version`, `outcome_code`, bounded `reason_code` or null, and exact
result identities or null. The registry and signal-runtime public seams return those stored bytes
without reconstruction, policy re-evaluation, or a new timestamp. JSON adapters forward them
byte-for-byte; HTML applies presentation after consuming the same stored envelope and cannot alter its
meaning.

The lookup protocol inside one immediate transaction is exact. Governance actions load their caller-ID
incumbent only: a matching operation/request digest replays stored bytes, a different digest records or
replays `CALLER_ID_REUSED`, and no incumbent permits the first claim.

Signal Invocation loads both the caller-ID incumbent and canonical channel/instant/version incumbent
before classifying either:

1. If both lookups resolve to the same row and its operation/request digest match, return its stored
   outcome bytes.
2. If both incumbents exist but are different rows, return/record `INVOCATION_CLAIMS_DIVERGE` with both
   incumbents in claim-kind order. This combined case is classified before either single-claim conflict
   and is therefore reachable.
3. If a caller-ID incumbent exists but step 1 did not match, return/record `CALLER_ID_REUSED`. A matching
   caller whose canonical lookup is unexpectedly absent is catalog corruption and follows internal
   failure semantics rather than becoming a deterministic conflict.
4. If only the canonical-identity incumbent exists under another caller ID, return/record
   `CANONICAL_INVOCATION_ALREADY_CLAIMED`.
5. If neither incumbent exists, insert and evaluate the first claim. Unique constraints on caller ID and
   canonical invocation identity are final race guards; a losing insert restarts the complete two-lookup
   classification, never domain work.

Each result in steps 2 through 4 changes no authority fact, audit authority, or projection. It inserts
or replays one immutable `idempotency_conflicts` row whose `conflict_id` is SHA-256 over this complete
canonical conflict identity:

```json
{
  "schema_version": 1,
  "namespace": "SIGNAL_INVOCATION",
  "reason_code": "CANONICAL_INVOCATION_ALREADY_CLAIMED",
  "actor_id": "signal-scheduler",
  "presented": {
    "caller_id": "invoke-20260901-004000-bocom-retry",
    "request_digest": "0000000000000000000000000000000000000000000000000000000000000000",
    "canonical_identity": "0000000000000000000000000000000000000000000000000000000000000000"
  },
  "incumbents": [
    {
      "claim_kind": "CANONICAL_IDENTITY",
      "record_id": "0000000000000000000000000000000000000000000000000000000000000000",
      "caller_id": "invoke-20260901-004000-bocom",
      "request_digest": "0000000000000000000000000000000000000000000000000000000000000000",
      "canonical_identity": "0000000000000000000000000000000000000000000000000000000000000000"
    }
  ]
}
```

`namespace` is `PROMOTION_ACTION` or `SIGNAL_INVOCATION`; `reason_code` is one of the three codes
above; action conflicts use null canonical identities; incumbent entries are unique and ordered
`CALLER_ID`, then `CANONICAL_IDENTITY`. The row also stores server time and the canonical conflict
response bytes, but neither enters `conflict_id`. IDs are bounded to 128 characters, digests are
lower-case 64-hex, reasons are bounded enums, and actor is the authenticated principal. A unique
`conflict_id` makes an identical conflict replay the original bytes and time. In particular, a globally
unique `promotion_actions.action_id` row remains the first request only; it cannot and does not store a
second request that reused that ID.

Same canonical immutable release or Deployment content under a fresh caller ID may converge to the
existing resource and store `NO_CHANGE`; that is resource deduplication, not idempotency replay. An
expected-current mismatch stores its own stale outcome and never retries as newest-wins. SQLite
busy/locked is a transient internal failure, never permission to bypass compare-and-swap.

### Legacy BOCOM and Au99.99 import

The importer handles exactly two external models and reads an explicit allowlist; it never scans a
home directory. The authoritative current sources are:

| Model | Reviewed sources |
| --- | --- |
| `bocom-20d-ema5-hysteresis-crossing-v1.2` (`601328.SS`) | `~/.hermes/data/quant-production-models/bocom-20d-ema5-hysteresis-crossing-v1.2.json`; its entry in `~/.hermes/data/quant-production-models/active-production.json`; `~/.hermes/scripts/bocom_trend_daily_action.py`; and job `297c11cad0dc` in `~/.hermes/cron/jobs.json` |
| `gold-au9999-ols55-ema1-b0175-s0275-v1` (`SGE.AU9999`) | `~/.hermes/data/quant-production-models/gold-au9999-ols55-ema1-b0175-s0275-v1.json`; `~/.hermes/data/quant-production-models/gold-production-manifest.sha256`; its entry in `~/.hermes/data/quant-production-models/active-production.json`; `~/.hermes/scripts/gold_slope_daily_action.py`; and job `1cd5557264db` in `~/.hermes/cron/jobs.json` |

`gold-report-terms.json`, runtime action logs, downloaded market data, report pages, delivery targets, and
all other home-directory content are not import sources. The importer accepts regular non-symlink
files only, applies per-file size limits (64 KiB JSON/text and 128 KiB Python), and reads no environment
file, cookie, token, SSH material, or credential.

Implementation creates bounded redacted fixtures at exactly:

- `tests/fixtures/model_promotion/legacy/bocom/{manifest.json,active-binding.json,cron-job.json,producer.py,claimed-hashes.json}`;
- `tests/fixtures/model_promotion/legacy/gold/{manifest.json,active-binding.json,cron-job.json,producer.py,claimed-hashes.json}`.

Fixture `cron-job.json` retains only `id`, `name`, `script`, `no_agent`, schedule expression/timezone,
and the audit-copy fields needed for parity. `active-binding.json` contains only asset, model ID,
manifest basename/digest, script basename, and cron job ID. Fixture producers are deterministic
synthetic/redacted equivalents with no network or delivery code. Fixture manifests remove report URLs
or other nonessential external locators. Every fixture is small enough for review and contains no
production chat IDs, destinations, user IDs, credentials, raw observations, or secrets.

Digest and executable-material rules are:

1. For each authoritative JSON file, preserve original bytes and record `raw_sha256`; parse strict
   UTF-8 with duplicate keys and non-finite values rejected; serialize the allowlisted projection with
   sorted keys, separators `(',', ':')`, `ensure_ascii=False`, `allow_nan=False`, and no trailing
   newline; then record its distinct `canonical_sha256`. Existing pinned manifest hashes are compared
   to `raw_sha256`, never silently reinterpreted as canonical hashes.
2. Parse `gold-production-manifest.sha256` as exactly lower-case 64-hex, two spaces, the expected
   manifest basename, and one LF. For BOCOM, compare the matching raw manifest digest independently
   across the active-registry entry, producer constant, and cron audit copy; none is treated as an
   additional approval.
3. The only executable artifact kinds are `PYTHON_SIGNAL_PRODUCER`, allowlisted to the two exact
   `~/.hermes/scripts/*.py` paths above. Record raw bytes and SHA-256, parse with Python AST, and inspect
   only allowlisted literal contract assignments. The importer never imports or executes source code,
   follows imports, or accepts shell, bytecode, notebook, archive, generated executable, or additional
   Python material. Any dynamic value needed as evidence is `LEGACY_UNKNOWN`, not evaluated.
4. The cron projection and active-binding projection use the same canonical JSON rules. Their job ID,
   basename, model/asset, schedule, `no_agent=true`, manifest digest, parameters, signal semantics, and
   `automatic_ordering=false` must agree with the manifest and producer literals.

Validation is preview-first and deterministic: copy reviewed bytes into a private staging directory;
reject type/path/size/secret-pattern violations; compute all raw digests; strict-parse and canonicalize
projections; verify every cross-source invariant; produce a redacted discrepancy report; then call the
normal legacy release preview with the expected bundle digest. Execution repeats all checks inside the
registry action, seals content-addressed bytes, and creates or returns the legacy Model Release.
Identical replay is `NO_CHANGE`; changed bytes under the same external model ID are a conflict. The
fixture workflow runs without home-directory access and must produce the same validation decisions and
canonical projections as the reviewed-source workflow.

The importer does not manufacture native IDs. Study, Experiment, Attempt, Trial, Metric Document,
holdout, evaluator, and platform approval fields stay absent and are displayed as
`LEGACY_EXTERNAL`/`LEGACY_UNKNOWN`. External manifest stage or promotion text and active-registry state
are Legacy Promotion Evidence only, never historical Promotion Approvals.

After import, each release starts `EXPERIMENTAL`. The protected role mapping resolves the current
release reviewer and production owner. The reviewer issues the new paper approval and the owner issues
the separate production approval at the actual migration time under the legacy stage-gate policy. One
identity may perform both only when the protected map explicitly assigns both roles. Those decisions
cite Legacy Promotion Evidence and its limitations and are never backdated.

Before cutover, run both old producers and the new producer adapters in delivery-suppressed shadow mode
against the same checked-in normalized input fixtures and injected scheduled instants. For every case,
compare canonical schedule occurrence, completed-session cutoff, normalized-input digest,
recommendation, target state, allowlisted reason code, finite measurements, complete signal-output
JSON, and output digest exactly. Include at least BUY, SELL, HOLD/WAIT, stale input, pre-close, missing
session, and duplicate invocation. Any mismatch blocks activation; approximate numeric or prose parity
is insufficient. Shadow runs contain no delivery destination and cannot mutate Active Deployment or
Confirmed Signal State.

### JSON and HTML flows

The JSON adapter exposes resource-oriented list/detail reads and explicit preview/mutation commands:

- release preview, release creation, release list/detail, and release approval/retirement;
- Deployment Channel list/detail/history, Deployment creation/detail, and guarded active transition;
- Signal Invocation and bound Signal Run list/detail, plus current Confirmed Signal State and logical
  delivery status;
- legacy import preview/execution only when migration mode and owner authorization are both enabled.

Mutations require authentication, exact same origin, CSRF for session clients, an action ID, strict
bounded JSON, and expected preview/stage/current fields. Success distinguishes `CREATED`, `NO_CHANGE`,
`APPROVED`, `ACTIVATED`, `REPLACED`, `DEACTIVATED`, and `ROLLED_BACK`. Stable JSON error codes cover
invalid input, stale preview, action conflict, forbidden role, ineligible evidence, invalid stage,
stale active selection, integrity failure, and unavailable catalog.

The server-rendered UI provides:

- separate Experimental, Paper, Production, and Retired release filters/counts;
- release preview with source lineage, exact parameter/version/digest tables, evidence freshness,
  warnings, stage eligibility, and explicit signal-only language;
- a confirmation page that repeats exact digest, source/target stage, warnings, and required reason;
- immutable release detail with complete approval and Deployment history;
- Deployment creation with canonical channel/schedule/runtime summary and no `latest` selector;
- channel detail with current generation, Active Deployment, prior transitions, guarded replace,
  deactivate, and Rollback forms;
- Signal Invocation, bound Signal Run, logical delivery, and Confirmed Signal State status without
  presenting pre-run failure or failed candidate output as current;
- legacy badges and explicit missing-lineage text for both imported models.

All critical information and mutations work without JavaScript. JavaScript may enhance preview and
confirmation but cannot relax server validation. Forms retain values after errors, use field-level and
summary errors, visible focus, semantic headings/tables, and confirmation language that never says
trade, buy order, sell order, fill, or execution when it means a signal.

### Authorization and audit

Authentication remains responsible for verified identity. Extend `Settings` with promotion-enabled,
role-map, machine-credential, and destination-config paths. Extend `AuthManager` as the single human and
machine authentication seam; routes, workers, and the registry cannot construct authority themselves.
The role-map file is regular non-symlink UTF-8 JSON owned by the service account with mode `0600` and
has exactly this closed shape:

```json
{
  "schema_version": 1,
  "human_assignments": {
    "reviewer@example.invalid": ["RELEASE_REVIEWER"],
    "owner@example.invalid": ["PRODUCTION_OWNER"]
  },
  "machine_assignments": {
    "signal-delivery-worker": ["SIGNAL_DELIVERY_WORKER"],
    "signal-scheduler": ["SIGNAL_SCHEDULER"]
  }
}
```

Human roles are exactly `VIEWER`, `RESEARCHER`, `RELEASE_REVIEWER`, and `PRODUCTION_OWNER`; machine
roles are exactly `SIGNAL_SCHEDULER` and `SIGNAL_DELIVERY_WORKER`. A principal appears in exactly one
assignment kind; machine principals receive exactly one machine role and no human role. Validation
rejects duplicate JSON keys, unknown fields/roles, non-normalized or non-allowlisted human identities,
duplicate role entries, shared human/machine IDs, symlinks, unsafe ownership/permissions, empty
assignments, and unreadable files. When promotion is enabled, startup requires a release reviewer,
production owner, and scheduler; it also requires a delivery worker when delivery is enabled. No role
is inferred from email domain, allowlist membership, display name, another role, or password fallback.

Machine secrets live only in a separate regular non-symlink mode-`0600` runtime file owned by the
service account. Its exact closed shape is:

```json
{
  "schema_version": 1,
  "credentials": [
    {
      "principal_id": "signal-delivery-worker",
      "key_id": "delivery-2026-09",
      "bearer_token": "BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
    },
    {
      "principal_id": "signal-scheduler",
      "key_id": "scheduler-2026-09",
      "bearer_token": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    }
  ]
}
```

Tokens are independently generated 32-byte values encoded as 43-character unpadded base64url; the
shown values are non-production placeholders. `principal_id` must exist in `machine_assignments`; key
IDs are globally unique normalized IDs; tokens are unique; and at most two keys may be active per
principal. Rotation adds a new key, restarts/reloads successfully, moves callers, then removes the old
key; removing all scheduler keys fails validation. A machine request sends `X-Machine-Key-Id` and
`Authorization: Bearer <token>` over the existing protected transport. `AuthManager` looks up the key,
compares the token in constant time, and derives the principal and role from server config. It never
accepts a caller-supplied principal or role. Session/SSO credentials cannot authenticate a machine
principal, and machine credentials cannot authenticate browser routes.

After authentication, `AuthManager` returns exactly one immutable `AuthorityContext`:

```json
{
  "schema_version": 1,
  "principal_kind": "MACHINE",
  "principal_id": "signal-scheduler",
  "authentication_method": "MACHINE_BEARER",
  "credential_id": "scheduler-2026-09",
  "roles": ["SIGNAL_SCHEDULER"]
}
```

For humans, `principal_kind` is `HUMAN`, `authentication_method` is `SESSION_SSO`, and
`credential_id` is null. Roles are unique and lower-codepoint sorted. AuthorityContext is never
accepted from request JSON. Tokens, session cookies, SSO assertions, CSRF values, and resolved
credentials are discarded after authentication and never enter the catalog, audit, canonical request,
digest, exception, or log; only non-secret principal and key IDs may be logged in bounded form.
Missing, invalid, or inconsistent role/credential/destination config fails startup before promotion
routes, registry, scheduler, or delivery workers are constructed.

Authorization is by operation and the current and target Deployment environments read inside the same
immediate transaction:

| Operation | `RELEASE_REVIEWER` | `PRODUCTION_OWNER` |
| --- | --- | --- |
| Approve `EXPERIMENTAL -> PAPER_FROZEN` | allow | deny |
| Approve `PAPER_FROZEN -> PRODUCTION_FROZEN`; retire; legacy import | deny | allow |
| Create `PAPER` Deployment | allow | allow |
| Create `PRODUCTION` Deployment | deny | allow |
| `ACTIVATE`, `REPLACE`, or `DEACTIVATE` with every non-null current/target environment `PAPER` | allow | allow |
| `ROLLBACK` with every non-null current/target environment `PAPER` | deny | allow |
| Any transition whose current or target Deployment is `PRODUCTION` | deny | allow |

An empty current or target has no environment. Thus a reviewer may activate an inactive channel to
PAPER, replace PAPER with PAPER, or deactivate PAPER only while neither actual current nor target is
PRODUCTION. Activating or rolling back to PRODUCTION, deactivating PRODUCTION, replacing either
direction across environments, otherwise displacing a current PRODUCTION Deployment, or performing an
explicit Rollback always requires `PRODUCTION_OWNER`. The owner may manage both environments.
`RESEARCHER` may only preview/create Experimental native releases; `VIEWER` may only use human read
interfaces. No role inheritance is implicit.

The registry receives AuthorityContext; routes do not interpret roles or trust caller-declared
environments. Inside the immediate transaction it first rejects requests that are intrinsically denied
by operation or requested target—for example a reviewer requesting PRODUCTION or any Rollback. It then
loads actual rows and resolves expected-current/generation mismatch before displacement authorization.
A stale request cannot commit and records `STALE_ACTIVE_SELECTION`; a still-current request must pass
the complete matrix against both actual current and target before any state validation or mutation.
Thus no unauthorized mutation is possible, while a concurrent winner cannot retroactively turn an
otherwise permitted PAPER request into `FORBIDDEN_ROLE`. Cross-environment race tests call the public
transition seam concurrently: one RELEASE_REVIEWER context targets PAPER and one PRODUCTION_OWNER
context targets PRODUCTION from the same empty expected generation. Both pass request-intrinsic
authorization; exactly one commits, and the other stores `STALE_ACTIVE_SELECTION`. This proves
exclusivity rather than testing a denied request.

`SIGNAL_SCHEDULER` may call only `invoke_signal(channel, scheduled_instant, invocation_contract_version,
invocation_id)` and `read_scheduler_binding(channel)`; the latter returns only channel, selected
Deployment identity or null, and generation. It cannot call general viewer reads, delivery, or any
release, approval, Deployment, activation, retirement, migration, or projection mutation. The separately
authenticated `SIGNAL_DELIVERY_WORKER` may claim/read delivery events and append DeliveryResult attempts
only; it cannot invoke signals or mutate governance/confirmation facts. Every post-identity governance
mutation records principal, authorizing role, action/request digest, entity/digest, actual expected
state, outcome code, bounded reason, and server time according to the idempotency rules above.

### Signal runtime and scheduler fail-closed contract

The scheduler stores only Deployment Channel identity and calls the signal-runtime interface with a
canonical scheduled instant, invocation contract version, and invocation ID. It cannot pass a release,
parameters, Study output, environment, or override Deployment. After scheduler authentication and
canonical request validation, the runtime creates or replays the Signal Invocation before active
lookup. No active selection commits terminal `NO_ACTIVE_DEPLOYMENT` on that invocation and creates no
Signal Run. A schedule mismatch commits terminal `INVALID_SCHEDULE` and creates no Signal Run. These
are the only v1 pre-run terminal outcomes. Governance policy rejections remain promotion action
outcomes and never become Signal Invocation outcomes. Malformed or unauthenticated calls and
internal/storage failures follow the transaction rules above.

Only after finding one Active Deployment does the runtime atomically bind its identity and channel
generation into a new Signal Run. Before candidate production, it reads one coherent snapshot and
verifies:

- channel asset/purpose and the bound Deployment's environment and required stage;
- Active Deployment and activation generation;
- immutable Deployment bytes/digest and channel binding;
- immutable Model Release bytes/Release Digest;
- complete exact-digest approval chain and non-retired effective stage;
- exact template/operator versions, parameters, Dataset/evidence identity, Metric Document or Legacy
  Promotion Evidence, evaluator/policy identity, and Execution Identity;
- deployed schedule, input-policy, RuntimeIdentity, SignalOutputContract, ProducerAdapterBinding,
  DeliveryContract, and DestinationAdapterBinding objects and exact digests, including trusted adapter
  registry matches;
- canonical schedule/timezone and that the invocation belongs to that schedule;
- completed-input timing, immutable operational input digest, and causal pre-action information;
- `automatic_ordering: false` at release, Deployment, and runtime layers.

The trusted producer returns a bounded candidate signal and evidence without external delivery side
effects. Before confirmation, a second immediate transaction re-verifies that channel generation,
Active Deployment, stage, release, runtime, and input identity are unchanged. It atomically marks the
Signal Run confirmed, advances Confirmed Signal State, and inserts exactly one immutable logical outbox
event protected by a unique run/event constraint. These are exactly-once platform facts. If an expected
check or production step fails, the Signal Run commits a bounded failed outcome and the prior Confirmed
Signal State remains untouched. Candidate output from a failed run is neither delivered nor shown as
confirmed. Internal/storage failure rolls back the attempted run transition and related writes.

External delivery is explicitly at least once and follows the authoritative DeliveryContract,
DeliveryRequest, and DeliveryResult schemas above. The separately authenticated delivery worker claims
only immutable events for confirmed runs, resolves the logical destination from protected runtime
config, and calls the trusted destination adapter with unchanged request bytes. A crash after provider
acceptance but before the DeliveryResult transaction commits causes a retry and may duplicate external
delivery; confirmation, the logical event, and its stable key remain unique. Tests inject that crash
and assert one event/state transition, repeated adapter calls with identical bytes/key, and no
exactly-once claim. Runtime and deployment configuration contain no broker adapter, order/fill state, or
credential material.

## Testing Decisions

### Test surface and prior art

Tests cross the highest public seam used by callers: promotion-registry behavior for governance and
one signal-runtime invocation for scheduling. They use a temporary real SQLite catalog and immutable
fixture directories. They assert returned behavior and persisted public history, not private helper
calls or SQL statement order. Direct schema tests are reserved for constraints, triggers, migrations,
and corruption cases that cannot be expressed through the interface.

Prior art includes Experiment task preview/submit/action idempotency, immutable operator publication,
Attempt canonical-success and integrity checks, Parameter Study preview/action/event state machines,
Metric Document verification, worker exception isolation, authenticated JSON/HTML parity, and the real
browser desktop/mobile/no-JavaScript harness. Reuse deterministic synthetic Dataset Snapshots and
injected clocks; no test fetches live market data or depends on wall time.

### Deterministic RED-GREEN-REFACTOR slices

1. **Canonical release identity.** RED proves exact field validation, canonical finite JSON, stable
   digest, timestamp/label exclusion, parameter/version/data/evaluator/runtime sensitivity, and
   automatic-ordering rejection. GREEN adds release contracts only. REFACTOR centralizes canonical
   identity primitives shared with existing schemas.
2. **Schema 9-to-10 upgrade and immutability.** RED upgrades a populated schema 9 catalog to exactly
   `1..10`; tests repeated/concurrent initialization, constraints, triggers, cross-environment active
   uniqueness, and old-row equality. It also updates release-deployment fixtures and proves the deploy
   helper accepts only `1..10` and rolls schema back on every mismatch. GREEN adds migration 10 and the
   required deploy helper/documentation/test changes. REFACTOR keeps version ownership explicit.
3. **Native release preview/create.** RED covers required Study champion/holdout binding, canonical
   successful Attempt, MetricDocumentFactory evidence, standalone Experiment/Attempt rejection,
   complete Study/Experiment/Attempt preview lineage, stale preview, divergence, drift, tampering,
   warnings, and duplicate actions. GREEN implements registry preview/create through existing evidence
   seams. REFACTOR removes duplicate resolution logic from callers.
4. **Legacy evidence import.** RED uses the bounded redacted BOCOM and gold fixture trees named above;
   covers source allowlists, raw/canonical hashes, AST-only producer inspection, cross-source parity,
   missing-native-lineage display, secret-like material, changed-byte conflict, and idempotent replay.
   GREEN adds the one-time adapter. REFACTOR ensures it can call only normal registry creation.
5. **Stage approvals.** RED exhaustively covers the permitted state machine, policy eligibility,
   exact-digest binding, Settings/AuthManager role-map validation, stale expected stage, action
   replay/conflict, direct skip,
   approval reuse, active-release retirement rejection, and terminal retirement. GREEN adds decisions
   and effective-stage folding. REFACTOR keeps authorization and audit inside the registry.
6. **Deployment creation.** RED proves channel digest depends only on asset/purpose and Deployment
   digest includes environment plus every exact closed-contract digest. Cover RuntimeIdentity,
   schedule/input policy, SignalOutputContract key/state/reason/value/unit validation, trusted
   ProducerAdapterBinding and DestinationAdapterBinding resolution without import paths,
   DeliveryContract logical destination/no-secret handling, wrong channel, `latest`, changed identities,
   signal-only assertion, corruption, and duplicate convergence. GREEN creates immutable channels and
   Deployments. REFACTOR hides all table writes behind the registry.
7. **Activation, replacement, deactivation, and Rollback.** RED covers request-intrinsic authorization,
   stale compare-and-swap ordering, and the actual current/target environment authority matrix. Race
   authorized reviewer/PAPER and owner/PRODUCTION calls through the public transition seam and two real
   connections from one expected generation; exactly one succeeds and the other is stale, never
   forbidden. Cover intrinsically forbidden requests, production displacement/deactivation/rollback,
   wrong expected current/channel, missing approval, retired/tampered targets, idempotency conflict
   ledger/replay, and atomic audit/projection behavior. GREEN uses immediate compare-and-swap
   transactions. REFACTOR keeps all transitions on one operation path.
8. **Authenticated API and no-JavaScript HTML.** RED covers the role matrix, CSRF/origin, strict body
   bounds, stable outcomes/errors, escaped legacy metadata, retained form errors, exact confirmation
   digest, list separation, and JSON/HTML behavior parity. GREEN adds thin adapters/templates.
   REFACTOR removes governance from presentation code.
9. **Signal Invocation and runtime preflight.** RED proves only `BOUND_TO_RUN`,
   `NO_ACTIVE_DEPLOYMENT`, and `INVALID_SCHEDULE` can be Signal Invocation outcomes; the latter two
   create no Signal Run, while malformed/unauthenticated calls create neither. Cover same caller-ID
   replay, changed-digest conflict, different-ID canonical conflict, divergent claims, immutable
   conflict replay, scheduler credential rotation/fail-closed config, and scheduler denial at every
   governance/delivery mutation seam. Parameterize all bound identity/integrity checks. Every failure
   avoids delivery and preserves Confirmed Signal State. GREEN implements the scheduler interface.
   REFACTOR keeps scheduler knowledge to channel/time/invocation only.
10. **Confirmation race and at-least-once delivery.** RED changes active selection or stage between
    candidate production and commit; injects producer interruption, database failure, duplicate
    invocation, destination-config failure, each DeliveryResult state, and a crash after provider
    acceptance but before result commit. Assert one Confirmed Signal State transition and one immutable
    logical event, identical DeliveryRequest bytes and stable key on retries, possible duplicate external
    delivery, and no persisted/logged secret. GREEN adds second verification and atomic
    confirmation/outbox creation. REFACTOR separates pure candidate production from post-commit delivery
    internally.
11. **Worker and regression integration.** RED proves signal failure cannot stop Study/Attempt workers,
    research completion cannot auto-create/approve/activate a release, existing Experiment/Attempt/
    Study behavior remains compatible, and production startup fails on invalid role, machine credential,
    destination, or schema configuration. GREEN wires composition and lifecycle. REFACTOR keeps
    independent exception seams and health reporting.
12. **Named migration acceptance.** RED/GREEN compare imported BOCOM and Au99.99 release/deployment
    summaries and shadow signal outputs with the externally guarded manifests/scripts without exposing
    credentials. REFACTOR records a bounded signed-off migration report and leaves no alternate active
    selector after cutover.

## Production Migration

1. Back up the production catalog consistently, verify restore instructions, inventory both external
   control bundles, and record their existing authoritative hashes without changing either job.
2. Deploy the additive schema and read-only registry/UI with promotion mutations disabled. Run schema
   integrity, foreign-key, old-row, health, authentication, and readback checks.
3. Enable owner-only migration mode. Preview and import the two byte-preserved Legacy Promotion
   Evidence bundles. Independently recompute hashes and compare imported summaries to the old manifests
   and active registry. Any discrepancy stops migration.
4. Have the current release reviewer and production owner review each exact imported Release Digest
   and its explicit missing-native-lineage warnings, then issue separate paper and production
   approvals at the current time. One identity may do both only when explicitly mapped to both roles.
5. Create immutable production channels and Deployments with exact existing schedules, runtime/data
   semantics, and signal-only assertions. Do not activate them yet.
6. Run the new runtime in delivery-suppressed shadow mode over deterministic fixtures and an agreed
   complete scheduled observation window. Compare parameter, input, signal, and explanation digests
   to the existing scripts; investigate every mismatch rather than accepting approximate equality.
7. At cutover, prevent duplicate delivery by pausing the old scheduler under the existing runbook,
   transactionally activate the matching platform Deployment with expected empty state, verify active
   generation/readback, invoke one idempotent production run, and verify Confirmed Signal State and
   outbox delivery. If a gate fails, deactivate the platform channel and restore the old scheduler;
   do not edit release/deployment records.
8. Repeat independently for the second model. Never batch both active transitions into an
   all-or-nothing operational step.
9. After an agreed stable period, retain old manifests, hashes, scripts, and active-registry exports as
   immutable migration evidence, but remove the old scheduler/registry from authority. The platform
   Active Deployment becomes the only scheduler selection source.
10. Verify rollback by selecting the prior immutable Deployment with expected-current semantics in a
    non-delivering rehearsal. Production Rollback remains an explicit owner action; no automated
    newest/previous fallback is enabled.

## Verification Gates

- Focused deterministic tests pass for identity, migrations, native/legacy release creation, stage
  decisions, Deployment creation, concurrent activation, runtime preflight, confirmation races,
  idempotency, authorization, audit, API, HTML, and workers.
- Existing research behavior remains green, while migration/deployment tests are intentionally updated
  for schema 10; then the complete resulting suite passes, followed by Ruff and the repository's
  available secret scan and lock/build checks.
- A current production schema-9 catalog copy upgrades to exactly `1..10` with `integrity_check` and
  `foreign_key_check` clean, all pre-migration Experiment/Attempt/Study counts and content unchanged,
  and repeated startup a no-op.
- Tampered manifest, changed parameter, changed source/evidence digest, missing approval, stale runtime,
  stale active generation, retired release, invalid schedule, and incomplete input each demonstrably
  fail closed while preserving prior Confirmed Signal State.
- The idempotency suite proves same-ID/same-digest byte replay, same-ID/different-digest conflict,
  different invocation ID for one canonical identity conflict, divergent claims, immutable deterministic
  conflict replay, and that `promotion_actions` retains only the first claim. Confirmation creates one
  logical outbox event while external delivery remains at least once.
- A real two-connection race through authorized public seams—reviewer/PAPER versus
  owner/PRODUCTION—on one asset-and-purpose channel yields exactly one winner and one stale conflict,
  with one Active Deployment and contiguous audited history.
- Desktop and 390 px browser acceptance passes with keyboard navigation, JavaScript enabled and
  disabled, authenticated sessions, visible focus, readable tables/warnings, escaped content, and all
  critical release/approval/deployment/rollback flows.
- JSON/API acceptance covers unauthenticated, wrong-role, CSRF/origin, stale preview/stage/current,
  corruption, and success paths with stable status/error contracts. Machine acceptance covers current
  and rotating scheduler keys, invalid/removed keys, scheduler-only seam access, separate
  delivery-worker authority, and zero secret persistence/logging.
- Both named legacy imports match their reviewed external manifests/hashes and shadow outputs exactly,
  contain no invented native lineage or backdated approval, and expose no secret material.
- Production cutover proves scheduler lookup uses only Active Deployment, the first confirmed signal is
  attributable to the exact Deployment/Release/input identities, the logical event is unique, adapters
  reuse its stable idempotency key where supported, external duplicates remain possible, and the old
  selector is no longer authoritative.
- Documentation links and Markdown structure pass available local checks; final diff contains only
  intended English domain/design/spec artifacts before implementation begins.

## Out of Scope

- Broker credentials, broker adapters, account connectivity, order creation, order routing, order
  submission, cancellation, fills, execution reconciliation, and broker position state.
- Automatic paper trading or live trading. `PAPER_FROZEN` authorizes a reviewed signal environment;
  it does not authorize simulated orders or fills.
- Automatic promotion from a Study champion, Experiment success, Attempt success, metric threshold,
  schedule, or semantic `latest` pointer.
- Mutable Model Releases or Deployments, in-place parameter changes, unretirement, approval transfer,
  newest-wins activation, or automatic rollback/fallback.
- Multi-model ensembles, canary percentages, traffic splitting, or more than one Active Deployment per
  Deployment Channel.
- New strategy research, parameter optimization, evaluation-policy design, model training, or changes
  to existing Experiment/Attempt/Study financial semantics.
- General arbitrary legacy import. V1 migration is constrained to the two issue-listed reviewed signal
  controls; later imports require an explicit contract extension.
- Secret migration, delivery-target administration, generic infrastructure redesign, actual production
  cutover, or changing the external cron jobs in the implementation commit. The schema-10 changes to
  `deploy/deploy-release.sh`, `deploy/README.md`, and `tests/test_deployment.py` are explicitly in scope.

## Risks and Mitigations

- **False provenance during migration:** old artifacts may not contain every native research fact.
  Preserve bytes and asserted hashes, mark unknowns explicitly, and create only current-time approvals.
- **TOCTOU between verification and confirmation:** an activation or retirement can race a run.
  Recheck generation/stage/digests in the confirmation transaction and discard stale candidates.
- **SQLite writer contention:** workers and governance actions share one database. Keep transactions
  bounded, use existing busy timeout/immediate semantics, surface transient failure, and never bypass
  compare-and-swap.
- **Database/filesystem split:** a sealed evidence file and SQLite commit are not one transaction.
  Seal before reference, compare existing bytes, treat unreferenced staging as garbage, and never
  overwrite or repair conflicting content.
- **Protected configuration error:** an allowlist alone does not prove governance or machine authority,
  and a logical destination is unusable without protected resolution. Apply the fail-closed
  Settings/AuthManager ownership, mode, role, credential, rotation, and destination validation defined
  by **Authorization and audit** before constructing any public or worker seam.
- **Duplicate or missing signal delivery:** external delivery cannot share the confirmation transaction.
  Create one immutable logical outbox event, pass its stable key where supported, expose attempt and
  receipt ambiguity, and accept/test possible duplicate external delivery after a crash.
- **Double authority during cutover:** old and new schedulers could both deliver. Shadow with delivery
  disabled, pause old scheduling before activation, verify readback, and retain an explicit restoration
  runbook until stability is accepted.
- **Overloaded production language:** users may infer trade authority. Apply the signal-only ADR in
  contracts, UI copy, permissions, tests, and runtime capability assertions.
- **Interface sprawl:** raw catalog access in routes or Cron would duplicate policy. Keep one registry
  mutation seam and one scheduler runtime seam; use temporary real catalogs rather than adding shallow
  repository interfaces.

## Further Notes

The glossary is authoritative for domain identity. ADR-0009 is authoritative for signal-only authority
and external-delivery semantics; ADR-0010 for native source eligibility and Release Stage; ADR-0011 for
cross-environment channel exclusivity. Within this plan, **Canonical runtime contracts**, **Schema and
migrations**, **Transactionality and idempotency**, **Legacy BOCOM and Au99.99 import**,
**Authorization and audit**, and **Signal runtime and scheduler fail-closed contract** are the normative
implementation sections. User stories, test slices, migration steps, verification gates, and risks
point to those rules and must not redefine them. If a summary conflicts with a normative section, the
normative section wins and the summary must be fixed.

This document defines technical behavior and verification, not product implementation priority or
cross-issue sequencing. Current sequencing belongs only in the external issue comment/Owner workflow;
it must not be inferred from section order, slice numbering, or migration numbering here.
