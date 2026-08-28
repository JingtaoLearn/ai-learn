# Parameter Study v1 production release evidence

## Reviewed source

- Integration PR: `#163`
- Merge commit: `47d478a5edcaf0b5aea0a34a112193f4cd3174d5`
- Reviewed source head: `205ef49208dd9b51306091614b45686bc1104786`
- Reviewed Git tree: `27b5c2f0694c2d3685a0d9222aa94f31d6261684`
- Feng release: `/home/feng/quant-platform/releases/47d478a`
- Installed execution source SHA-256: `0a069654b85b1bd0d405e159695a724cc2bc53216acd19c4a0320d8b7161e58b`
- Release manifest: 170 expected files, 170 actual files, no missing, extra, or mismatched files
- Release manifest SHA-256: `f6a7e3a0ded073f4899ca701db974246726ae7e790c6870cd2ae4d78cd90eb7f`
- Candidate runtime: `/home/feng/quant-platform/runtime/venv-ui-47d478a`
- Runner image: `sha256:2d7c6d52c80e8940e6d01d87ec89c2508c96eea0b3c5bb5a2e326d3b94eb9d05`

The release source has no write bits. Its only symlink is `.venv`, which points to the release-specific runtime above. The service unit binds both `WorkingDirectory` and `ExecStart` to the explicit immutable release path rather than the `current` convenience symlink.

## Automated gates

- Full isolated project suite: 800 passed, 3 skipped
- Focused Study, evaluation, UI, and runner suite: 83 passed
- Real Chromium acceptance: desktop and 390 px; JavaScript enabled and disabled; keyboard flows passed
- Ruff, Python compilation, and `git diff --check`: passed
- Gitleaks repository and staged scans: no leaks
- Docker image build: passed
- Local image manifest: `sha256:d3c367856227c55e8c8e3ce5f9a243bc9b031ad971cabd1a8fa6417c6722b1fc`
- No-network image import and CLI smoke: passed
- Feng systemd user `PrivateTmp=true` anchored-source test: passed
- Independent Standards, Spec, security, accessibility, UX, and adversarial financial/provenance reviews: passed after remediation

## Real-data Study acceptance

- Dataset: `601328.SS`
- Dataset snapshot: `562d375baf4f831cca39bd6cc697d454a4aefd9593a0710153325b14d4ff0100`
- Effective interval: 2024-01-02 through 2026-08-28
- Grid: `fit.window_sessions = [20, 40]`
- Validation: two outer folds, two inner folds, 20 scoring sessions, 120 minimum training sessions, one purge session
- Terminal holdout: 40 sessions, `FORCE_FLAT_WITH_COST`
- Primary Study: `548329f143b187f1055f4bf8ab38f4e58a142a9a47362d3027c5dc9d36254de7`
- Result: `CHAMPION_SELECTED`; terminal holdout `ACCESSED + PASSED`
- Trials: 2
- Bindings: 15
- Verified Metric Documents: 15
- Coordinator restart continuation: passed after a real lease expiry
- Reuse Study: `debabe6bd9e2e05922e792b2ac673e092e348dafa229a80b0114b542f6cef16a`
- Minimum inner Experiments reused: 10
- New selection-dependent outer Experiments: 1
- Attempt count: 15 before reuse Study, 16 after
- Independent candidate, Metric Document, evaluation, outer-evidence, net-return, and Sharpe recomputation: passed
- Sealed evidence SHA-256: `9dc69c918aa0ae5fb0411e3c60ed87a35a9609d74c410cf29238acccc0bc9360`

The holdout freshness was correctly reported as `PREVIOUSLY_EXPOSED`; the platform made no claim of global novelty.

## Production readback

- Final production hotfix merge: `e7632696d1b8281d356374c77170ab9a2ebe2904`
- Final production release: `/home/feng/quant-platform/releases/e763269`
- Final release manifest: 171 expected files, 171 actual files, no missing, extra, or mismatched files
- Final release manifest SHA-256: `8dc6a4fb4f2b203ad5d814e42ec3b35d1e931102f4797bf00fff3ccdee6a9b05`
- Final full isolated suite: 803 passed, 3 skipped
- The production loop now runs both `SerialStudyWorker` and `SerialAttemptWorker`; each worker has an independent logged exception boundary, so one failed Study cannot stop Attempt processing while health remains green.
- Service state: active/running
- Restart count after deployment: 0
- Working directory: `/home/feng/quant-platform/releases/e763269`
- Public health: HTTP 200
- Anonymous UI: redirects to `/login`
- Anonymous API: HTTP 401
- Production browser matrix using an opaque short-lived session: desktop and 390 px, JavaScript enabled/disabled, keyboard focus, form grouping, touch targets, and horizontal-overflow checks passed
- Microsoft SSO gateway target: `https://ms-login.ai.jingtao.fun/auth/login`
- Callback and audience: `https://quant.ai.jingtao.fun/auth/callback`
- TLS subject/SAN: `quant.ai.jingtao.fun`
- TLS validity: 2026-08-27 through 2026-11-25
- Service warnings/errors after deployment: 0/0

## Rollback

The original pre-release rollback point remains under `/home/feng/quant-platform/backups/pre-47d478a`. The post-hotfix rollback point is `/home/feng/quant-platform/backups/pre-e763269`; it restores release `47d478a`. Both contain the application state, systemd unit, and environment file needed for an explicit rollback.
