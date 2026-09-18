# Validation Evidence

## Artifact identity

- Private sanitized snapshot SHA-256: `f9d8443595fcce5241f590973e3868e0351519842e3e2ed4643adbac18a70480` (untracked)
- Private preview SHA-256: `07c0c202efaaba7458edd8f03bac0adcfaf561d5581f585609a53aeb320fdf76` (untracked)
- Synthetic example preview SHA-256: `ca9d7651517099524f51be5e060a7a35bb66f7344b1ad5e199a8d06feba7b44e`
- Embedded tasks: `89`
- Embedded boards: `3`

## Static and build checks

- Deterministic rebuild: PASS; before and after preview digests are identical.
- HTML/parser/data-contract check: PASS.
- Executable JavaScript `node --check`: PASS.
- Strict exact-field allowlist: PASS; free-text summaries are removed in the traditional privacy posture.
- Forbidden path/repository/branch-reference scan: PASS in the private rendered artifact.
- Mutation-surface scan: PASS (`fetch`, `XMLHttpRequest`, and POST form absent).
- Docker image build from the tracked synthetic fixture: PASS (`sha256:4edc51e9552c3ce8bd234a3a7f5a51514f9d70dbb39461afc72888f964251d45`).
- Gitleaks repository and staged scans: PASS.
- Repository privacy: PASS; the real snapshot and generated preview are ignored and absent from `git ls-files`.

## Browser checks

Public private-link preview was loaded in Chromium.

- `390×844`: 89 task cards and no horizontal overflow; task details use the strict brief allowlist.
- Detail interaction: native dialog opened, correct task title rendered, sheet width 390px.
- Search interaction: `FOCuS` reduced the list to 9 matching tasks.
- Board filter: Quant Research Platform showed 80 snapshot tasks.
- `360×740`: no horizontal overflow; three metric cards remain within viewport; blocked quick filter showed 6 tasks.
- `1280×900`: main content width capped at 920px; no horizontal overflow.
- Accessibility tree exposes named metric, filter, reset, and task-detail controls.

Visual-model inspection was unavailable because the configured model endpoint rejected image analysis. DOM geometry, accessibility tree, interaction, and overflow checks passed.
