# Real-product validation

Validate the framework by using it on a real product, such as QuantResearch.
Component canaries support diagnosis; they do not replace product acceptance.
A separate framework self-demo or fixed seven-day soak is not a prerequisite.
This file defines a reusable protocol, not the outcome of a particular run.

## Acceptance journey

1. The suite Assistant configures the product's existing group and a fresh,
   current-generation Owner using supported Hermes capabilities. Reuse valid
   product code and evidence; do not restore a retired Agent runtime.
2. The Product Owner investigates current product capabilities, existing work,
   user-facing results and unresolved limitations, then produces a baseline
   report under [REPORTING.md](REPORTING.md). The Owner may delegate research or
   report preparation but remains responsible for its accuracy.
3. Publish the baseline in the product group with a discussion Thread. Discuss
   this initial baseline with the human before choosing the first real Outcome.
   This initial alignment is not a recurring approval requirement for reports.
4. The Owner independently chooses and completes one meaningful product Outcome
   within the agreed scope, doing work directly or delegating as appropriate.
   Changing product code, using Issues, opening/merging PRs and deploying are
   normal possible actions within the product's granted authority.
5. Verify the actual effect in the owning product/system. A task becoming done,
   a commit, deployment success, or plausible report is not sufficient alone.
6. The human asks a follow-up in that Outcome's Thread. Verify that the
   responsible Owner answers with the relevant product context and that any
   accepted correction informs subsequent product decisions.
7. At an approved restart boundary, verify continuity: the Owner recovers the
   accepted context, does not duplicate completed work, and continues the same
   discussion and outstanding responsibility. Record native Session behavior
   honestly; do not infer continuity from a matching display name.

## Evidence boundary

Acceptance requires all five observable results: baseline report Thread, real
Outcome, effect read-back, contextual Thread follow-up and restart continuity.
Preserve actual observations in the product runtime. The framework repository
stores this protocol and reusable lessons, not concrete run records or IDs.

Investigate native thread/session routing before promising one canonical Owner
across report Threads. If the installed release cannot provide the required
behavior, report the exact limitation, use a supported narrower route only with
an explicit explanation, or wait for a formal release. Do not build a courier,
message bus or substitute Owner to conceal the gap.

A negative research conclusion can be a valid real Outcome when the question
was meaningfully investigated and the limitations are clear. Runtime limitations
or failed checks remain open; they are never converted into a full PASS.
Optional longer observation can add confidence after this journey.
