# Owner reports and product discussion

Read this contract when a human asks what a product has done, when an Outcome
reaches a milestone, or when continuing discussion of a shared report.

## Responsibility and evidence

The Product Owner owns the report and subsequent discussion. It may prepare the
report directly or delegate when that improves the result. The suite Assistant
maintains the framework; it does not become the reporting Owner for products.

Use existing Agent conversations, native tasks, product code, Issues/PRs and
actual artifacts as needed. Retrieve facts rather than asking the human to
reconstruct them. No new logging system, reporting daemon, mandatory periodic
scanner or dedicated reporting Agent is required.

## Write for the user

Explain in ordinary product language:

- What work or approaches were actually attempted, and which remain unfinished.
- What results were observed, including failed or inconclusive approaches.
- What can be concluded, what remains uncertain, and what matters next.

Lead with the answer. Include reasons, comparisons, evidence links or a useful
visual only when they help understanding. Keep Tool Calls, execution IDs,
internal state labels and raw logs out of the main narrative. Do not substitute
activity volume for results or imply that all attempted approaches succeeded.

A report is an explanation of work, not a separate runtime record format.
Generate it on demand from current evidence; do not fabricate missing history.

## One Outcome, one discussion

Use the dedicated product group. Each coherent Outcome has one report/root
message and one discussion Thread. Publish revisions and answer follow-ups in
that same Thread; do not create a new group or root for every update.
For an already reported Outcome, locate the existing root before publishing.
A genuinely different Outcome gets its own root. No cross-group request router
is needed.

A shareable report link plus a concise group summary is appropriate for longer
reports. Keep report access within the intended audience and verify the posted
message and link. Store concrete URLs/root IDs only in product runtime context.

Native Thread routing and Owner continuity must be verified in the deployed
Hermes version. A Thread must not silently become an uncoordinated new product
Owner. Carry accepted product decisions into the responsible Owner's context
using supported native mechanisms, without assuming every reply is approval.

## Automatic event delivery and decisions

For an automatic Kanban, Heartbeat or delayed-result turn, use the supported routing in ROUTING.md. If a report is warranted, send it explicitly with the official Lark CLI to the verified report/root message using `+messages-reply --reply-in-thread`; read back the actual returned message ID. Return `NO_REPLY` after a successful explicit send so the gateway does not publish a duplicate. A final answer stored in a Session is not evidence of delivery.

A genuine human decision needs a short top-level alert in the product group, mentioning the intended human and linking the Outcome discussion. State the decision, recommendation, impact and what can continue meanwhile. Keep ordinary engineering repairs within existing authority; do not manufacture another approval just because an artifact hash changed. Actual tool safety gates still apply and must not be bypassed.

Use an idempotency key for each decision or report revision. Store the sent message ID and disposition in live State only after successful send and read-back. Before reissuing a pending request, check whether a newer user instruction or Goal change superseded it. Do not revive obsolete data-inquiry requests from a late scout callback.

If sending fails, retain an explicit delivery failure and use an available supported product-group send path; do not mark the human notified or quietly wait for approval that was never delivered. No separate report daemon or Assistant patrol is needed.

## Inform without blocking

Proactively notify only for result milestones, major direction changes,
material risk/blockers or decisions that genuinely need the human. Ordinary
execution and routine engineering decisions proceed within granted authority;
report delivery does not create an acknowledgement gate.

For the first real-product validation, discuss the baseline before the first
Outcome. After initial alignment, an informational question is not a pause,
while an explicit stop or direction change takes precedence. Product-level
Goals, principles and authority remain human-owned.
