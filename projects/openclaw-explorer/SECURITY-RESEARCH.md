# OpenClaw Security Mechanisms — Source Code Research Report

Research conducted on 2026-03-17 from the OpenClaw source code and documentation.

---

## 1. Trust Model Overview

OpenClaw follows a **one-user personal assistant** trust model. A single gateway serves one operator boundary. Authenticated callers are treated as trusted operators. The model (LLM) is **not** a trusted principal — security boundaries come from host/config trust, authentication, tool policy, sandboxing, and exec approvals.

Key trust assumptions:
- The host machine is within a trusted OS/admin boundary
- Anyone with access to `~/.openclaw` is a trusted operator
- Workspace `MEMORY.md` and `memory/*.md` are trusted local operator state
- Plugins are loaded in-process and treated as trusted code

**Source:** `SECURITY.md`

---

## 2. Trust Boundary Architecture (5 Layers)

The MITRE ATLAS-based threat model defines **5 trust boundaries**:

| Layer | Boundary | Purpose |
|-------|----------|---------|
| 1 | Channel Access | Device pairing, AllowFrom validation, token/password auth |
| 2 | Session Isolation | Session keys per agent:channel:peer, tool policies, logging |
| 3 | Tool Execution | Docker sandbox or host with exec-approvals, SSRF protection |
| 4 | External Content | URL fetching, email, webhooks with content wrapping |
| 5 | Supply Chain | ClawHub skill publishing, moderation, VirusTotal scanning |

**Source:** `docs/security/THREAT-MODEL-ATLAS.md`

---

## 3. Trust Boundary 1 — Channel Access Control

### 3.1 DM Policy

Three DM access modes:
- **open** — Any user can DM (flagged CRITICAL by audit)
- **allowlist** — Only allow-listed users
- **pairing** — Requires device pairing challenge

**Source:** `src/security/dm-policy-shared.ts`, `src/security/audit-channel.ts`

### 3.2 Device Pairing System

- 8-character challenge codes (alphanumeric, excluding confusable chars 0/O/1/I)
- 60-minute TTL for pairing requests with automatic expiration
- Max 3 pending requests per channel (DoS prevention)
- Account-scoped pairing with file-based allowlist stores
- Atomic JSON writes with file locks for race condition prevention
- Bootstrap tokens for device activation

**Source:** `src/pairing/pairing-store.ts`, `src/pairing/pairing-challenge.ts`, `src/pairing/setup-code.ts`

### 3.3 Device Auth Payloads

- Versioned payloads (v2, v3) with platform/device-family metadata
- Nonce inclusion for freshness verification
- Pipe-delimited format: `v3|deviceId|clientId|clientMode|role|scopes|timestamp|token|nonce|platform|deviceFamily`
- Role-based access: operator vs node roles determine permitted scopes

**Source:** `src/device-auth.ts`

### 3.4 Group Policies & Allowlists

- Group policy: open (CRITICAL) vs allowlist
- Compiled allowlists for O(1) lookups with wildcard support
- Multiple match sources: ID, name, tag, username, slug, localpart
- Mutable allowlist detection per platform (Discord username vs ID, Slack non-ID patterns, etc.)
- Mention gating in groups (bot requires explicit mention unless bypassed)
- Command gating with access group authorization

**Source:** `src/channels/allowlist-match.ts`, `src/channels/mention-gating.ts`, `src/channels/command-gating.ts`, `src/security/mutable-allowlist-detectors.ts`

---

## 4. Trust Boundary 2 — Session Isolation

### 4.1 Session Key Generation

Multi-level session keys with format: `agent:agentId:mainKey|channel|accountId|peerKind|peerId|threadId`

Four DM scoping levels via `dmScope`:
- **main** — All DMs in single session
- **per-peer** — Separate session per peer user
- **per-channel-peer** — Separate per channel + peer
- **per-account-channel-peer** — Maximum isolation

Identity link resolution maps cross-platform identities to canonical peer IDs. Thread session keys provide optional thread suffix for threaded conversations.

**Source:** `src/routing/session-key.ts`

### 4.2 Account Routing

Default account ID normalization and multi-account session isolation.

**Source:** `src/routing/account-id.ts`, `src/routing/account-lookup.ts`

---

## 5. Trust Boundary 3 — Tool Execution Security

### 5.1 Tool Policy System

