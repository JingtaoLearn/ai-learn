# Information Sources

The Product Owner Agent chooses which sources matter for the current decision. Information events deliver Signals into its existing canonical Owner Session. It may inspect sources with existing Hermes tools; no ingestion service is required.

## Durable shared files

- `GOAL.md` — current goal and phase boundary.
- Canonical Owner Session — product decisions, active context, and received Signals.
- `STATE.md` — compact supporting projection of current reality and the next useful question.
- `INBOX.md` — new user or automation signals not yet absorbed.
- `runs/*/RESULT.md` — outputs from prior specialist Agents.
- `runs/*/DECISION.md` — prior Owner decisions.

## Live sources available on demand

- User messages delivered to the canonical Owner Session.
- Bot-to-Bot messages from Product Agents.
- Webhook and gateway events from product information sources.
- Recent Hermes Sessions when historical context is needed.
- GitHub issues, pull requests, branches, and current diffs.
- Hermes Kanban tasks when work is deliberately scheduled there.
- Temporary Heartbeat, Loop, or Cron observations used as polling backstops.
- Production health, logs, reports, or files relevant to the current Goal.
- Web or knowledge sources when research is needed.

A busy source is not automatically important. The Owner reads a source only when it can change the next decision. The final trigger path is a real-time information event; timer output is delivered as a Signal to the same Owner Session.

## Hermes design references

- [Profiles](https://hermes-agent.nousresearch.com/docs/user-guide/profiles) — isolated Agent state and the one-Profile-per-Agent rule.
- [Bot Mode](https://hermes-agent.nousresearch.com/docs/user-guide/bot-mode) — canonical persistent Bot Chats and Bot-to-Bot messaging.
- [Sessions](https://hermes-agent.nousresearch.com/docs/user-guide/sessions) — persisted conversations and resume behavior.
- [Session Heartbeats](https://hermes-agent.nousresearch.com/docs/user-guide/features/heartbeat) — recurring prompts in the current Session.
- [Recurring Loops](https://hermes-agent.nousresearch.com/docs/user-guide/features/loops) — timer-driven work in the current Session.
- [Cron Jobs](https://hermes-agent.nousresearch.com/docs/user-guide/features/cron) — durable schedules that run in isolated Sessions.
