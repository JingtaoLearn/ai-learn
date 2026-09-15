# Native Owner return and user delivery

This is reusable guidance, not a live routing registry. Store concrete Profile,
Session, chat, user and message IDs in the product runtime only.

## Two destinations, one accountable Owner

Automatic task events enter the existing canonical top-level product Owner.
User discussions stay in their actual Outcome Threads. A separate Thread can
have its own native Session under the same Owner Profile; it is not permission
to create an unrelated Owner or resurrect old product decisions.

For a Feishu release whose no-anchor thread-create path fails validation, use
native top-level Owner event routing and the existing official Lark reply API
for Thread reports. Do not pass a thread ID as message.create receive_id_type,
patch Hermes core, add a courier, or claim the upstream defect is fixed.

## Before runnable work

For every durable task, including implementation dependencies and review or
correction tasks:

1. Verify the exact Role/Profile and the valid isolated workspace.
2. Configure a native wake-only Owner return subscription using the product's
   canonical chat/principal/Profile, without a Thread ID for the affected
   Feishu configuration.
3. Read back the subscription, then make the task runnable. With the CLI,
   initially blocked creation followed by subscribe and unblock provides this
   ordering. Use native dependency links where appropriate.

A subscription on a research parent does not cover its implementation child's
approval event. Do not broadly replay historical completed tasks.

## Reports and human decisions

Follow REPORTING.md. An automatic Owner turn that needs a Thread report calls
the official message.reply operation against a verified real report/root
message ID, reads back the returned message, and then returns NO_REPLY so the
gateway does not send a duplicate. Human-triggered replies may retain the
normal working reply path.

For a genuine human decision, publish a concise product-group alert with the
intended human mentioned and a link to the Outcome discussion. Record sent IDs
only after actual read-back; deduplicate by decision/report revision and check
supersession against the latest Goal and user instructions.

## Idle recovery

The Product Owner, not the Assistant, holds one native idle Heartbeat when the
product needs autonomous continuation. Check current tasks and owned gaps,
without interrupting active turns or duplicating work. Do not substitute a
fresh reasoning Cron for the canonical Owner or add Assistant patrols.

Verify a real timed turn in the intended Owner, a successful user delivery,
and stable scope. Replace temporary test-speed schedules with the intended
steady cadence. A fire counter is not proof of successful delivery.