- Allow/deny lists per agent sandbox
- Union semantics: `allow` + `alsoAllow` creates combined allowlist
- Wildcard support: `*` grants all tools unless deny list constrains
- Dangerous tools classified into two categories:
  - **HTTP-denied:** `sessions_spawn`, `sessions_send`, `cron`, `gateway`, `whatsapp_login`
  - **ACP-dangerous:** `exec`, `spawn`, `shell`, `sessions_spawn`, `sessions_send`, `gateway`, `fs_write`, `fs_delete`, `fs_move`, `apply_patch`

**Source:** `src/agents/sandbox-tool-policy.ts`, `src/security/dangerous-tools.ts`

### 5.2 Exec Approval System

- Companion app/node host guardrail for sandboxed agent command execution
- Approval context binding: canonical cwd, exact argv, env binding, pinned executable path
- File binding: best-effort binding of one concrete local file operand

Policy knobs:
- **Security:** deny | allowlist | full
- **Ask:** off | on-miss | always
- **Ask fallback:** deny | allowlist | full

Allowlist features:
- Case-insensitive glob patterns matching binary paths
- Per-entry metadata (UUID, last used timestamp/command/path)
- Auto-allow skill CLIs via `skills.bins` over Gateway RPC

**Source:** `src/infra/exec-approvals.ts`, `docs/tools/exec-approvals.md`

### 5.3 Safe Bins

Stdin-only stream filter binaries: `jq`, `cut`, `uniq`, `head`, `tail`, `tr`, `wc`
- Do NOT include interpreters/runtimes (python3, node, bash, etc.)
- File-oriented options denied (sort -o, jq -f, grep -f)
- Argument tokens treated as literal text (no globbing, no variable expansion)
- Default directories: /bin, /usr/bin only
- Shell chaining (&&, ||, ;) allowed when every segment satisfies allowlist
- Redirections unsupported in allowlist mode
- Command substitution rejected

**Source:** `docs/tools/exec-approvals.md`

### 5.4 Approval Flow

- Gateway broadcasts `exec.approval.requested` to operator clients
- Confirmation dialog shows: command+args, cwd, agent id, resolved executable path
- Actions: Allow once, Always allow (add to allowlist), Deny
- Approval forwarding to chat channels (Discord, Telegram)
- macOS IPC: Gateway → Node Service (WebSocket) → Mac App (Unix socket + token + HMAC + TTL)

**Source:** `docs/tools/exec-approvals.md`

---

## 6. Trust Boundary 4 — External Content Protection

### 6.1 SSRF Protection

Comprehensive SSRF prevention with DNS pinning:
- IPv4 special-use blocking (127.x, 10.x, 172.16-31.x, 192.168.x, 169.254.x, etc.)
- IPv6 special-use blocking (::1, fe80::/10, ff00::/8, embedded IPv4)
- Blocked hostnames: localhost, *.local, *.internal, metadata.google.internal
- Two-phase DNS validation (pre-DNS literal check + post-DNS resolution validation)
- DNS-pinned undici Agent/ProxyAgent for connection

Policy options: `allowPrivateNetwork`, `dangerouslyAllowPrivateNetwork`, `allowedHostnames[]`

**Source:** `src/infra/net/ssrf.ts`

### 6.2 Guarded Fetch

HTTP client with SSRF and redirect protection:
- **STRICT mode:** DNS pinning required, no env proxy
- **TRUSTED_ENV_PROXY mode:** Allows http_proxy env vars for operator-controlled URLs
- Max 3 redirects with visited URL tracking (loop detection)
- Cross-origin header stripping (safe headers whitelist only)
- Timeout & AbortSignal relay

**Source:** `src/infra/net/fetch-guard.ts`

### 6.3 External Content Wrapping (Prompt Injection Defense)

Defense against prompt injection from external sources (email, webhooks, API, browser):
- `detectSuspiciousPatterns()`: 12+ injection patterns (e.g., "ignore previous instructions", system message tags, rm -rf)
- `wrapExternalContent()`: Unique boundary markers with random hex IDs (randomBytes(8))
- Security warning header prepended to untrusted content
- Unicode homoglyph defense (25+ mappings for fullwidth/CJK/mathematical brackets)
- Invisible character stripping (ZWSP, ZWNJ, ZWJ, Word Joiner, BOM, Soft Hyphen)
- Content source classification: email | webhook | api | browser | web_search | web_fetch

