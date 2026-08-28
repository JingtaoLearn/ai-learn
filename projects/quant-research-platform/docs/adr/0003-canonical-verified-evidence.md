# ADR-0003: Accept only canonical verified Experiment evidence

- Status: Accepted
- Date: 2026-08-28

## Context

An existing Experiment or duplicate submission proves identity reuse, not that a trustworthy result exists. Multiple successful Attempts may also disagree.

## Decision

A Parameter Study consumes an Experiment only through its canonical successful Attempt after artifact and Metric Document verification. Any divergent successful Attempt makes the Experiment `CONTESTED` and unusable as validated evidence.

## Consequences

Duplicate Experiment submission may result in waiting, retry policy, reuse, or fail-closed handling. It never automatically means a Trial succeeded.

## Alternatives considered

Using the first Attempt returned by duplicate submission or selecting the newest successful Attempt was rejected because both can hide divergence and mutable result choice.
