# TDD evidence — Session message CLI

## RED

The target API was written first in `tests/test_session_send.py`:

- exact Profile and Session targeting;
- private temporary message file and cleanup;
- stderr-only authoritative Session identity;
- fail-closed Session mismatch;
- target process failure propagation;
- source-Profile secret isolation;
- inline/file message-source CLI behavior;
- machine-readable JSON response.

The first focused run failed during collection:

```text
ModuleNotFoundError: No module named 'notification.session_send'
```

This established that the tests exercised a missing capability rather than pre-existing behavior.

## GREEN

The minimum `notification/session_send.py` implementation made all focused tests pass:

```text
8 passed
```

## Live acceptance

The CLI was then invoked with a nested source environment:

- source environment: `ResearchAgent-QuantResearch` Profile home;
- target Profile: `productowneragentquantresearch`;
- exact target Session: `20260903_075757_73a49f`;
- message: `SESSION-CLI-SMOKE-001`;
- required response: `OWNER_SESSION_DELIVERY_OK`.

Observed JSON:

```json
{"ok": true, "profile": "productowneragentquantresearch", "response": "OWNER_SESSION_DELIVERY_OK\n", "session_id": "20260903_075757_73a49f"}
```

The exact existing Owner Session handled the message. No Bot Chat title lookup, replacement Session, file mutation, repository mutation, external action, or production change occurred.
