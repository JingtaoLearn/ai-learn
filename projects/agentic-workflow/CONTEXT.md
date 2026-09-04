# Agentic Workflow Glossary

## Agentic Workflow

A goal-directed information flow in which Agents own judgments and bounded actions, while Results change subsequent decisions.

**Distinguish from:** a workflow engine, which encodes routing decisions in a runtime.

## Agentic Workflow Assistant

The global Agent that creates and maintains the shared form of Product Agent Suites without owning any product's Goal or product decisions.

**Distinguish from:** a Product Owner Agent, which owns decisions for one product.

## Product Agent Suite

The isolated set of role-specific Agents serving one product under one Product Owner Agent.

**Distinguish from:** an Agent fleet shared across products.

## Product Owner Agent

The Agent accountable for reconciling a product's Goal, Principles, Signals, Portfolio, Evidence, and Results and selecting a capacity-bounded Action set across its Workstreams.

**Distinguish from:** Jingtao, who owns the Goal and the authorization boundaries the Agent must preserve.

## Owner Session

The one canonical persistent conversation through which a Product Owner Agent retains product context and makes product decisions.

**Distinguish from:** a State projection, which supports inspection and recovery but is not the product's decision brain.

## Specialist Agent

A role-specific Agent selected for one bounded Action because the observed Gap requires its independent capability.

**Distinguish from:** a mandatory workflow stage.

## Specialist Role Pool

The zero-to-three isolated Profile instances that can perform one concrete Specialist role for one product. The role supplies common responsibility; each Worker Slot supplies independent runtime identity and capacity.

**Distinguish from:** the singleton Product Owner and from an unbounded shared Agent fleet.

## Worker Slot

One Profile instance in a Specialist Role Pool, with its own Session state, active-process limit, workspace lease, and fencing identity.

**Distinguish from:** a thread or process started against another slot's Profile.

## Task Session

A conversation used by a Specialist Agent for one bounded task and allowed to end after its Result returns.

**Distinguish from:** the persistent Owner Session.

## Signal

New information delivered to an existing Owner Session that may change the next product decision.

**Distinguish from:** a clock, which only determines when to inspect for information.

## Evidence

A sourced fact that can support or change a product decision.

## Gap

The specific missing knowledge, judgment, or result that prevents useful progress toward the Goal.

## Action

One bounded outcome chosen by the Product Owner Agent to close or reduce a Gap.

## Portfolio

All durable Workstreams that contribute to one Product Goal and compete for shared execution capacity.

**Distinguish from:** a backlog, which stores candidate work without asserting current Goal contribution or readiness.

## Workstream

One durable outcome lane in a Portfolio. A Workstream survives multiple sequential Actions and has its own Evidence, Gap, state, health, dependencies, and wake condition.

**Distinguish from:** an Action, Issue, or Kanban card, each of which is a bounded delivery record inside or supporting a Workstream.

## Ready set

The Workstreams whose next bounded Action is dependency-complete and safe to start.

## Action set

The bounded Actions selected from the Ready set to fill currently compatible execution slots.

## Execution slot

Capacity created by an unoccupied Worker Slot, a non-overlapping Action Workspace and semantic-seam lease, an eligible Execution Host, and sufficient live resources.

## Action Workspace

The one run-scoped directory tree owned by one Action and one Worker Slot, containing its exact-SHA source, immutable inputs, outputs, logs, scratch data, and manifest.

**Distinguish from:** the canonical product evidence directory or another Action's workspace.

## Execution Host

The machine selected from live capability and availability evidence to run an Action's commands. Heavy formal verification belongs on an eligible remote host rather than the control VM.

## Transfer Manifest

The immutable inventory of non-Git files crossing hosts, including direction, relative destination, size, hash, sensitivity, and promotion state.

**Distinguish from:** bidirectional directory synchronization or credential distribution.

## Handoff

The complete task contract by which a Product Owner Agent gives one Action to one Specialist Agent.

## Matt flow

A Matt Skill selected as a professional method because it fits the observed Gap and Action.

**Distinguish from:** a fixed stage or mandatory pipeline.
