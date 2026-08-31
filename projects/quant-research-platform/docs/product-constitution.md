# Quant Research Decision System Product Constitution

- **Status:** Accepted
- **Decision date:** 2026-08-31
- **Product decision authority:** Jingtao
- **Product and engineering owner:** Hermes

## Purpose

This project exists to turn Jingtao's investment objectives and market hypotheses into falsifiable, reproducible, comparable, and continuously monitored research decisions.

It is not primarily a backtest website. It is a private system shared by Jingtao and Agents for:

1. formalizing an investment idea;
2. proving or rejecting the riskiest assumption cheaply;
3. running governed Experiments and parameter-training Studies;
4. evaluating evidence without rewarding data mining;
5. presenting a decision that a human can understand;
6. optionally promoting an approved immutable result into monitored signal production.

The platform must improve decision quality. It does not promise profitable strategies.

## Why the System Exists

The system addresses failures that cannot be solved reliably by chat messages, one-off scripts, or isolated reports:

- **Evidence fragmentation:** strategy logic, data, parameters, code, reports, and daily jobs otherwise live in unrelated sessions and files.
- **Research drift:** a result can silently change when `latest` operators, data revisions, dependencies, or runtime code change.
- **Multiple-testing bias:** repeated parameter search can manufacture an apparent winner without genuine out-of-sample edge.
- **Incorrect economics:** missing dividends, tax, costs, market availability, or matched-exposure controls can make a correct computation answer the wrong investment question.
- **Research-to-signal divergence:** a daily script can run different data, parameters, or semantics from the result that was reviewed.
- **Human-Agent asymmetry:** Agents need stable APIs and identities, while Jingtao needs a decision-first UI and reports rather than raw logs.
- **Lost learning:** rejected routes and exposed evidence must remain visible so the same failed idea is not rediscovered under a new name.

## North Star

Given an Investment Objective or Research Hypothesis, the system carries its evidence lifecycle through:

```text
Investment Objective
-> Research Question
-> Research Hypothesis / Route
-> Evidence-first Spike
-> Experiment
-> Parameter Study
-> Independent Evaluation
-> REJECTED / INSUFFICIENT_EVIDENCE / QUALIFIED
-> Delivery-suppressed Signal Run
-> Explicit Approval
-> Confirmed Signal State and Continuous Monitoring
```

The same domain contracts serve both interfaces:

- Jingtao uses the authenticated, decision-first web interface;
- Agents use stable APIs, files, and immutable identities.

The system produces research conclusions and signals only. It never submits, routes, or executes an order.

## Core Product Capabilities

### 1. Research intent and route memory

The system must preserve the objective, question, hypothesis, route, predeclared success/failure threshold, prior related research, and final verdict.

This layer answers *why the work exists*. An Experiment answers only *what was computed*.

A rejected or invalidated route is a successful research result. It remains discoverable and prevents repeated data mining.

### 2. Evidence-first validation

An unvalidated idea starts with the cheapest valid Spike using existing code and data. Its pass/fail threshold is stated before observation.

The verdict is one of:

- `VALIDATED`: the risky assumption passed and permits the smallest useful implementation;
- `PARTIAL`: the assumption passed only under recorded constraints that the owner explicitly accepts;
- `INVALIDATED`: the idea stops before production implementation;
- `INCONCLUSIVE`: missing or contradictory evidence prevents a claim.

Formal code and broad test scaffolding do not precede this proof gate.

### 3. Trusted data and economic accounting

Data is a first-class product capability, not an attachment. The system must:

- backfill the requested historical range on first use;
- refresh after expected market sessions;
- detect bounded source revisions even when no date is missing;
- preserve raw source evidence, calendar identity, normalized data, and revision lineage;
- bind corporate actions, dividend tax, costs, tradability assumptions, and information availability;
- fail closed on gaps, conflicts, stale evidence, or unsupported semantics;
- reconcile cash, positions, receivables, costs, tax, and final equity mechanically.

Adjusted data may support causal signal calculation but never invent executable cash flow.

### 4. Governed Experiments and Attempts

A resolved immutable task identity owns one Experiment. It binds exact data, date range, template, operator versions and digests, parameters, source, dependencies, runtime, and evaluation semantics.

