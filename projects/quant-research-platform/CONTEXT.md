# Quantitative Research Platform Domain

## Product Constitution

This project is Jingtao and Agents' private quantitative research and decision system. It governs evidence-first hypothesis validation, reproducible Experiments, parameter training, independent evaluation, daily evidence, and explicitly approved signal production. It never creates or executes orders.

The authoritative product purpose, role split, principles, capabilities, and stage gates are in [`docs/product-constitution.md`](docs/product-constitution.md). Domain and implementation decisions must support that constitution rather than optimize code volume, Experiment count, or headline backtest return.

## Investment Objective

The decision need, asset scope, horizon, return intent, and risk preference that motivate research. It is owned by Jingtao and is not an Experiment configuration.

## Research Question

One falsifiable question derived from an Investment Objective. It defines what evidence could change a decision.

## Research Hypothesis

A predeclared, testable claim with an observable success/failure threshold. Parameter search cannot retroactively redefine it after results are seen.

## Research Route

One economic or behavioral mechanism and its proposed strategy family for answering a Research Question. Related Studies retain route lineage so a failed route cannot be restarted under a new identity to erase prior testing.

## Spike Verdict

The evidence-first result for an unvalidated idea: `VALIDATED`, `PARTIAL`, `INVALIDATED`, or `INCONCLUSIVE`. Formal production implementation begins only after a validated or explicitly accepted partial verdict.

## Research Decision

The evidence-level conclusion after independent evaluation: `REJECTED`, `INSUFFICIENT_EVIDENCE`, or `QUALIFIED`. Qualification permits review; it never authorizes signal production.

## Parameter Training

The user-facing process of proposing and evaluating operator parameter configurations under one frozen route, data, cost, evaluation, and evidence protocol. It seeks a defensible stable region or no eligible candidate, not one fragile historical maximum. A Parameter Study is the concrete frozen plan plus append-only evolving record of this process.

## Daily Research Protocol

A versioned after-close monitoring contract that appends new evidence to a frozen research decision. It never silently retrains parameters, forks a Study, resets exposure history, or converts observed data into a pristine holdout.

## Parameter Study

A frozen parameter-search and chronological-validation protocol together with every Trial, Experiment binding, evaluation, control event, and conclusion it produces.

**Distinguish from:** model training. A Parameter Study does not produce mutable model weights.

## Study Lineage

The prior Parameter Studies whose inspected candidates, search ranges, evaluation choices, or results influenced the current Study. Lineage prevents a researcher from resetting the multiple-testing history by creating a new Study.

## Study Preview

A canonical, content-addressed view of the exact Frozen Study Plan that would be submitted now. Submission succeeds only when the expected preview digest still matches a newly resolved plan.

## Frozen Study Plan

The canonical immutable form of a Study specification after dataset, template, operator, search, evaluation, metric, validation, lineage, and execution identities have been resolved.

## Trial

One unique canonical Strategy Configuration proposed inside a Parameter Study search round. A Trial is neither a process nor a physical execution.

## Strategy Configuration

The complete normalized template parameters and seven operator parameter sets for one candidate. Its identity excludes dates, data, evaluation rules, and runtime identity.

## Experiment

The platform-wide deduplicated strategy computation for one frozen task identity. Parameter Studies reuse Experiments rather than creating a second execution model.

## Attempt

One authorized physical launch of an Experiment. A rerun creates a new Attempt under the same Experiment.

## Search Round

An internal Parameter Study phase that proposes and evaluates Trials for one chronological training window. Users manage the Study, not Search Rounds.

## Fold Window

An immutable chronological value defining readable data, available-through time, scoring interval, role, information interval, and account reset or stitching policy.

## Execution Dataset Slice

An immutable physical projection of a parent Dataset Snapshot containing only rows a run may read, plus a separate scoring interval. The UI calls this a DatasetView.

## Experiment Binding

An internal association between a Parameter Study and an Experiment with exactly one role: `INNER_SCORE`, `OUTER_AUDIT`, or `TERMINAL_HOLDOUT`.

## Evaluation Policy

A versioned trusted research rule that consumes only verified Metric Documents and returns eligibility, one validation score, independent metrics, constraints, tie-break fields, and explanation. It is not an eighth strategy operator.

## Metric Document

An immutable verified account-level evidence document derived from a canonical successful Attempt after artifact integrity and ledger/equity/cost reconciliation.

## Parameter Suggester

The only replaceable parameter-search seam. It proposes candidates from a Frozen Study Plan and ordered history but owns no Experiment, Trial, or result facts.

## Search Distribution

A frozen typed domain of permitted values for one selected operator parameter. It defines what a Parameter Suggester may propose, not a mutable parameter value.

## Suggestion Journal

The authoritative ordered record of candidate proposals and same-round inner evaluations used to deterministically reconstruct an adaptive Parameter Suggester after interruption.

**Distinguish from:** Optuna storage. Third-party sampler state is derived from this journal and is never the platform fact source.

## Adaptive Suggestion

A proposal made after observing earlier canonical inner-fold evaluations from the same Search Round. Outer-audit and terminal-holdout evidence are never adaptive inputs.

