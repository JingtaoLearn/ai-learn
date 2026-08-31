# Release deployment

`deploy-release.sh` installs one prepared immutable release into the existing Feng user service:

```bash
./deploy/deploy-release.sh EXACT_RELEASE_ID
```

This self-contained helper is an explicit project-level exception to the numbered `vm/scripts`
convention: it does not source `vm/scripts/lib/common.sh`. An immutable project release must remain
deployable when that repository-level library is unavailable or belongs to a different checkout.
Issue #175 also requires retained rollback evidence instead of flag-based idempotency. The existing
rollback directory is therefore a safety interlock: a subsequent invocation fails until an operator
reviews and deliberately archives or removes that evidence.

The immutable source release must already exist under `/home/feng/quant-platform/releases/`.
Its matching runtime must be a non-symlink directory at
`/home/feng/quant-platform/runtime/venv-ui-EXACT_RELEASE_ID` with an executable `bin/python`;
the source release does not need a `.venv`. The active user unit, private environment file,
and `state/platform/catalog.sqlite3` must exist as regular, non-symlink files.

Before changing the service, the helper creates `/home/feng/quant-platform/rollback` and stores a
consistent SQLite backup plus protected copies of the active unit and environment file. It never
prints the environment contents. An existing rollback path causes deployment to stop without
changing the service; the helper never overwrites rollback evidence.

The helper updates only `QUANT_PROJECT_ROOT` in the active environment file and substitutes the
same release ID into the immutable source `WorkingDirectory` and per-release runtime `ExecStart`
in the reviewed unit template. It then verifies:

- local `/health` with `Host: quant.ai.jingtao.fun` and an exact `{"status":"ok"}` body from a
  currently successful curl request;
- contiguous catalog migrations 1 through 9;
- the exact immutable release path reported as the systemd `WorkingDirectory`;
- the exact per-release runtime command reported as the systemd `ExecStart`;
- public `/health`;
- an unauthenticated public root redirect with HTTP 303;
- an unauthenticated public `/api/operators` rejection with HTTP 401;
- systemd `NRestarts=0`.

Any failure after backup creation stops the candidate service, restores the unit, environment, and
catalog, reloads systemd, and restarts the previous unit. The rollback directory remains after both
success and rollback. Review and archive or deliberately remove it before the next deployment.

This helper does not prepare release contents, install dependencies, expose a public port, connect
to a broker, or add any live-order path.
