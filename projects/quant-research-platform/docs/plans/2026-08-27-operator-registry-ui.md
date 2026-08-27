# Operator Registry and Experiment UI Implementation Plan

**Goal:** Deliver the first production-shaped operator registry and experiment application for
the research platform. The phase separates immutable, independently published operators from
template-driven experiments, preserves the existing causal financial semantics, executes all
submitted Python only inside the established Docker boundary, and provides an authenticated,
server-rendered research UI at `quant.ai.jingtao.fun`.

**Architecture:** Add a service/CLI domain layer backed by SQLite WAL and immutable files below one
configured state root. SQLite owns transactional catalog pointers and experiment/attempt state;
content-addressed operator bundles and strategy run artifacts remain immutable on disk. Seed the
closed built-in operator set as published `1.0.0` bundles and seed immutable template
`single_stock_daily_causal@1`. Task submission resolves selectors before identity calculation and
adapts resolved built-ins into the existing config-driven runner rather than changing its replay,
accounting, or report semantics. Submitted source is treated as opaque bytes by the web process.
Compile, contract, fixture, and runtime loading for every slot occur only in a no-network,
read-only, resource-limited, non-root Docker process. Custom code receives no writable bind mount:
source/input are read-only and scratch space is bounded tmpfs. The host runner, not the web process,
container, or submitter, owns CID/stdout/stderr/control capture and creates validation evidence only
after confirmed termination. FastAPI/Jinja2 presents the same domain
services through JSON endpoints and semantic HTML. Authentication accepts one-use,
audience-bound HS256 assertions from the existing Microsoft login proxy or a deliberately
configured scrypt password fallback.

**Safety boundaries and non-goals:**

- Research only: no broker, order routing, live trading, paper trading, or deployment action.
- No template authoring UI in v1. Templates and published operator versions are immutable.
- A task never carries source, documentation, schema, defaults, or tests. It references only
  published selectors and supplies declared parameters.
- The web process never imports, compiles, or executes submitted operator source.
- No network access, container-writable evidence/source/input mounts, elevated user, added capability, or relaxed
  CPU/memory/PID/no-new-privileges control is introduced for operator validation or execution.
- All seven slots have narrow dynamic contracts and deterministic fixtures. A slot whose complete
  contract cannot be validated and executed in the existing sandbox fails publication explicitly;
  validation success is never fabricated.
- Immutable operator versions, experiment identities, attempts, and run artifacts are never
  overwritten or silently repaired.
- Production authentication is fail-closed. Password fallback is implemented and tested but is
  not enabled by committed production defaults.
- This phase adds deployment artifacts only. It does not push, deploy, restart, open a pull
  request, or read runtime secrets.

## Frozen contracts

### Template and operator model

- Template identity is `(name, version)`. The initial template is
  `single_stock_daily_causal@1` with required slots `fit`, `smoothing`, `statistic`, `decision`,
  `sizing`, `cost`, and `report`; it owns the existing template parameter schema.
- Operator identity is `(stable operator_id, semantic version)`. Every published version records
  its slot, exact immutable source bundle, supported JSON-schema subset, defaults, Chinese
  title/summary, sanitized Markdown documentation, creation timestamp, SHA-256 content digest,
  isolated validation evidence, and `PUBLISHED` status.
- Publishing identical content to an existing identity returns `NO_CHANGE`. Different content at
  the same identity is a conflict. `latest` is the highest published semantic version and is
  updated in the same transaction as publication. Older versions remain addressable.
- The accepted parameter schema subset is an exact JSON object with explicit properties, required
  names, `additionalProperties: false`, and scalar `string`, `integer`, `number`, or `boolean`
  properties supporting finite defaults, enum, minimum, and maximum where type-compatible.
- Built-ins are seeded as immutable `1.0.0` entries whose defaults preserve the committed BOCOM
  configuration. Their source bundle is an auditable descriptor rather than caller-controlled
  Python.

### Python plugin model

- A custom operator is one UTF-8 `operator.py` file with no symlink or hard-link alias. It exports
  `OPERATOR_API_VERSION = 1`, immutable `SLOT`, and callable `apply(payload, parameters)`.
