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

## Product Group

The one private Feishu group through which authorized users communicate with one Product Owner Agent. A native exact-`chat_id` profile route connects it to the Owner Profile, and a supported session bind keeps the existing canonical Owner Session as the product brain.

**Distinguish from:** a workflow engine, task queue, evidence store, or second Owner Session.

## Specialist Agent

A role-specific Agent selected for one bounded Action because the observed Gap requires its independent capability.

**Distinguish from:** a mandatory workflow stage.

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

Capacity created by an available specialist Profile, a non-overlapping workspace or mutable-frontier lease, and sufficient host and token budget.

## Handoff

The complete task contract by which a Product Owner Agent gives one Action to one Specialist Agent.

## Matt flow

A Matt Skill selected as a professional method because it fits the observed Gap and Action.

**Distinguish from:** a fixed stage or mandatory pipeline.