**Source:** `src/security/external-content.ts`

### 6.4 ReDoS Prevention

Regular Expression Denial of Service prevention:
- Nested quantifier detection
- Pattern complexity analysis via tokenization
- LRU cache (max 256 entries) with pattern folding
- 2048-character bounded test window

**Source:** `src/security/safe-regex.ts`

---

## 7. Trust Boundary 5 — Supply Chain

ClawHub skill publishing moderation:
- GitHub account age verification
- Path sanitization
- File type validation
- 50MB bundle size limits
- Pattern-based moderation (regex for malware indicators, suspicious keywords, wallet/crypto, webhooks, URL shorteners)
- Planned: VirusTotal integration, community reporting, audit logging

Skill security scanning:
- Code safety scanning for skill scripts/plugins
- Scannable extensions: .js, .ts, .mjs, .cjs, .mts, .cts, .jsx, .tsx
- File scan cache (max 5000 entries) with mtime validation
- Max 500 files, 1MB per file limits

**Source:** `src/security/skill-scanner.ts`, `docs/security/THREAT-MODEL-ATLAS.md`

---

## 8. Gateway Authentication

### 8.1 Auth Modes

Multiple authentication methods:
- **Token** — Shared secret token
- **Password** — Password-based auth
- **Tailscale** — Verifies Tailscale user via x-forwarded-for, whois lookup, header consistency
- **Trusted Proxy** — Extracts user identity from proxy headers (Pomerium, Caddy, nginx+OAuth)
- **Device Token** — Per-device bootstrap tokens
- **Bootstrap Token** — One-time pairing tokens

**Source:** `src/gateway/auth.ts`, `docs/gateway/trusted-proxy-auth.md`

### 8.2 Rate Limiting

Per-IP rate limiting with sliding window:
- Default: 10 failures per 60 seconds
- 5-minute lockout after threshold
- Loopback addresses exempt (prevents CLI lockout)
- Separate scopes for shared-secret and device-token auth

**Source:** `src/gateway/auth-rate-limit.ts`

### 8.3 Timing-Safe Secret Comparison

SHA256 hashing both inputs, then `timingSafeEqual()` from Node crypto module. Prevents timing side-channel attacks.

**Source:** `src/security/secret-equal.ts`

### 8.4 Origin Check

Browser origin validation (CORS-like):
- Allowlist-based Origin header checking
- Host-header fallback
- Loopback exception
- Prevents XSS/CSRF

**Source:** `src/gateway/origin-check.ts`

### 8.5 Role-Based Access Control

Two gateway roles:
- **Operator** (full access): 5 scopes — admin, read, write, approvals, pairing
- **Node** (restricted): Fixed set of node-only methods (invoke, events, pending work, canvas)
- Default-deny for unclassified methods
- Scope hierarchy: Admin ⊃ (Read + Write + Approvals + Pairing)

**Source:** `src/gateway/role-policy.ts`, `src/gateway/method-scopes.ts`

---

## 9. Security Audit CLI

`openclaw security audit` command with options:
- `--deep` — Extended checks
- `--password` / `--token` — Auth for remote audit
- `--fix` — Auto-remediate issues
- `--json` — Machine-readable output

Audit checks include:
- DM/group policy (open = critical)
- Small model risk (≤300B params without sandbox)
- Dangerous config flags (dangerous*/dangerously* prefixes)
- Webhook ingress without session keys
- Permissive tool policies overriding "minimal" profile
- Mutable allowlist detection (names vs stable IDs)
- Gateway auth mode "none"
- Sandbox browser Docker network modes
- Stale Docker containers
- Unpinned npm plugin/hook installs
- File permission issues (POSIX + Windows ACL)
- Secrets in config (env ref patterns)
- Synced folder detection (iCloud, Dropbox, OneDrive, Google Drive)

`--fix` auto-remediates:
- Flips groupPolicy="open" to "allowlist"
- Sets logging.redactSensitive to "tools"
- Tightens permissions on state/config files

**Source:** `src/security/audit.ts`, `src/security/audit-extra.sync.ts`, `src/security/audit-fs.ts`, `src/security/audit-channel.ts`, `docs/cli/security.md`

---

## 10. Dangerous Configuration Flags