- Each slot has a versioned JSON-in/JSON-out contract matching the existing pure built-in seam:
  `fit`, `smoothing`, `statistic`, `decision`, `sizing`, `cost`, and `report`. Fixtures contain
  canonical finite JSON input, parameters, and expected output. Slot adapters validate exact
  input/output schemas before results can enter replay, accounting, or report generation.
- Validation creates a sealed candidate bundle, mounts it read-only with the validator harness,
  gives custom code only bounded tmpfs, and runs the existing digest-pinned worker image with the
  established Docker envelope. The host captures CID/stdout/stderr outside container mounts,
  reconciles termination, rejects links/special files/hostile modes, and seals its own evidence.
  Publication requires successful compile, metadata, forbidden import, synthetic contract, and
  submitted fixture checks with checksummed host-owned evidence.
- Experiment execution resolves only published bundles. Built-ins use the existing in-process
  numerical implementation. Any composition containing custom operators is assembled and executed
  as one isolated replay launch so cross-slot values never require repeated container launches and
  submitted modules never enter the web process.

### Experiment and attempt model

- Canonical experiment identity binds template name/version and template parameters; immutable
  dataset instrument/snapshot; resolved operator IDs, versions, content digests, and parameters;
  plus source/runtime execution identity. One database row exists per identity.
- Normal submission resolves all selectors and atomically creates or returns the experiment.
  Exact duplicate submission returns the existing experiment and creates no attempt.
- Rerun accepts only `experiment_id` plus one idempotency/action ID; it never accepts a task,
  selectors, parameters, or source. It atomically creates one new attempt with the experiment's
  already-frozen resolution. The initial non-duplicate submission creates one attempt. A caller
  action ID is unique so request retries cannot create extra attempts.
- Attempts retain, for every action and slot, requested selector mode/value, latest version and
  digest at action time, frozen resolved version and digest, timestamps, state, logs, CID/control
  evidence, result/report identity, and canonical output
  comparison. Normal terminal states are `SUCCEEDED|FAILED`; restart reconciliation additionally
  uses `INTERRUPTED|TERMINATION_UNCONFIRMED`.
- Each attempt has an immutable launch count constrained to zero or one. Claiming records the
  launch before process creation, and no recovery path may launch that attempt again. Restart
  recovery reconciles runner-owned CID/control evidence through the existing termination and
  quarantine boundary. It preserves the prior attempt as `INTERRUPTED` after confirmed termination
  or `TERMINATION_UNCONFIRMED` when survival cannot be excluded. Explicit recovery policy requires
  a distinct replacement attempt/action; it never returns the same attempt to `PENDING`. A failed
  or interrupted attempt never establishes canonical success.
  First success becomes canonical. Equal later success points at the canonical run; divergent
  output remains immutable and is flagged.
- Drift is computed at read time by comparing each resolved version/digest with the current latest
  published version.

### Authentication and HTTP model

- Production SSO login redirects to
  `https://ms-login.ai.jingtao.fun/auth/login?redirect=<exact callback>`.
- `POST /auth/callback` accepts form-urlencoded `token`; verifies exact three-part HS256 JWT,
  signature, an `aud` claim exactly equal to
  `https://quant.ai.jingtao.fun/auth/callback`, `iat`, `exp`, short lifetime, email, and allowlist;
  stores a token SHA-256 once in
  SQLite before creating a signed session. Token bytes are never logged.
- Signed sessions contain only user identity, issued/expiry time, and CSRF seed, and use
  `Secure; HttpOnly; SameSite=Lax`. Logout is a CSRF-protected POST.
- Every mutation requires session authentication, constant-time CSRF verification, allowed host,
  and exact same-origin checks. Login endpoints have bounded per-address rate limits.
- Password fallback verifies one configured scrypt hash in constant time and refuses startup when
  required values are absent. Committed production examples select SSO and contain placeholders.
- The exact public callback URL and JWT audience are represented in deployment examples and the
  Microsoft login callback allowlist documentation; production startup rejects mismatch.
