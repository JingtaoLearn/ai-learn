# Product Owner terminal-event adapter

This is the smallest edge adapter required after native `message_agent` has completed a specialist turn. It is not a workflow runtime, scheduler, or database.

## Contract

A terminal Agent writes its immutable result artifact first, then emits one of:

- `RESULT_READY`
- `BLOCKED`
- `FAILED`

The adapter:

1. validates the terminal event and hashes the artifact;
2. computes a deterministic `event_id` from product, source, Owner, run/action, artifact, and summary fields;
3. persists `event.json` and the exact Owner prompt before delivery;
4. invokes the configured Owner profile through exact `--resume <owner_session_id>` targeting;
5. requires the authoritative CLI Session ID written on stderr to equal the expected persistent Owner Session;
6. records every delivery attempt and response;
7. writes `delivered.json` only after a successful exact-Session trigger;
8. deduplicates later emissions of the same terminal event.

The subprocess environment is rebuilt from a small operational whitelist, removes inherited specialist state and credentials, and restores the real OS-user `HOME` before selecting the Owner profile. Without that boundary, a nested `hermes -p <owner>` can resolve credentials inside the source Agent's profile and fail before delivery. Event initialization uses an atomic directory rename, durable writes fsync files and parent directories, and event content is private by default (`0700` directories and `0600` files).

## CLI

```bash
python notification/owner_event.py \
  --state-root /path/to/product/notifications/events \
  --product-workspace /path/to/product \
  --product-id example-product \
  --event-type RESULT_READY \
  --source-profile researchagentexample \
  --owner-profile productowneragentexample \
  --owner-session-id 20260101_000000_example \
  --run-id run-001 \
  --action-id action-001 \
  --artifact /path/to/product/runs/run-001/RESULT.md \
  --summary 'Research result ready'
```

Exit `0` with status `delivered` means the exact Owner Session ran. Status `deduplicated` means the event was already delivered and no second Owner turn occurred. Exit `1` leaves the event and failed attempt durable for later recovery.

## Verification

```bash
python3 -m pytest tests/test_owner_event.py -q
```

The hardened live QuantResearch acceptance event is `7aa3c011dff1ee6b0b54c689e80970517736b6033f042d5f9c22e631d03f337f`. Its recorded command used exact `--resume 20260903_075757_73a49f`; Hermes reported that same Session on stderr. The Owner verified Result SHA-256 `1e7f84f80242c1d729f1ab431750c9c29eedb0b7998cc262d7a1699e93a87edf` and wrote `runs/notification-link-002/DECISION.md` without a manual Completion Signal. A duplicate of the earlier acceptance event produced `deduplicated` with no Owner message-count change. A separate invalid-owner smoke preserved a failed attempt without writing `delivered.json`.

See [`TDD.md`](TDD.md) for the observed RED → GREEN evidence.

## Next links

Reuse this exact terminal-event envelope for Assistant and Reviewer terminal outcomes. External signals, human decisions, and watchdog/dead-letter escalation remain separate later slices; they must not introduce a second terminal-result protocol.