Monitored flags (all default to safe values):
- `gateway.controlUi.allowInsecureAuth=true`
- `gateway.controlUi.dangerouslyAllowHostHeaderOriginFallback=true`
- `gateway.controlUi.dangerouslyDisableDeviceAuth=true`
- `hooks.gmail.allowUnsafeExternalContent=true`
- `hooks.mappings[].allowUnsafeExternalContent=true`
- `tools.exec.applyPatch.workspaceOnly=false`

**Source:** `src/security/dangerous-config-flags.ts`

---

## 11. Formal Verification (TLA+/TLC)

Machine-checked security policy enforcement using TLA+/TLC model checking:

| Model | What It Checks |
|-------|---------------|
| Gateway exposure | Open gateway misconfiguration |
| Nodes.run pipeline | Highest-risk exec capability |
| Pairing store | DM gating correctness |
| Ingress gating | Mentions + control-command bypass |
| Routing isolation | Session key isolation |

Extended v1++ models cover:
- Pairing store concurrency/idempotency
- Ingress trace correlation/idempotency
- Routing dmScope precedence + identityLinks

Models are published separately at GitHub. Important caveat: these model TypeScript behavior, not replace it. Drift is possible.

**Source:** `docs/security/formal-verification.md`

---

## 12. MITRE ATLAS Threat Model

Comprehensive threat analysis mapping 21 threats to 9 MITRE ATLAS techniques:

**Critical risks (P0):**
- T-EXEC-001: Direct Prompt Injection
- T-PERSIST-001: Malicious Skill Installation
- T-EXFIL-003: Credential Harvesting
- T-IMPACT-001: Unauthorized Command Execution

**High risks (P1):**
- T-EXEC-002: Indirect Prompt Injection
- T-EXEC-003: Tool Argument Injection
- T-EXEC-004: Exec Approval Bypass
- T-PERSIST-002: Skill Update Poisoning
- T-EVADE-001: Moderation Pattern Bypass
- T-EXFIL-001: Data Theft via web_fetch
- T-ACCESS-003: Token Theft
- T-IMPACT-002: Resource Exhaustion (DoS)

**Documented attack chains:**
1. Skill-Based Data Theft: malicious skill → moderation bypass → credential harvesting
2. Prompt Injection to RCE: direct injection → exec approval bypass → unauthorized commands
3. Indirect Injection via Fetched Content: poisoned URL → data exfiltration

**Source:** `docs/security/THREAT-MODEL-ATLAS.md`

---

## 13. Secrets Management

### 13.1 Secrets Audit

Comprehensive plaintext secret detection and SecretRef validation:
- Scans .env files, openclaw.json config, auth store profiles, models.json
- Detects plaintext secrets, unresolved SecretRef references, shadowed refs, legacy residue
- Sensitive header detection: authorization, proxy-authorization, x-api-key, etc.
- Concurrency-limited resolution validation (8 concurrent)

**Source:** `src/secrets/audit.ts`

### 13.2 File Permission Auditing

- POSIX mode bit checking (world/group writable/readable)
- Windows ACL parsing with icacls (non-English SID support)
- Symlink detection and resolution
- Trusted vs world vs group principal classification
- Auto-remediation via chmod/icacls commands

**Source:** `src/security/audit-fs.ts`, `src/security/windows-acl.ts`

---

## 14. Additional Security Mechanisms

### 14.1 Browser CSRF Protection

Browser mutation guard middleware:
- Mutating method detection: POST, PUT, PATCH, DELETE
- Origin/Referer validation (rejects cross-site requests)
- Enforces loopback origin/referer
- Non-browser clients (curl/undici) bypass due to lack of headers

**Source:** `src/browser/csrf.ts`

### 14.2 Browser Navigation Guard

Pre-navigation DNS resolution with SSRF policy:
- Safe protocols: http, https only
- Allowed non-network URLs: about:blank (bootstrap only)
- Post-navigation validation for redirect chains
- Strict mode: env proxy detection blocks private navigation

**Source:** `src/browser/navigation-guard.ts`

### 14.3 Browser Control Auth

Browser HTTP API authentication protecting the CDP control socket:
- Token or password protection
- Auto-generation if not configured
- Gateway auth integration

**Source:** `src/browser/control-auth.ts`

### 14.4 Host Environment Security