- JSON routes always return JSON errors. HTML routes render escaped error states. Security
  middleware adds CSP, HSTS for forwarded HTTPS, `X-Content-Type-Options`, strict referrer policy,
  and same-origin framing.

## Task 1: Transactional catalog schema and built-in migration

**Files:**

- Create `src/quant_platform/catalog.py`
- Create `src/quant_platform/schemas.py`
- Create `src/quant_platform/seed.py`
- Create `tests/test_operator_catalog.py`
- Create `tests/test_operator_schema.py`

**RED:**

1. Prove initialization enables foreign keys, busy timeout, and WAL; creates versioned migrations;
   and is idempotent under concurrent startup.
2. Prove the template is immutable and exposes exact slot/default ownership.
3. Prove all existing built-ins seed as published `1.0.0`, have valid schemas/defaults/docs and
   immutable digests, and preserve the existing BOCOM values.
4. Prove semantic version ordering handles numeric major/minor/patch values, rejects non-canonical
   or prerelease syntax, and updates latest only for a higher published version.
5. Prove unique/check constraints prevent duplicate operator versions, experiment identities,
   action IDs, replay-token hashes, any attempt launch count outside zero or one, and illegal
   attempt statuses including recovery-only terminal states.

**GREEN:**

- Add narrow connection/transaction helpers, numbered schema migrations, normalized catalog
  tables, foreign keys, and indexes.
- Implement exact schema/default validation and canonical finite JSON helpers.
- Convert the existing literal registry into deterministic seed descriptors without changing its
  numerical functions. Materialize each seed through the normal immutable publication path.

**REFACTOR/GATE:** Keep SQL and filesystem naming centralized, use explicit transactions, run the
two focused test files, and inspect the state root to ensure no mutable files live inside bundles.

**Commits:**

- `test(quant): specify immutable operator catalog`
- `feat(quant): add transactional operator catalog`

## Task 2: Immutable operator submission and isolated validation

**Files:**

- Create `src/quant_platform/operator_service.py`
- Create `src/quant_platform/operator_worker.py`
- Create `tests/test_operator_submission.py`
- Create deterministic fixtures under `tests/fixtures/operators/` for all seven slots
- Modify `src/quant_platform/isolation.py`

**RED:**

1. Cover source/manifest/schema/default/docs/fixture exactness, size limits, UTF-8, duplicate JSON
   keys, finite numbers, undeclared defaults, and Markdown sanitization.
2. Cover `CREATED`, `NO_CHANGE`, same-version conflict, old-version addressability, semantic latest,
   and concurrent identical submission convergence.
3. Cover path traversal, absolute paths, symlink/hardlink source, unsafe state root, partial
   publication, and candidate/bundle race replacement.
4. Assert the exact validator Docker command: pinned image, no pull/network, read-only root and
   source/input, non-root, no-new-privileges, cap-drop, CPU/memory/PID limits, bounded tmpfs, no
   writable bind mount, and host-only CID/stdout/stderr/control paths.
5. Assert host-runner-owned evidence binds the candidate digest, validator image digest, execution
   envelope, fixture digest, observed checks, CID/exit/termination state, captured stream digests,
   timestamps, and outcome. Reject caller/container-supplied evidence and any evidence whose
   binding differs from the staged candidate.
6. Run safe deterministic fixtures for all seven slot contracts through compile/contract/fixture
   validation where Docker is available; otherwise test the runner contracts directly and command
   construction separately.
7. Prove timeout and launch failure terminate by exact CID, surviving/unconfirmed containers
   quarantine the candidate/control evidence, evidence finalization rejects links, special files,
   and hostile chmod, and no publication occurs before confirmed termination.
8. Prove imports or side effects outside the narrow contracts fail rather than being marked
   runnable.

**GREEN:**

- Stage candidate bytes under a private locked directory, fsync, validate in Docker, checksum the
  evidence, atomically rename to the digest/version target, seal `0555/0444`, then insert catalog
  rows and latest pointer under an immediate SQLite transaction and filesystem lock.
