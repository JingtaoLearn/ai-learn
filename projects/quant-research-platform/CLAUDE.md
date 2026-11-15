# CLAUDE.md

Trend Sentinel — see README.md for the signal definition, topology and deploy.

- Signals only: never add broker connectivity or order placement.
- Signals must use only data up to the decision close; trades execute at the next open.
- Keep it small. Add a test only for behaviour that matters (engine math, signal dedupe, alerts); no governance/audit scaffolding.
- Unit tests never hit the network; mock `quant.data`.
- Services bind to 127.0.0.1; never print `.env` contents or tokens.
- Changing `engine.py` semantics changes live signals: keep `tests/test_engine.py` (v1 equivalence) green or state the intended change.
