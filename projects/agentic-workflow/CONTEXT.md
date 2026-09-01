# Agentic Workflow Domain

## Workflow Project

A long-running product or system effort governed by one current intent and advanced through evidence-backed actions.

**Distinguish from:** a repository, which is one possible execution target; and a Session, which is one temporary reasoning context.

## Session

One bounded model conversation used to reason about or execute part of a Workflow Project.

**Distinguish from:** a Pulse, Action, Attempt, or Run, each of which has a durable workflow identity independent of the conversation.

## Worker

A process that accepts one Handoff Package and returns evidence for one Attempt.

## Constitution Revision

An immutable version of the governance rules that define user sovereignty, evidence standards, authorization boundaries, and durable non-goals.

**Distinguish from:** a Goal Revision, which states the desired outcome; and an Operating Profile Revision, which states changeable execution preferences.

## Goal Revision

An immutable, explicitly user-authorized version of the desired outcome, scope, success evidence, constraints, accepted trade-offs, and non-goals.

## Operating Profile Revision

An immutable version of currently accepted autonomy, synchronization, method, routing, budget, and execution-venue preferences.

## Active Intent

The current atomic combination of one Constitution Revision, one Goal Revision, and one Operating Profile Revision.

## Intent Binding

The association between an authoritative workflow artifact and the Active Intent that authorizes it.

## Signal Event

An observed, potentially untrusted fact offered to a Workflow Project.

**Distinguish from:** a User Decision, which carries verified decision authority.

## User Decision

An authenticated instruction that may revise intent within its declared scope.

## Approval Decision

A bounded User Decision that authorizes one exact external effect.

## Portfolio

The current evidence-based map of desired outcomes, open Gaps, candidate work, dependencies, active work, and fog.

**Distinguish from:** an issue backlog, which is one projection of work rather than the product goal.

## Gap

A material difference between the current evidence-backed state and the outcome required by the Active Intent.

## Pulse

One bounded reconciliation attempt that records one next Action or a Legal Stop.

**Distinguish from:** a Session, which may execute a Pulse but is not its durable identity.

## Action

One bounded unit of intended work that reduces a Gap or obtains evidence that changes how the Gap should be understood.

## Action Envelope

The immutable contract that authorizes one Action and defines its method, route, ownership, limits, acceptance, and stop conditions.

## Capability Snapshot

An immutable observation of what one execution venue can control, prove, and limit at a particular time.

## Execution Venue

An environment capable of accepting a Handoff Package and producing a Run or evidence.

## Capability Matrix

The accepted projection of eligible Capability Snapshots used to compare and select execution candidates.

## Route Plan

A proposed route selected from the Capability Matrix before authority is frozen.

## Route Envelope

The immutable, authorized form of an accepted Route Plan inside an Action Envelope.

## Exact Route

A route that permits no substitution from its explicitly required execution characteristics.

## Capability-Class Route

A route that may select from an accepted set of equivalent-enough candidates.

## Matt Method

A standardized planning, design, implementation, testing, or review discipline selected from the Matt Skills body of practice.

## Matt Invocation

The authorized application of one Matt Method to one cognitive Action.

## Matt Receipt

Evidence that a Matt Invocation followed its required method and produced a classified result.

## Handoff Package

The immutable transfer contract by which one execution venue offers an Action to another.

## Attempt

One intentional try to execute an Action. A retry creates a new Attempt; duplicate delivery does not.

## Run

One physical execution belonging to an Attempt and one executor.

## Route Receipt

Evidence of the actual route used by a Run.

## Evidence

An immutable, provenance-bearing fact that can support or contradict a goal, action, review, or outcome claim.

## Test Evidence

Evidence produced by executing a declared test profile against one exact source and environment identity.

## Review Receipt

An independent verdict about whether an Action and its evidence satisfy the required specification and standards.

## Outcome Evidence

Evidence about whether a delivered capability or decision actually advances the user-owned outcome.

**Distinguish from:** delivery evidence, which proves that a technical change occurred.

## Operation Record

The durable lifecycle of one intended external effect.

## Outbox Event

A durable intent to deliver one immutable notification without repeating the underlying Action.

## Daily Brief

The single human-facing daily projection of material goal, evidence, frontier, stop, and decision changes.

## Replay

A side-effect-free evaluation that feeds recorded historical inputs through the current workflow and compares its decisions with later evidence.

## Shadow

A live evaluation mode that observes current Signal Events and produces proposed decisions without authorizing external effects.

## Legal Stop

A truthful non-action result such as `NO_ACTION`, `WAITING_HUMAN`, `BLOCKED_EXTERNAL`, `INVALIDATED`, `SUPERSEDED`, or `BUDGET_EXHAUSTED`.
