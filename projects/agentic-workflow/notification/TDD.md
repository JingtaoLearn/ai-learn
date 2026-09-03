# TDD evidence — terminal-result notification

The notification adapter was developed through observed RED → GREEN cycles.

## Cycle 1 — missing adapter

Command:

```bash
python3 -m pytest tests/test_owner_event.py -q
```

Observed RED: test collection failed with `ModuleNotFoundError: No module named 'notification'`.

GREEN: the initial adapter implementation made six focused behavior tests pass.

## Cycle 2 — nested Profile credentials

A live ResearchAgent emission durably failed twice because the nested Owner process resolved the source Profile environment and reported `No usable credentials found for provider 'copilot'`.

The regression test imported `build_delivery_environment` before it existed and failed during collection. After adding the boundary, seven tests passed. The first implementation still retained arbitrary specialist environment variables; the independent review later forced the whitelist hardening in Cycle 4.

## Cycle 3 — authoritative Session metadata

A live Owner trigger succeeded, but the adapter marked it failed because Hermes writes authoritative `session_id:` metadata to stderr while the Owner response is stdout.

The focused test `test_accepts_owner_session_id_emitted_on_stderr` failed with `expected=owner-session-1 observed=None`. Parsing CLI metadata made eight tests pass.

## Cycle 4 — independent review blockers

Independent Spec and Standards reviews identified exact-Session targeting, crash initialization, launch-failure evidence, profile-secret isolation, private modes, and fsync durability gaps. Tests were added before the fixes.

Observed RED:

```text
7 failed, 7 passed
```

The seven failures covered:

1. missing exact `--resume <owner_session_id>` targeting;
2. model stdout able to spoof Session metadata;
3. arbitrary specialist environment variables crossing the Profile boundary;
4. launch exceptions not producing durable attempts;
5. world/group-readable event and response files;
6. incomplete event initialization not failing closed;
7. no file or directory fsync calls.

GREEN after the bounded fixes:

```text
14 passed
```

## Hardened live acceptance

ResearchAgent-QuantResearch emitted event `7aa3c011dff1ee6b0b54c689e80970517736b6033f042d5f9c22e631d03f337f`. The recorded command used exact `--resume 20260903_075757_73a49f`; Hermes reported that same Session on stderr; the Owner verified artifact SHA-256 `1e7f84f80242c1d729f1ab431750c9c29eedb0b7998cc262d7a1699e93a87edf` and wrote `runs/notification-link-002/DECISION.md`. Event directories are mode `0700`; event, prompt, attempt, response, and delivery files are mode `0600`.
