# Quantitative Research Platform Domain

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
