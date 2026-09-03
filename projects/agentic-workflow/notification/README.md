# Session message CLI

This is the complete lightweight transport primitive for the current Agentic Workflow: send one message to one exact Hermes Agent Session and return that Agent's response to the caller.

It is not a message broker, workflow runtime, scheduler, database, outbox, or retry service.

## Contract

The caller supplies:

- target Hermes Profile ID;
- exact target Session ID;
- target project working directory;
- exactly one message source: inline text or a UTF-8 file.

The CLI then:

1. writes the message to a private `0600` temporary file;
2. rebuilds a small operational environment so source-Agent credentials do not cross the Profile boundary;
3. invokes `hermes -p <profile> chat --resume <session> --in <workdir> -Q --query-file <temporary-file>`;
4. accepts Session identity only from Hermes CLI metadata on stderr;
5. fails if the observed Session differs from the requested Session;
6. prints one JSON result containing the exact target Profile, Session ID, and Agent response;
7. deletes the temporary message file on success or failure.

There is no title lookup and no `--create-if-missing`; an invalid or missing Session fails closed instead of creating a replacement brain.

## CLI

Inline message:

```bash
python notification/session_send.py \
  --profile productowneragentquantresearch \
  --session 20260903_075757_73a49f \
  --workdir /home/jingtao/.hermes/workflows/quant-research \
  --message 'RESULT_READY run-001 /path/to/RESULT.md'
```

Message file:

```bash
python notification/session_send.py \
  --profile productowneragentquantresearch \
  --session 20260903_075757_73a49f \
  --workdir /home/jingtao/.hermes/workflows/quant-research \
  --message-file /path/to/message.md
```

Successful output:

```json
{"ok": true, "profile": "productowneragentquantresearch", "response": "...", "session_id": "20260903_075757_73a49f"}
```

## Agent usage

A specialist writes its Result first, then calls this CLI with a compact message containing the result type, run/action identity, artifact path, and artifact SHA-256. The target Owner turn reads and verifies the artifact before deciding what happens next. Assistant, Reviewer, external adapters, and human-decision adapters use the same CLI; they differ only in message content.

The caller owns retry and message-level idempotency. Those policies stay outside this transport primitive unless real failures later prove that a shared broker is necessary.

## Verification

```bash
python3 -m pytest tests/test_session_send.py -q
```

The clean nested-Profile smoke set `HERMES_HOME` and `HOME` to `ResearchAgent-QuantResearch`, then sent `SESSION-CLI-SMOKE-001` to exact Owner Session `20260903_075757_73a49f`. The CLI returned:

```json
{"ok": true, "profile": "productowneragentquantresearch", "response": "OWNER_SESSION_DELIVERY_OK\n", "session_id": "20260903_075757_73a49f"}
```

See [`TDD.md`](TDD.md) for observed RED → GREEN evidence.
