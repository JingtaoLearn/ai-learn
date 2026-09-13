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

## Inform without blocking

Proactively notify only for result milestones, major direction changes,
material risk/blockers or decisions that genuinely need the human. Ordinary
execution and routine engineering decisions proceed within granted authority;
report delivery does not create an acknowledgement gate.

For the first real-product validation, discuss the baseline before the first
Outcome. After initial alignment, an informational question is not a pause,
while an explicit stop or direction change takes precedence. Product-level
Goals, principles and authority remain human-owned.