- An exact duplicate returns the existing Experiment.
- A physical rerun creates a new Attempt under that Experiment.
- A rerun cannot change selectors, source, parameters, or `latest` resolution.
- Each Attempt launches at most once and preserves its logs, artifacts, outcome, and integrity evidence.
- Divergent successful reruns are visible evidence, never silently collapsed.

### 5. Parameter training

Parameter training is a core product function. It searches selected operator parameters within a frozen strategy structure and evidence protocol.

A Parameter Study must:

- evaluate the default parameters first;
- support deterministic Grid/Random and feedback-driven ask/tell suggestion;
- learn proposals only from canonical inner-training evidence;
- keep outer audit and terminal holdout evidence outside adaptive feedback;
- deduplicate repeated parameter configurations;
- freeze search distributions, budget, data views, costs, evaluator, source, and runtime;
- separate qualification from ranking;
- prefer a stable parameter region over one fragile maximum;
- allow `NO_ELIGIBLE_CANDIDATE` and failed holdout as valid outcomes;
- never change accounting, cost, report, or evidence semantics to improve the score.

Parameter training refines a route that passed its initial proof gate. It must not be used to rescue an invalidated hypothesis by widening the search.

### 6. Independent evaluation and decision

Evaluation consumes only verified immutable evidence. It must distinguish:

- absolute performance from matched-exposure edge;
- natural exits from forced terminal liquidation;
- parameter-search evidence from outer audit and terminal holdout;
- a policy constraint pass from deployment qualification;
- previously exposed evidence from genuinely unexposed evidence;
- one observed best configuration from statistical or economic confidence.

The product decision can be `REJECTED`, `INSUFFICIENT_EVIDENCE`, or `QUALIFIED`. The platform never fabricates a champion when no candidate qualifies.

### 7. Decision-first human and Agent experience

The primary view answers:

1. What changed?
2. What is today's conclusion or state?
3. Is there evidence of edge?
4. What are the important caveats?
5. What is the next legitimate action?
6. Which immutable evidence supports the answer?

Price path, trades, holding intervals, costs, tax, total return, controls, drawdown, stability, significance, and evidence freshness remain available without forcing the user to inspect raw logs.

Desktop, approximately 390 px mobile, keyboard, JavaScript, no-JavaScript, and authenticated API paths are first-class interfaces to the same domain behavior.

### 8. Daily evidence and controlled signals

A versioned Daily Research Protocol appends monitoring evidence after each expected close. It never silently retrains, forks a Study, resets exposure history, or relabels observed data as a pristine holdout.

Daily research outcomes are bounded and explicit, such as:

- `NO_CHANGE`;
- `NEW_EVIDENCE`;
- `REJECTED_NO_EDGE`;
- `FAILED`.

Research evidence never auto-promotes. Signal production requires an immutable Model Release, explicit approval, an immutable Deployment, shadow parity, exact runtime verification, and an audited activation.

A failed run never replaces the last Confirmed Signal State. Existing external BOCOM and Au99.99 jobs remain authoritative until their separate cutover gates pass.

## Role Contract

### Jingtao owns

- investment objectives and risk preference;
- changes to the product's principles, process, final goal, phases, or automation boundary;
- approval to replace an authoritative production signal;
- any future decision to add simulated or automatic trade execution.

### Hermes owns

- product discovery and evidence gathering;
- research formalization and Spike design;
- domain modeling, architecture, implementation, and focused tests;
- Agent orchestration, review, CI, releases, deployment, operations, and recovery;
- issue decomposition and sequencing;
- truthful status and escalation of only genuine product/risk decisions.

Ordinary engineering choices and retrievable facts are not sent to Jingtao for resolution.

## Development Principles

1. **Prove value before formal implementation.** Test the riskiest assumption with the cheapest valid evidence.
2. **Implement the smallest useful vertical slice.** Do not generalize before one real path works.
3. **Test the chosen behavior after implementation.** Focus on finance correctness, causality, immutable identity, idempotency, and recovery; do not build broad scaffolding for discarded ideas.
4. **Truth outranks attractive returns.** `NO_EDGE`, `INVALIDATED`, and failed holdout are useful outcomes.
5. **Reuse before self-building.** Adopt mature components only after same-data, same-strategy correctness and resource admission.
6. **One authority for each fact.** Platform records and sealed artifacts own experiment facts; external tools are projections or adapters.
7. **Never rewrite evidence.** Corrections create new evidence and read-time classification.
8. **Separate research, signal, and execution.** This system stops before order creation.
9. **Ship through independent gates.** No self-reported Agent completion substitutes for exact-diff review, GitHub state, or target-environment verification.