## Execution Identity

The content identity of source, dependencies, runtime, runner image, metric semantics, and protocol implementation frozen by a Study. A release path is only a locator.

## Holdout Ledger

The append-only record of holdout access (`SEALED`, `GRANTED`, `ACCESSED`) and exposure events. Access state, outcome, and freshness are separate dimensions.

## Selection Outcome

The result of final candidate selection: `NOT_DETERMINED`, `CHAMPION_SELECTED`, or `NO_ELIGIBLE_CANDIDATE`. Non-terminal Studies use `NOT_DETERMINED`.

## Holdout Outcome

The final holdout result: `NOT_RUN`, `PASSED`, or `FAILED`. A failed holdout never authorizes a runner-up.

## Holdout Freshness

A historical evidence assessment: `NO_RECORDED_PLATFORM_EXPOSURE`, `PREVIOUSLY_EXPOSED`, or `LEGACY_UNKNOWN`. It never claims global novelty.

## Study Control Status

An orthogonal control state: active, paused, cancelled, or failed. Control status does not overwrite the Study phase or fabricate selection/holdout outcomes.

## Model Release

An immutable, promotable record of one exact strategy configuration, its execution identity, and the verified evidence from which it was accepted. A native Model Release is derived from a Parameter Study's canonical successful Attempt and Metric Document; a legacy import has separate provenance.

**Distinguish from:** a Study champion, Experiment, or Attempt. A standalone Experiment or Attempt is not natively promotable, and qualifying Study evidence never becomes a Model Release automatically.

## Release Digest

The content identity of a Model Release against which every approval, deployment, and runtime verification is made.

## Release Stage

The current governance classification of a Model Release: `EXPERIMENTAL`, `PAPER_FROZEN`, `PRODUCTION_FROZEN`, or `RETIRED`. A stage expresses authorization, not research quality or deployment activity.

## Promotion Approval

An immutable decision by an identified authority to move one exact Release Digest through a permitted Release Stage transition.

**Distinguish from:** evidence eligibility. Passing research gates permits review but never supplies approval.

## Deployment Channel

The unique combination of asset and operational signal purpose. A channel is the cross-environment exclusivity scope within which at most one Deployment is active.

**Distinguish from:** a Deployment. Environment, schedule, runtime, and release identity belong to the Deployment and do not change channel identity.

## Deployment

An immutable binding of one approved Model Release to a Deployment Channel, environment, schedule, and runtime identity.

**Distinguish from:** a Model Release, which can exist without any operational binding, and an Active Deployment, which selects which binding is authoritative now.

## Active Deployment

The single authoritative Deployment selected for a Deployment Channel at a point in its audited activation history, regardless of that Deployment's environment.

## Runtime Identity

The immutable content identity of the operational source, dependencies, runner image, and protocol used for Signal Production.

**Distinguish from:** Execution Identity, which is frozen research evidence; a Deployment separately binds the operational Runtime Identity it will execute.

## Signal Output Contract

The closed vocabulary and validation rules for one Deployment's recommendations, target states, reason codes, measurement keys, numeric ranges, and units.

## Producer Adapter Binding

The exact identity of a source-reviewed trusted implementation authorized to produce a signal under one Runtime Identity and Signal Output Contract.

## Delivery Contract

The closed at-least-once delivery semantics, logical non-secret destination reference, payload contract, and trusted destination-adapter identity bound by a Deployment.

## Authority Context

An immutable server-derived statement of an authenticated human or machine principal and its explicit roles; callers cannot assert it themselves.

## Idempotency Conflict

An immutable record that a caller ID or canonical invocation identity was presented inconsistently with its first accepted claim; it grants no operational authority.

## Signal Production

Operational generation and delivery of a model-derived recommendation or target state without submitting, routing, or executing an order.

**Distinguish from:** paper trading and live trading, both of which create order or fill state and remain outside this platform.

## Signal Invocation

One canonical scheduled or requested signal action, identified independently of whether a Deployment is active. It records an idempotent pre-run outcome such as `NO_ACTIVE_DEPLOYMENT` when no Signal Run can begin.

**Distinguish from:** a Signal Run, which exists only after the invocation binds an Active Deployment.

## Signal Run

One attempt, created only after a Signal Invocation binds an Active Deployment, to verify and produce a signal under that exact runtime binding. A failed Signal Run produces no new confirmed signal.

## Confirmed Signal State

The most recent signal output whose Active Deployment and release, approval, evidence, parameter, data, and runtime identities all passed verification. Failed or interrupted Signal Runs never replace it.

## Signal Delivery

An external delivery attempt for one immutable logical outbox event created with a confirmed Signal Run. The platform creates that event once; external transport is at least once and can duplicate delivery after a crash.

## Rollback

A new audited activation that restores a previously created immutable Deployment; it does not reverse or edit history.

## Legacy Promotion Evidence

The available pre-registry manifests, digests, review records, and runtime bindings preserved as the provenance of an imported Model Release without claiming missing approval or native Study, Experiment, or Attempt lineage.