- Implement a validator/executor harness that imports submitted source only in its container
  process and emits one bounded strict-JSON result on stdout for every slot. The host runner owns
  all control/evidence paths, confirms termination, hashes bounded stdout/stderr, builds evidence,
  and only then seals/publishes the bundle.
- Expose service methods for submit, grouped list, and version detail.

**REFACTOR/GATE:** Reuse the established Docker envelope constants and safe file readers. Run
focused tests and scan bundles for writable, linked, unexpected, or unchecksummed files.

**Commits:**

- `test(quant): specify isolated operator publication`
- `feat(quant): publish validated operator versions`

## Task 3: Resolution, experiment identity, and attempts

**Files:**

- Create `src/quant_platform/experiment_service.py`
- Create `src/quant_platform/worker.py`
- Create `tests/test_experiment_service.py`
- Create `tests/test_attempt_worker.py`

**RED:**

1. Prove a task rejects source and all operator-publication fields.
2. Cover default/latest and explicit selectors, unknown operator, wrong slot, unpublished version,
   invalid/unknown parameters, defaults, and resolution audit fields.
3. Prove exact duplicate and concurrent submits converge to one experiment and one initial
   attempt; duplicate submission adds no history and does not run.
4. Prove explicit rerun accepts only an existing `experiment_id` and action ID, creates an attempt
   under that experiment, and action retries converge. Task/selectors/parameters/source are not
   accepted on rerun.
5. Prove every submit/rerun attempt records, per slot, requested selector mode/value, current latest
   version/digest for that action, and frozen resolved version/digest. After latest advances, rerun
   executes the original frozen resolution while its action audit records the new latest pointer.
6. Cover attempt state transitions, an atomic zero-to-one launch count, launch failure, restart
   reconciliation through CID/control evidence to `INTERRUPTED` or `TERMINATION_UNCONFIRMED`,
   quarantine/termination confirmation, log bounds, and illegal transitions. Prove no attempt can
   be claimed or launched twice and explicit recovery creates a distinct attempt ID.
7. Cover first canonical success, equal rerun artifact sharing, divergent rerun preservation and
   prominent flagging, and failed reruns not replacing canonical success.
8. Cover current latest drift and unique-experiment history filtering.

**GREEN:**

- Resolve and validate a task inside one immediate transaction, calculate canonical identity, and
  use unique constraints plus action IDs for convergence.
- Add serial worker claim/reconciliation and explicit state transition methods. Increment launch
  count in the claim transaction, persist runner control identity before launch, reuse the existing
  exact-CID termination/quarantine semantics, and never transition a launched attempt back to
  `PENDING`.
- Store bounded logs and immutable run identities; compare canonical output from stable metrics and
  ledger checksums rather than mutable paths.

**REFACTOR/GATE:** Make writes domain-service-only, keep HTTP absent, run both focused test files,
and inspect duplicate/rerun transaction boundaries.

**Commits:**

- `test(quant): specify experiments and attempts`
- `feat(quant): add experiment attempt domain`

## Task 4: Resolved-config execution adapter and BOCOM acceptance

**Files:**

- Create `src/quant_platform/resolved_runner.py`
- Create `tests/test_resolved_runner.py`
- Create `tests/fixtures/platform/bocom-task.json`
- Modify `src/quant_platform/strategy_config.py`
- Modify `src/quant_platform/strategy_runner.py`

**RED:**

1. Resolve seeded operator `1.0.0` versions into the existing config version `1` names and versions
   without broadening the public legacy config parser.
2. Prove the resolved BOCOM task yields byte-equivalent accounting metrics, events, trades, and
   report semantics to the existing config-driven runner.
3. Prove run/report manifests include experiment, attempt, requested selector, resolved version,
   operator digest, template, dataset, and execution identity.
4. Cover a mixed composition spanning custom implementations for all seven slots through one
   isolated replay launch with no web-process import, plus explicit failure when Docker or any
   published contract is unavailable.
5. Prove immutable run artifacts are reused only after complete verification.

**GREEN:**

- Build a private adapter from resolved catalog descriptors to `ValidatedStrategyConfig` and call
  the existing replay/publication path for built-ins.
- Add audit metadata as a sidecar owned by the experiment state root, leaving existing strategy
  artifact identities and financial implementation intact.