## Delivery Stages

### Stage 0 — Reproducible foundation: shipped

The platform has immutable Dataset Snapshots, versioned operators, one causal template, Experiment/Attempt deduplication, isolated custom execution, Parameter Studies, adaptive suggestion, authenticated UI, sealed reports, and fail-closed application/UI release deployment.

This foundation is necessary but does not prove that investment conclusions are correct.

### Stage 1 — Trustworthy evidence: current

Complete finance-correct data, corporate actions, dividend tax, costs, execution availability, source revisions, matched-exposure controls, and historical evidence classification.

**Gate:** a production-shaped BOCOM replay reconciles mechanically and cannot be labeled qualified without total-return and matched-exposure evidence.

### Stage 2 — Research and parameter-training decision loop

Add explicit Research Question/Hypothesis/Route memory above existing Experiment and Study capabilities. Complete the shared journey from a predeclared Spike through Experiment, parameter training, independent evaluation, and a decision-first verified report.

**Gate:** one BOCOM route can be accepted or rejected end to end without manual database repair, hidden parameter changes, or misleading evidence claims.

### Stage 3 — Daily evidence loop

Run one frozen after-close Daily Research Protocol, update audited data and revisions, reuse immutable identities, publish material decisions, and stay quiet on no-change outcomes.

**Gate:** every expected session has one idempotent terminal outcome with complete evidence, and the observation window has zero silent misses.

### Stage 4 — Controlled signal loop

Implement immutable promotion/deployment governance, import existing external evidence honestly, run delivery-suppressed shadow parity, require explicit approval, and rehearse rollback.

**Gate:** BOCOM and Au99.99 pass separate parity, cutover, observation, and rollback decisions before either external authority is retired.

### Stage 5 — Research expansion

Admit new strategy routes, templates, operators, assets, engine adapters, and advanced diagnostics only through evidence-first admission. Prioritize portfolio construction and asset-allocation questions over further single-stock timing search when the latter lacks edge.

**Gate:** each expansion demonstrates measurable decision value without weakening the evidence contract or Feng resource envelope.

### Stage 6 — Accumulated personal investment research system

Use cross-Study route memory to preserve what worked, what failed, under which market and cost conditions, and why. Support portfolio-level comparisons and ongoing belief revision without pretending that historical evidence guarantees future returns.

**Gate:** a new research question can reuse prior evidence and avoid repeating an already invalidated route while keeping every conclusion traceable to immutable facts.

## Current Priority

The current project is in Stage 1 even though parts of Stage 2 are already implemented. Trust correctness gates product completion.

The immediate proving path is:

1. freeze authoritative BOCOM corporate-action and tax evidence;
2. run an evidence-first old/new total-return Spike;
3. extract the smallest finance-correct implementation only if validated;
4. add matched-exposure qualification and classify existing evidence truthfully;
5. complete the daily data/report journey;
6. prove unattended operation;
7. migrate BOCOM and Au99.99 signals separately;
8. resume broader parameter training and new strategy-route research.

## Explicit Non-goals

- broker credentials;
- order submission, routing, fills, or automatic trading;
- maximizing the number of Experiments, parameters, features, or closed issues;
- a public multi-tenant SaaS platform;
- Kubernetes, a data lake, GPU/high-concurrency training, or a large replacement frontend without demonstrated need;
- using parameter training to make an invalid strategy appear successful;
- rewriting historical evidence to match newer semantics.

## Authoritative References

- Domain glossary: [`CONTEXT.md`](../CONTEXT.md)
- Project operation rules: [`AGENTS.md`](../AGENTS.md)
- Product roadmap: [GitHub Issue #195](https://github.com/JingtaoLearn/ai-learn/issues/195)
- Promotion and signal-governance design: [`docs/plans/2026-08-31-model-promotion-deployment-registry.md`](plans/2026-08-31-model-promotion-deployment-registry.md)