Blocked environment variables from sandbox execution:
- LD_PRELOAD, LD_LIBRARY_PATH, DYLD_* (shared library injection)
- PYTHONPATH, RUBYLIB, PERLLIB (interpreter library paths)
- NODE_OPTIONS, NODE_EXTRA_CA_CERTS
- AWS_*, AZURE_*, GCP_* credentials
- *_PROXY variables (proxy injection)
- SSL_CERT_FILE, SSL_KEY_FILE
- PATH overrides blocked (part of command resolution boundary)

Shell wrapper allowlist (safe to pass through):
- TERM, LANG, LC_* (locale/terminal)
- COLORTERM, NO_COLOR, FORCE_COLOR (UI preferences)

**Source:** `src/infra/host-env-security.ts`, `src/infra/host-env-security-policy.json`

### 14.5 Command Obfuscation Detection

Detects encoded/obfuscated execution attempts:
- Strips 95 invisible Unicode codepoints
- Detects base64 piped to shell, hex/xxd piped to shell
- Detects perl/python -e piped to shell
- Variable expansion bypass detection
- Max command length: 10,000 chars

**Source:** `src/infra/exec-obfuscation-detect.ts`

### 14.6 Path Guards & Symlink Protection

- Windows path normalization (\\?\ prefix, UNC paths)
- Cross-platform path containment check
- Resolves .. traversal safely
- Hardlink detection (stat.nlink > 1)
- ELOOP/EINVAL/ENOTSUP symlink error detection

**Source:** `src/infra/path-guards.ts`, `src/infra/hardlink-guards.ts`

### 14.7 Trusted Proxy Auth

For deployments behind identity-aware reverse proxies:
- Supports Pomerium, Caddy+OAuth, nginx+oauth2-proxy, Traefik
- Extracts user identity from configured headers
- Validates request source from trusted proxy IPs
- allowUsers list for additional access control

**Source:** `docs/gateway/trusted-proxy-auth.md`

### 14.3 Connect Policy

Gateway WebSocket connection policy:
- Device identity enforcement (unless explicitly bypassed)
- Control UI hardening flags
- Auth method prioritization: shared auth → trusted-proxy → device tokens
- Scope-based bypass for operators with shared auth

**Source:** `src/gateway/server/ws-connection/connect-policy.ts`

---

## 15. Key File Reference

| Mechanism | File Path |
|-----------|-----------|
| Main security audit | `src/security/audit.ts` |
| Dangerous tools | `src/security/dangerous-tools.ts` |
| Dangerous config flags | `src/security/dangerous-config-flags.ts` |
| External content wrapping | `src/security/external-content.ts` |
| SSRF protection | `src/infra/net/ssrf.ts` |
| Guarded fetch | `src/infra/net/fetch-guard.ts` |
| ReDoS prevention | `src/security/safe-regex.ts` |
| Exec approvals | `src/infra/exec-approvals.ts` |
| DM policy | `src/security/dm-policy-shared.ts` |
| Channel audit | `src/security/audit-channel.ts` |
| Mutable allowlist detection | `src/security/mutable-allowlist-detectors.ts` |
| Session key generation | `src/routing/session-key.ts` |
| Gateway auth | `src/gateway/auth.ts` |
| Auth rate limiting | `src/gateway/auth-rate-limit.ts` |
| Role-based access | `src/gateway/role-policy.ts` |
| Method scopes | `src/gateway/method-scopes.ts` |
| Origin check | `src/gateway/origin-check.ts` |
| Timing-safe comparison | `src/security/secret-equal.ts` |
| File permissions audit | `src/security/audit-fs.ts` |
| Windows ACL | `src/security/windows-acl.ts` |
| Skill scanner | `src/security/skill-scanner.ts` |
| Path safety | `src/security/scan-paths.ts` |
| Secrets audit | `src/secrets/audit.ts` |
| Browser control auth | `src/browser/control-auth.ts` |
| Pairing store | `src/pairing/pairing-store.ts` |
| Device auth | `src/device-auth.ts` |
| Sandbox tool policy | `src/agents/sandbox-tool-policy.ts` |
| Threat model | `docs/security/THREAT-MODEL-ATLAS.md` |
| Formal verification | `docs/security/formal-verification.md` |
| Exec approvals docs | `docs/tools/exec-approvals.md` |
| Gateway security guide | `docs/gateway/security/index.md` |
| Trusted proxy auth | `docs/gateway/trusted-proxy-auth.md` |
| Security CLI | `docs/cli/security.md` |
