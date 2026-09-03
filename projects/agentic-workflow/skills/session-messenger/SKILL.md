---
name: session-messenger
description: Use when one exact Hermes Agent Session must receive a question, reply, result, review, decision, or Signal. Routes lightweight callbacks while leaving durable formal work to Kanban.
version: 1.2.0
author: Hermes
license: MIT
metadata:
  hermes:
    tags: [agents, sessions, messaging, signals, callbacks]
    related_skills: [hermes-agent, kanban-worker]
---

# Session Messenger

Use one small addressed envelope for lightweight Agent conversation and Signals. Target an existing Hermes Profile and exact Session ID; never find a Session by title or create a replacement.

This Skill fills one local gap: in the current headless Profile layout, native `message_agent` successfully runs the target Bot but its background completion does not wake the sender after the sender's one-shot CLI process exits. The bundled script detaches delivery and carries an explicit callback Session.

## Choose the native plane first

- **Short canonical Bot Chat exchange with a live owning process:** use native `message_agent`.
- **Exact Session callback when the sender process will exit:** use this Skill's script.
- **Formal work with acceptance, artifacts, blocking, retry, review, or crash recovery:** use Kanban; its task ID is the correlation ID and `notify+wake` returns terminal events to the subscribed Owner.
- **Scheduled Signal:** prefer Cron `deliver=bot-chat:<profile>`.
- **Immediate local Signal without a Session:** use this script with `--source`.
- **Cross-machine Hermes:** use `hermes peer`; **cross-framework:** use A2A.

The script is a narrow migration adapter, not a new workflow runtime.

A task created from a plain CLI-owned Owner Session may have no native Kanban wake subscription. When that task reaches `done`, `blocked`, or terminal review, a supervising observer sends one idempotent RESULT/REVIEW envelope to the exact Owner Session. Skip that fallback only when the board shows a verified native subscription already delivered the terminal event.

## Replyable Session message

```bash
python3 <skill>/scripts/send.py \
  --from-profile <my-profile> \
  --from-session <my-session> \
  --to-profile <their-profile> \
  --to-session <their-session> \
  --kind QUESTION \
  --correlation-id <task-or-goal-id> \
  --workdir <product-workdir> \
  --message 'Question or artifact reference'
```

Use the exact current Session ID shown in the system prompt. Launch newly created Agent Sessions with Hermes `--pass-session-id`. An inbound envelope's `to_*` values are your current callback identity, so replies need no discovery.

The command returns immediately with a dispatch ACK containing `message_id` and a private `result_file`. ACK means only that delivery started; the result file later records whether Hermes resumed the requested Session.

## Reply

When a response is useful and `reply_available: true`:

1. Swap inbound `from_*` and `to_*`.
2. Preserve inbound `correlation_id`.
3. Set `--causation-id` to inbound `message_id`.
4. Set `--hop` to inbound `reply_hop`.
5. Dispatch once, then finish the current turn so the sender Session can be resumed safely.

Do not send courtesy acknowledgments. Reply only with new information, a decision, a blocking question, or a requested terminal result. The script refuses a send when `hop >= max_hops`; the default cap is six.

## Signal

Timers, GitHub/CI adapters, production monitors, and data checks reuse the envelope without inventing separate notification code:

```bash
python3 <skill>/scripts/send.py \
  --source github \
  --to-profile <owner-profile> \
  --to-session <owner-session> \
  --kind SIGNAL \
  --correlation-id <event-or-goal-id> \
  --idempotency-key <stable-key> \
  --workdir <product-workdir> \
  --message-file <signal-file>
```

A non-Session Signal has `reply_available: false`. If its collector owns a Hermes Session and should receive questions, use a replyable Session message instead.

## Envelope

Every message carries:

- `schema: agent-message/v1`;
- unique `message_id`;
- stable `correlation_id` for the task, goal, or event;
- optional `causation_id` for the immediately preceding message;
- optional stable `idempotency_key` for retry-sensitive work;
- open `type` (`QUESTION`, `REPLY`, `RESULT`, `BLOCKED`, `REVIEW`, `DECISION`, `SIGNAL`, or another truthful value);
- exact target Profile and Session;
- exact sender Profile and Session, or a non-replyable source label;
- optional repeatable `--artifact` references;
- `requires_decision`;
- RFC3339 creation time;
- bounded hop count;
- short body.

These fields are transport-neutral and can later map to Kanban metadata, peer Runs, A2A, or CloudEvents without changing business meaning.

## Reliability and safety

- Put large evidence in artifacts; send paths, URLs, and hashes.
- Stable `idempotency_key` is required before a retried message can cause an external side effect. The receiver owns idempotency; this transport does not claim exactly-once delivery.
- Delivery and permission are separate. The receiver keeps its own merge, deployment, publication, payment, trading, and production gates.
- The target runs in its own Profile environment; sender credentials are not forwarded.
- Never edit `state.db` directly or inject concurrent writers. Delivery uses Hermes `--resume`, preserving Session turn-lease behavior.
- Inspect `result_file` only when delivery is suspected to have failed. A false `ok`, nonzero exit, or mismatched `observed_session` means delivery is not verified.

This scaffold has no daemon, broker, database, retry loop, dead-letter queue, or test suite. Formal durable work belongs in Kanban; this Skill remains lightweight conversation and Signal glue.
