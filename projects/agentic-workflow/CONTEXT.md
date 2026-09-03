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

The Agent accountable for interpreting a product's Goal, Signals, Evidence, and Results and choosing its next bounded Action.

**Distinguish from:** Jingtao, who owns the Goal and the authorization boundaries the Agent must preserve.

## Owner Session

The one canonical persistent conversation through which a Product Owner Agent retains product context and makes product decisions.

**Distinguish from:** a State projection, which supports inspection and recovery but is not the product's decision brain.

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

## Handoff

The complete task contract by which a Product Owner Agent gives one Action to one Specialist Agent.

## Matt flow

A Matt Skill selected as a professional method because it fits the observed Gap and Action.

**Distinguish from:** a fixed stage or mandatory pipeline.