- Add a composition manifest and one isolated replay bridge per attempt that loads every resolved
  custom slot bundle inside the runner and validates each JSON seam against its published contract.

**REFACTOR/GATE:** Diff deterministic built-in outputs against the legacy path and run all existing
strategy config/operator/replay/report/runner tests with the new focused test.

**Commits:**

- `test(quant): preserve resolved strategy semantics`
- `feat(quant): execute resolved operator experiments`

## Task 5: JSON-only CLI and web authentication

**Files:**

- Modify `src/quant_platform/cli.py`
- Create `src/quant_platform/auth.py`
- Create `src/quant_platform/settings.py`
- Create `tests/test_platform_domain_cli.py`
- Create `tests/test_auth.py`
- Modify `pyproject.toml`
- Modify `requirements.in`
- Regenerate `requirements.lock`

**RED:**

1. Cover operator submit/list/detail, template detail, task resolve/submit/rerun, experiment
   list/detail, and attempt list/detail as one-line JSON success or error.
2. Cover HS256 algorithm confusion, malformed segments, bad signature, missing/wrong/multiple
   audience claims, future iat, expiry, excessive lifetime, case-normalized allowlist, token replay,
   and token non-disclosure.
3. Cover signed session tampering/expiry, CSRF, origin/host enforcement, logout, and login
   rate-limiting boundaries.
4. Cover scrypt success/failure, malformed hash, missing fallback configuration, and production
   startup failure for incomplete SSO/session/allowlist configuration.

**GREEN:**

- Add domain CLI subcommands with strict JSON input loading and one JSON output object.
- Implement JWT and signed sessions with stdlib `hmac`, `hashlib`, `base64`, `secrets`, and strict
  parsers; persist replay hashes through the catalog and require exact configured audience
  equality.
- Load settings explicitly without reading `.env`; validate the selected mode at application
  creation.
- Declare FastAPI, Jinja2, Uvicorn, Markdown, and Bleach as direct pinned dependencies while
  retaining generated hash integrity.

**REFACTOR/GATE:** Keep auth pure except explicit replay/rate stores, run focused CLI/auth tests,
and confirm no assertion/session/token value appears in logs or error bodies.

**Commits:**

- `test(quant): specify domain CLI and authentication`
- `feat(quant): add secure platform authentication`

## Task 6: FastAPI JSON API and server-rendered UI

**Files:**

- Create `src/quant_platform/web.py`
- Create `src/quant_platform/templates/base.html`
- Create `src/quant_platform/templates/login.html`
- Create `src/quant_platform/templates/dashboard.html`
- Create `src/quant_platform/templates/operators.html`
- Create `src/quant_platform/templates/operator_detail.html`
- Create `src/quant_platform/templates/operator_submit.html`
- Create `src/quant_platform/templates/template_detail.html`
- Create `src/quant_platform/templates/experiment_new.html`
- Create `src/quant_platform/templates/history.html`
- Create `src/quant_platform/templates/experiment_detail.html`
- Create `src/quant_platform/templates/error.html`
- Create `src/quant_platform/static/app.css`
- Create `src/quant_platform/static/app.js`
- Create `tests/test_web_api.py`
- Create `tests/test_web_ui.py`

**RED:**

1. Cover health, authentication redirects, callback/login/logout, every JSON domain route, JSON
   error shape/status, mutation security, and request/body limits.
2. Cover dashboard totals/recent attempts/failures and unique-experiment history.
3. Cover grouped operator versions/latest/status/docs, no-JS submission, validation result, and
   safe limited Markdown.
4. Cover template slot/default ownership and new-experiment controls for dataset, latest/explicit
   versions, generated schema parameters, resolved summary, duplicate preview, submit, and rerun.
5. Cover history filters/search/status/drift/attempt count and experiment identity/timeline/
   canonical metrics/report link.
6. Inject XSS payloads through every user-controlled display field and prove escaped or sanitized
   output.
