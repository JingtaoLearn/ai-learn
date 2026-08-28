# ADR-0001: Reuse Experiment and Attempt for Parameter Study execution

- Status: Accepted
- Date: 2026-08-28

## Context

A Parameter Study needs durable coordination, pause/resume, and crash recovery. Building a second execution lifecycle for Trials would duplicate the existing globally deduplicated Experiment and single-launch Attempt semantics.

## Decision

`ParameterStudy` is the only primary Study module. It submits and observes the existing `ExperimentService`; Trials bind to Experiments with an explicit role. Pause or cancellation prevents new Study effects but does not revoke an Attempt already submitted to the global worker. Study coordination records effect intent before execution and uses deterministic action IDs. A private coordinator entry may discover the next Study; Web, CLI, and the outer worker never query Study tables.

## Consequences

Experiments remain reusable across Studies and cancellation cannot delete shared evidence. The Study module must reconcile in-flight Attempts instead of owning their process lifecycle.

## Alternatives considered

A second Trial execution state machine was rejected because it would split deduplication and recovery truth. Letting Web or worker query Study tables was rejected because it would make `ParameterStudy` a shallow middle man.
