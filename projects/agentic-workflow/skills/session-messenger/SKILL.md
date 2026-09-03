---
name: session-messenger
description: Use when one Agent Session must message, question, reply to, or signal another exact Agent Session. Dispatches an addressed callback envelope through one lightweight script.
version: 1.0.0
author: Hermes
license: MIT
metadata:
  hermes:
    tags: [agents, sessions, messaging, signals, callbacks]
    related_skills: [hermes-agent]
---

# Session Messenger

Use one transport for Agent questions, replies, results, reviews, approvals, and external signals. It targets an existing Hermes Profile and exact Session ID; it never finds a Session by title or creates a replacement.

## Send from an Agent Session

Run `scripts/send.py` with both endpoints and a short message:

```bash
python3 <skill>/scripts/send.py \
  --from-profile <my-profile> \
  --from-session <my-session> \
  --to-profile <their-profile> \
  --to-session <their-session> \
  --kind QUESTION \
  --workdir <product-workdir> \
  --message 'Question or artifact reference'
```

Use the exact current Session ID shown in the system prompt. Agent launchers must use Hermes `--pass-session-id`. When handling an inbound envelope, its `to_profile` and `to_session` are your current reply identity, so no discovery is needed.

The command dispatches in the background and returns immediately with JSON containing `message_id` and `result_file`. The target Agent receives a `[SESSION-MESSAGE v1]` envelope containing both endpoints.

## Reply

If `reply_available: true` and a reply is useful:

1. Swap the inbound `from_*` and `to_*` values.
2. Set `--correlation-id` to the inbound `message_id`.
3. Choose a truthful `--kind`, usually `REPLY`, `QUESTION`, `RESULT`, `BLOCKED`, or `DECISION`.
4. Dispatch once, then finish the current turn so the sender Session can be resumed safely.

Do not wait or poll after dispatching. The target Session will be triggered independently.

## Send a Non-Session Signal

Timers, GitHub/CI adapters, production monitors, and data checks use the same script without a callback address:

```bash
python3 <skill>/scripts/send.py \
  --source github \
  --to-profile <owner-profile> \
  --to-session <owner-session> \
  --kind SIGNAL \
  --workdir <product-workdir> \
  --message-file <signal-file>
```

A signal carries `reply_available: false`. If a source itself owns a Hermes Session and should receive follow-up questions, use `--from-profile` and `--from-session` instead of `--source`.

## Message Discipline

- Put large evidence in an artifact; send its path and SHA-256 rather than copying it into the message.
- Treat `kind` as routing context, not a workflow state machine. New kinds need no code change.
- Preserve authorization boundaries. Delivery does not authorize the receiver to merge, deploy, publish, pay, trade, or alter production.
- The receiver applies its own role and verifies referenced evidence before acting.
- A successful dispatch means the detached delivery process started. The private `result_file` records whether Hermes resumed the exact requested Session; it expires after 24 hours.

## Failure Handling

Read `result_file` only when a delivery is suspected to have failed. A false `ok`, a nonzero `exit_code`, or a mismatched `observed_session` means the message was not accepted as delivered. Report the failure to the current Owner; do not silently create another Session.

This scaffold has no daemon, broker, database, retry loop, dead-letter queue, or test suite. Add infrastructure only after observed failures demonstrate the need.