7. Prove report responses never serve arbitrary paths: resolve only a successful attempt's
   database-bound immutable artifact; reject symlinks, hardlinks, path escapes, writable or
   unverified files; send a separate restrictive report CSP with `default-src 'none'`,
   `connect-src 'none'`, inline style/script and data-image allowances only; and embed with exactly
   `sandbox="allow-scripts"` without same-origin, navigation, downloads, forms, or popups.
8. Assert durable browser selectors and responsive accessibility structure: landmarks, labels,
   focus states, 44 px targets, reduced motion, scroll-contained tables, and no page overflow.

**GREEN:**

- Build an application factory with injected settings/services, JSON exception handlers, auth and
  security middleware, static mounts, and semantic server-rendered routes.
- Implement primary forms without JavaScript; use small external JavaScript only for live operator
  resolution/parameter controls, duplicate preview, submission progress, and table scroll cues.
- Apply the explicit Linear-inspired tokens: `#08090a` canvas, `#0f1011` surfaces, restrained
  `#5e6ad2` accent, system/Inter-compatible sans, mono technical labels, subtle borders, no
  gradients, glass effects, emoji, or decorative dashboard cards.
- Render empty/loading/success/error states. Serve reports only through a dedicated ID-based,
  containment-checked, fully sealed/verified artifact route and embed with
  `sandbox="allow-scripts"` but never `allow-same-origin`.

**REFACTOR/GATE:** Run API/UI unit tests, then real browser tests at 390 px and desktop with
JavaScript enabled and disabled. Use keyboard navigation and exercise operator submission,
latest/explicit selection, schema-generated parameters, duplicate preview, experiment submission,
progress, history, detail, rerun, and report sandbox behavior. Static selectors alone are not an
acceptance substitute. Inspect generated parent HTML for inline script/style/event handlers and
verify the main app CSP remains strict and separate from the untrusted report CSP.

**Commits:**

- `test(quant): specify operator registry web flows`
- `feat(quant): add authenticated research platform UI`

## Task 7: Worker lifecycle and deployment artifacts

**Files:**

- Create `projects/quant-research-platform/deploy/quant-research-ui.service`
- Create `projects/quant-research-platform/deploy/quant-research-ui.env.example`
- Create `vm/host-services/quant-research-tunnel/README.md`
- Create `vm/host-services/quant-research-tunnel/quant-research-tunnel.service`
- Create `vm/host-services/quant-research-tunnel/run-tunnel.sh`
- Create `vm/docker-services/quant-research-ui-proxy/docker-compose.yml`
- Create `vm/docker-services/quant-research-ui-proxy/nginx.conf`
- Create `vm/docker-services/quant-research-ui-proxy/README.md`
- Modify `vm/host-services/README.md`
- Modify `vm/docker-services/README.md`
- Modify `projects/quant-research-platform/.gitignore`
- Modify `projects/quant-research-platform/README.md`
- Modify `tests/test_deployment.py`

**RED:**

1. Assert Uvicorn binds only `127.0.0.1:8090`, runs as Feng's non-root user, uses
   `/home/feng/quant-platform/state/ui`, validates startup, recovers attempts, and starts one serial
   worker.
2. Assert environment examples contain placeholders, production mode is SSO, and real env files
   are ignored.
3. Assert the ailearn tunnel resolves the nginx-proxy bridge gateway once, rejects
   empty/loopback/wildcard/multiple resolution, and uses that exact same address as both the SSH
   bind address and literal nginx upstream address, with no `host-gateway` alias or independent
   second lookup. It encrypts transport to Feng loopback, has strict host-key checking, and fails
   rather than selecting another interface.
4. Assert nginx uses pinned `nginx:alpine` image identity where repository policy permits,
   nginx-proxy external network, no host port, literal resolved-gateway upstream, body/time limits, health
   handling, secure proxy headers, HSTS, CSP-compatible headers, and no buffering of secrets.
   Uvicorn trusts forwarding only from the SSH endpoint; the sidecar accepts client address
   forwarding only from the explicitly trusted outer proxy and replaces hostile client headers.
   If a trustworthy address is unavailable, authentication limits use a normalized account/login
   key rather than a globally shared proxy address.
