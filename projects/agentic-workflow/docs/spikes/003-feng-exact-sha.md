# Spike 003: Feng Exact-SHA Test Receipt

- Status: `VALIDATED`
- Type: Execution prototype
- Prototype branch: `prototype/agentic-feng-receipt`
- Prototype commit: `34a15f7afa966a805019217b1d2e86084ea68d02`

## Question

Can Feng accept one immutable test manifest, execute only the declared test profile against the exact source commit/tree in an isolated workspace, and return evidence that the kernel can independently verify?

## Cheapest valid prototype

Transfer a Git bundle or fetchable commit plus a canonical manifest to Feng. The runner creates an isolated workspace, verifies commit/tree identity, runs one deterministic command under time/resource bounds, hashes stdout/stderr/artifacts, and returns a canonical receipt. Repeat with a wrong SHA and duplicate delivery ID.

## Predeclared verdicts

- `VALIDATED`: correct exact-SHA execution returns a verifiable receipt; wrong SHA fails before the test; duplicate delivery resolves to the original run; local protected resources remain unchanged; cleanup succeeds.
- `PARTIAL`: exact execution works but one resource or cleanup guarantee is not yet enforceable and production use remains disabled.
- `INVALIDATED`: Feng can run a different tree, duplicate a logical run, omit artifact identities, or mutate protected state.
- `INCONCLUSIVE`: Feng or required runtime access is unavailable.

## Evidence required

- input manifest and digest;
- accepted commit/tree and workspace identity;
- command, deadline, resource envelope, exit code, stdout/stderr hashes;
- artifact hashes and cleanup result;
- wrong-SHA and duplicate-delivery results.

## Verdict evidence

A clean Git bundle containing the prototype commit was transferred to a new Feng `/tmp` directory, cloned at detached exact HEAD, executed with system Python, and removed afterward.

- accepted commit: `34a15f7afa966a805019217b1d2e86084ea68d02`;
- accepted tree: `8b89b212c66966d1d17f841a5024f032569309c7`;
- declared command exit: `0`;
- duplicate delivery returned the original receipt: `true`;
- receipt canonicalization: `true`;
- wrong SHA failed in `preflight` with exit `2`;
- wrong-SHA test execution started: `false`;
- receipt SHA-256: `0e90c42df414a0caf4c9c91952fbfc392ab22a31f30c2607d4166f92cb469121`;
- temporary Feng bundle and workspace were removed by the bounded probe.

The observed behavior meets every `VALIDATED` criterion for this prototype. Production sandboxing, signing, stronger resource controls, and a durable queue remain implementation work.

## Boundary

The prototype runs one harmless deterministic test. It does not install an always-on service, expose a network port, or authorize production deployment.