5. Assert deployment configuration adds the exact
   `https://quant.ai.jingtao.fun/auth/callback` to `ALLOWED_CALLBACKS` and makes ms-login issue the
   same value as the 30-second JWT `aud`, without reading or printing the shared secret. Document
   the shared-secret trust domain and compatibility for consumers that ignore the added claim.
6. Run a real acceptance probe from the proxy container through the exact-address tunnel to Feng's
   application `/health`, not merely a local nginx synthetic health response.
7. Validate systemd units, shell syntax, Compose rendering, and nginx syntax where local tools are
   available.

**GREEN:**

- Add reviewed user-service and placeholder environment templates.
- Add a strict gateway-resolution tunnel script and systemd unit using encrypted SSH forwarding.
- Add the nginx-proxy sidecar and update service catalogs/documentation.

**REFACTOR/GATE:** Keep scripts fail-fast and English, run deployment tests and available static
validators, and confirm no service was started or restarted.

**Commits:**

- `test(quant): specify UI deployment boundary`
- `feat(quant): add quant UI deployment artifacts`
- `docs(quant): document operator research platform`

## Task 8: Full acceptance and repository hygiene

**RED/GREEN verification sequence:**

1. Run each focused pytest file during its owning task, observing the expected failure before
   implementation and a pass after implementation.
2. Run the complete Python 3.12 suite serially.
3. Run Ruff over `src` and `tests`.
4. Run `node --test tests/test_strategy_lab.js`.
5. Run Gitleaks against the worktree without transmitting repository content.
6. Run available `systemd-analyze verify`, `bash -n`, `docker compose config`, and
   `nginx -t`-equivalent static checks without starting services.
7. Execute a deterministic seeded BOCOM/synthetic task, verify one experiment/attempt and the
   expected accounting, submit the exact duplicate and verify no new attempt, rerun explicitly and
   verify equality/canonical sharing.
8. Execute deterministic fixtures for custom operators in all seven slots and one composed custom
   replay through a single isolated launch where Docker is available; if the daemon is unavailable,
   retain passing worker-contract and exact-command tests and record that environmental limitation.
9. Exercise real-browser desktop and 390 px keyboard flows with JavaScript enabled and with primary
   actions JavaScript disabled: operator submission, latest/explicit selection, generated
   parameters, duplicate preview, rerun, history, and `allow-scripts`/opaque-origin report sandbox.
10. Confirm production startup fails without auth configuration, all API errors are JSON, all
   generated JSON rejects NaN/infinity, and all user content is escaped/sanitized.
10. Confirm committed files are English except permitted Chinese UI/operator copy, no secrets or
    runtime artifacts are tracked, every requested change is committed, and `git status --short`
    is empty.

**Final commit:** `test(quant): verify operator registry acceptance`

## Acceptance criteria

- Operators are independently submitted, validated, versioned, immutable, auditable, and resolved
  by atomic latest pointers; built-ins remain the initial complete functional path.
- Tasks cannot contain source and can select only published compatible versions with valid declared
  parameters.
- Exact duplicate tasks converge without added history; explicit reruns add attempts beneath one
  immutable experiment identity.
- Attempts recover safely, preserve all resolution audit fields, and distinguish canonical equal
  output from deterministic divergence.
- The initial BOCOM task resolves seeded `1.0.0` operators and preserves the proven financial
  accounting/report results.
- No submitted source is imported or executed by the web process; custom implementations for all
  seven slots validate and execute only within one hardened Docker launch per attempt.
- Microsoft SSO exact callback-as-audience binding, replay prevention, secure sessions, CSRF/origin/host
  checks, rate limiting, and fail-closed password fallback are deterministic and tested.
- The professional dark UI provides all required pages and primary no-JS flows, is secure against
  XSS, serves reports only through the verified sandbox route, and passes real-browser acceptance
  at 390 px and desktop with JavaScript enabled and disabled.
- Reviewed systemd, encrypted tunnel, and nginx-proxy artifacts satisfy the fixed Feng/ailearn
  topology without deployment.
- Existing and new Python tests, Ruff, Node tests, Gitleaks, and available static deployment
  validators pass; the final local worktree is clean.
