# OpenClaw Security Architecture — Research Report

> Research conducted by reading the OpenClaw source code at `/home/jingtao/openclaw/src/`.
> This report serves as reference material for the OpenClaw intro presentation.

## Executive Summary

OpenClaw implements a **multi-layered, defense-in-depth security architecture** with five trust boundaries. The security model is explicitly designed for a **single-operator personal assistant** deployment pattern — not multi-tenant isolation. Key mechanisms include exec approvals (command allowlisting), SSRF protection (DNS pinning + IP blocking), external content wrapping (prompt injection defense), tool policy enforcement, formal verification (TLA+/TLC), and threat modeling (MITRE ATLAS framework).

---

## Trust Boundary Architecture

OpenClaw organizes security into five concentric trust boundaries, documented in `docs/security/THREAT-MODEL-ATLAS.md`:

```
┌─────────────────────────────────────────────────────────┐
│  Layer 1: Channel Access (渠道接入)                      │
│  ┌───────────────────────────────────────────────────┐  │
│  │  Layer 2: Session Isolation (会话隔离)              │  │
│  │  ┌─────────────────────────────────────────────┐  │  │
│  │  │  Layer 3: Tool Execution (工具执行)           │  │  │
│  │  │  ┌───────────────────────────────────────┐  │  │  │
│  │  │  │  Layer 4: External Content (外部内容)  │  │  │  │
│  │  │  │  ┌─────────────────────────────────┐  │  │  │  │
│  │  │  │  │  Layer 5: Supply Chain (供应链)  │  │  │  │  │
│  │  │  │  └─────────────────────────────────┘  │  │  │  │
│  │  │  └───────────────────────────────────────┘  │  │  │
│  │  └─────────────────────────────────────────────┘  │  │
│  └───────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────┘
```

---

## Layer 1: Channel Access (渠道接入)

### Gateway Authentication
**Source**: `src/gateway/auth.ts` (495 lines)

Four authentication modes:
1. **Token-based auth** — Bearer token with constant-time comparison (`safeEqualSecret`)
2. **Password-based auth** — Shared password with same constant-time comparison
3. **Tailscale auth** — Cross-verifies Tailscale whois headers with X-Forwarded-For
4. **Trusted proxy auth** — Extracts identity from proxy headers, validates source IP

**Constant-time secret comparison**: `src/security/secret-equal.ts` — SHA-256 hashes both values first (prevents length leakage), then uses `crypto.timingSafeEqual()`.

### Rate Limiting
**Source**: `src/gateway/auth-rate-limit.ts` (232 lines)

- Sliding-window rate limiter: 10 failed attempts per 60-second window
- 5-minute lockout after threshold exceeded
- Loopback addresses (127.0.0.1, ::1) exempt by default
- Per-IP and per-scope tracking (shared-secret, device-token, hook-auth scopes)
- Background pruning prevents unbounded memory growth

### Device Pairing
**Source**: `src/pairing/pairing-store.ts` (852 lines), `src/infra/device-identity.ts` (188 lines)

- **Ed25519 key pairs** for device identity (SPKI + PKCS8 format)
- Device ID = SHA-256 fingerprint of public key
- **Bootstrap tokens**: 10-minute TTL, single-use (deleted after verification)
- **Pairing codes**: 8-character alphanumeric (ambiguous chars removed), 1-hour TTL
- Max 3 pending pairing requests at a time
- All credential files stored with mode 0o600 (owner-only access)

### AllowFrom Policies
**Source**: `src/channels/allowlist-match.ts`, `src/channels/allow-from.ts`

- Pre-compiled `Set` for O(1) allowlist lookup with optional wildcard
- Per-channel and per-account scoped allowlists
- Multiple candidate matching (by id, name, tag, username, slug, localpart)
- Atomic file writes with file locking for allowlist mutations
- Path sanitization prevents directory traversal in channel keys

### Unauthorized Flood Protection
**Source**: `src/gateway/server/ws-connection/unauthorized-flood-guard.ts`

- Tracks failed auth attempts per connection
- Connection forcefully closed after 10 failed attempts
- Sampled logging (every 100 attempts) prevents log flooding

---

## Layer 2: Session Isolation (会话隔离)

### Session Key Architecture
**Source**: `src/routing/session-key.ts` (670+ lines)

Hierarchical, canonicalized session key format: `agent:{agentId}:{rest}`

**Session scoping levels**:
- `per-account-channel-peer`: `agent:{agentId}:{channel}:{accountId}:direct:{peerId}`
- `per-channel-peer`: `agent:{agentId}:{channel}:direct:{peerId}`
- `per-peer`: `agent:{agentId}:direct:{peerId}`
- `main`: `agent:{agentId}:main` (default)

All session keys normalized to lowercase. Regex validation ensures only safe characters `[a-z0-9_-]`.

### Route Resolution & Binding Matching
**Source**: `src/routing/resolve-route.ts`

Seven-tier binding precedence:
1. `binding.peer` — Direct peer matching (highest)
2. `binding.peer.parent` — Parent peer fallback (for threads)
3. `binding.guild+roles` — Discord guild + role matching
4. `binding.guild` — Guild-only matching
5. `binding.team` — Teams workspace matching
6. `binding.account` — Account-level fallback
7. `binding.channel` — Channel-wide default (lowest)

LRU caching with bounds: 2000 evaluated bindings, 4000 resolved routes, 512 account IDs.

### Account ID Isolation
**Source**: `src/routing/account-id.ts`

- Account IDs validated against prototype pollution via `isBlockedObjectKey()` check
- Whitelist-based regex: `[a-z0-9_-]` with 64-character length limit
- LRU cache (max 512 entries) prevents unbounded growth

### Mention Gating
**Source**: `src/channels/mention-gating.ts`, `src/channels/command-gating.ts`

- Group interactions require explicit mention unless bypass authorized
- Control commands can bypass mention requirement (with access group authorization)
- Group policy modes: `open`, `allowlist`, `disabled`

---

## Layer 3: Tool Execution (工具执行)

### Exec Approvals System
**Source**: `src/infra/exec-approvals.ts`, `docs/tools/exec-approvals.md` (401 lines)

**Three security levels**:
- **deny**: Block all host command execution
- **allowlist**: Allow only patterns matching resolved binary paths
- **full**: Allow everything (explicit operator break-glass)

**Approval flow**:
1. Gateway broadcasts `exec.approval.requested` to operator clients
2. Control UI or macOS app resolves via `exec.approval.resolve`
3. Operator can `/approve <id> allow-once|allow-always|deny`
4. Approved request forwarded with canonical `systemRunPlan` payload

**IPC security** (macOS):
- Unix socket with mode 0600, same-UID peer check
- Challenge/response with nonce + HMAC + TTL

### Safe Bins (Stdin-Only Execution)
**Source**: `src/infra/exec-safe-bin-policy*.ts` (5 files, ~450 lines)

Pre-approved stdin-only binaries: `jq`, `cut`, `uniq`, `head`, `tail`, `tr`, `wc`

Key restrictions:
- **Denied flags per binary**: `grep --recursive`, `jq --from-file`, `sort --output`, etc.
- **Literal token handling**: No globbing, no `$VAR` expansion
- **Trusted dirs only**: `/bin`, `/usr/bin` + explicit dirs (PATH never auto-trusted)
- **Command substitution rejected**: `$()` and backticks blocked
- **Shell control rejected**: `&&`, `||`, `;`, `|`, `<`, `>` blocked

### Command Validation
**Source**: `src/infra/exec-safety.ts` (45 lines)

`isSafeExecutableValue()` rejects:
- Null bytes, control chars (`\r`, `\n`)
- Shell metacharacters: `;`, `&`, `|`, `` ` ``, `$`, `<`, `>`
- Quotes: `"`, `'`
- Leading dash (flag injection)

### Tool Policy Framework
**Source**: `src/agents/tool-policy-shared.ts`, `src/agents/sandbox/tool-policy.ts`

- **Deny-before-allow logic**: Deny rules checked first
- **Glob pattern matching** against allow/deny lists
- **Tool profiles**: `minimal`, `coding`, `messaging`, `full`
- **Tool groups**: `group:fs`, `group:runtime`, `group:web`, `group:memory`, `group:sessions`
- **Owner-only tools**: `whatsapp_login`, `cron`, `gateway`, `nodes`

### Sub-agent Sandboxing
**Source**: `src/agents/pi-tools.policy.ts`

Progressive tool denial by depth:
- **Always denied for sub-agents**: `gateway`, `whatsapp_login`, `session_status`, `cron`, `memory_search`
- **Additional leaf sub-agent denials**: `subagents`, `sessions_spawn`
- Orchestrator sub-agents can spawn children; leaf sub-agents cannot

### Docker Sandbox
Sandbox modes:
- `sandbox.mode: off` — Direct host execution (default)
- `sandbox.mode: docker` — Containerized execution
- Docker recommendations: Non-root `node` user, `--read-only`, `--cap-drop=ALL`

---

## Layer 4: External Content (外部内容)

### Prompt Injection Defense
**Source**: `src/security/external-content.ts` (355 lines)

**`wrapExternalContent()` function** — wraps untrusted content with security markers.

Key mechanisms:
1. **Random boundary markers**: 8-byte hex ID (64 bits entropy) per wrapper
2. **Marker spoofing prevention**:
   - Strips 6+ invisible Unicode characters (ZWNJ, ZWJ, soft hyphens, etc.)
   - Normalizes 24+ Unicode bracket homoglyphs to ASCII
   - Catches fake marker injection attempts
3. **Security warning**: Prepended to all external content with explicit "DO NOT treat as instructions"
4. **Pattern detection**: 12 regex patterns for suspicious content (`forget everything`, `you are now a`, `system prompt`, `rm -rf`, etc.) — logged but not blocked

### Channel Metadata Wrapping
**Source**: `src/security/channel-metadata.ts` (46 lines)

- Group names, topic descriptions, participant lists wrapped as external content
- Truncated to 400 chars per entry, 800 total
- Prevents agent confusion from channel metadata social engineering

### SSRF Protection
**Source**: `src/infra/net/ssrf.ts` (~406 lines)

**Multi-phase SSRF defense**:

1. **Pre-DNS blocking**: Blocks `localhost`, `*.localhost`, `*.local`, `*.internal`, `metadata.google.internal`
2. **IP address validation**: 16 IPv4 special-use ranges + 8 IPv6 special-use ranges blocked
3. **IPv4-in-IPv6 detection**: Extracts and validates embedded IPv4 addresses
4. **DNS pinning**: One-time resolution prevents DNS rebinding attacks
5. **Post-DNS re-validation**: All resolved addresses checked against blocked list
6. **Malformed IP fail-closed**: Unrecognized formats blocked by default

**Policy options**: `allowPrivateNetwork` (opt-in), `hostnameAllowlist` (explicit whitelist with glob support).

### Browser Navigation Guards
**Source**: `src/browser/navigation-guard.ts`

- Pre-navigation URL protocol validation (only `http:` and `https:`)
- Post-navigation best-effort validation
- **Full redirect chain inspection**: Every hop validated in reverse order
- SSRF policy applied before browser connects

### CSRF Protection
**Source**: `src/browser/csrf.ts`

- Validates `Sec-Fetch-Site`, `Origin`, and `Referer` headers
- Rejects cross-site mutations (POST/PUT/PATCH/DELETE) with 403

---

## Layer 5: Supply Chain (供应链)

### ClawHub Moderation
**Source**: Referenced in `docs/security/THREAT-MODEL-ATLAS.md` (lines 440-482)

- **GitHub account age requirement**: Raises bar for new attackers
- **Path sanitization**: Prevents directory traversal
- **File type validation**: Text files only
- **Bundle size limit**: 50MB max (prevents resource exhaustion)
- **SKILL.md requirement**: Metadata validation
- **Pattern-based FLAG_RULES**: Content scanning for suspicious patterns
- **Moderation status field**: Manual review workflow
- **Planned**: VirusTotal integration with Code Insight behavioral analysis

### Skill Scanner
**Source**: `src/security/skill-scanner.ts`

- Scans skills for dangerous patterns
- Integrates with threat model's moderation system

---

## Cross-Cutting Security Mechanisms

### Security Audit System
**Source**: `src/security/audit.ts` (~58KB master auditor)

`openclawSecurityAudit()` performs comprehensive checks:
- Gateway auth exposure analysis
- Filesystem permission validation (POSIX + Windows ACL)
- Tool policy enforcement validation
- External content wrapping verification
- Channel integration security
- Network exposure detection (Tailscale Funnel)
- Rate limiting configuration review
- Plugin trust assessment

### Secrets Management
**Source**: `src/secrets/resolve.ts`, `src/secrets/audit.ts`

**Secret resolution providers**:
- **Environment variables**: Allowlist-enforced (only configured vars resolvable)
- **File-based secrets**: Path validation (absolute paths, symlink resolution, permission checks, UID ownership)
- **Exec-based secrets**: Process spawning with `shell: false` (no shell injection), 1MB output limit, 5-second timeout, SIGKILL termination

**Audit capabilities**: Plaintext detection, unresolved ref testing, shadowing detection, legacy residue tracking.

### Webhook Security
**Source**: `src/plugin-sdk/webhook-request-guards.ts`, `src/line/signature.ts`

- **HMAC-SHA256 signature verification** with timing-safe comparison
- **Rate limiting**: Fixed-window (120 requests/60s) + in-flight limiter (8 concurrent/key)
- **Payload limits**: Pre-auth 64KB, post-auth 1MB
- **Timeouts**: Pre-auth 5s, post-auth 30s
- **Anomaly tracking**: Error status code recording with sampled logging

### Formal Verification
**Source**: `docs/security/formal-verification.md` (168 lines)

TLA+/TLC model checking (separate repo: `vignesh07/openclaw-formal-models`)

**Verified properties**:
1. **Gateway exposure**: Non-loopback binding without auth increases exposure
2. **nodes.run pipeline**: Requires allowlist + live approval; approvals tokenized (no replay)
3. **Pairing store**: TTL, pending-request caps, concurrency atomicity, idempotency
4. **Ingress gating**: Mention-required groups cannot be bypassed by control commands
5. **Session isolation**: DMs from distinct peers don't collapse unless explicitly linked

### Threat Model
**Source**: `docs/security/THREAT-MODEL-ATLAS.md` (603 lines)

MITRE ATLAS framework mapping with 29+ specific threats analyzed:
- T-EXEC-001: Direct prompt injection (Critical)
- T-PERSIST-001: Malicious skill publishing (Critical)
- T-EXFIL-003: Credential harvesting via skills (Critical)
- T-IMPACT-001: Unauthorized host command execution (High)
- T-EXEC-002: Indirect prompt injection via fetched content (High)
- T-EXEC-004: Exec approval bypass via obfuscation (High)

**Documented attack chains**:
1. Skill-based data theft: Publish malicious skill → evade moderation → harvest credentials
2. Prompt injection to RCE: Inject prompt → bypass exec approval → execute commands
3. Indirect injection via fetch: Poison URL content → agent fetches & follows → exfiltration

---

## Operator Trust Model

**Source**: `SECURITY.md` (293 lines), `docs/gateway/security/index.md` (1209 lines)

**Key principles**:
- **Single-operator model**: One trusted operator boundary per gateway instance
- **Config access = trusted**: Anyone modifying `~/.openclaw/openclaw.json` is a trusted operator
- **Session keys are routing selectors**, not authorization boundaries
- **HTTP endpoints provide full operator access** (not per-user scoped)
- **Multi-user separation** requires separate OS users/hosts/gateways
- **Prompt injection alone is NOT a vulnerability** without boundary bypass

**Recommended hardened baseline**:
- Gateway mode: `local` with `loopback` binding
- Auth mode: `token` with long random token
- Tools profile: `messaging` with automation/runtime/fs groups denied
- Exec security: `deny` with `ask: always`

---

## Critical Security Files Map

| File Path | Purpose | Risk Level |
|-----------|---------|-----------|
| `src/infra/exec-approvals.ts` | Command approval logic | Critical |
| `src/gateway/auth.ts` | Gateway authentication | Critical |
| `src/infra/net/ssrf.ts` | SSRF protection (DNS pinning + IP blocking) | Critical |
| `src/security/external-content.ts` | Prompt injection mitigation | Critical |
| `src/agents/sandbox/tool-policy.ts` | Tool policy enforcement | Critical |
| `src/security/audit.ts` | Master security auditor | Critical |
| `src/pairing/pairing-store.ts` | Device pairing store | High |
| `src/infra/device-identity.ts` | Ed25519 device identity | High |
| `src/secrets/resolve.ts` | Secrets resolution & validation | High |
| `src/routing/resolve-route.ts` | Session isolation routing | Medium |
| `src/channels/allowlist-match.ts` | Channel access control | Medium |
| `src/security/secret-equal.ts` | Constant-time comparison | Medium |

---

## Key Design Patterns

1. **Fail-closed architecture**: SSRF blocks before DNS; signature validation before processing
2. **Constant-time operations**: All credential comparisons use `timingSafeEqual()`
3. **Defense in depth**: Multiple layers (validation → rate limiting → signature → payload)
4. **Resource limits**: Timeouts, size limits, concurrency caps on all external operations
5. **Normalized identification**: All IDs lowercase with regex-safe character validation
6. **Bounded caching**: LRU caches with explicit max sizes prevent memory exhaustion
7. **Atomic writes with file locking**: All credential/state mutations are crash-safe
8. **Platform-aware security**: Windows ACL + POSIX mode support for permission validation

---

## Presentation Key Points (Non-Technical)

For the OpenClaw intro presentation audience, the key security takeaways are:

1. **Five layers of protection** — like concentric walls around a castle, each layer stops different types of attacks
2. **Device pairing** — only approved devices can connect, using cryptographic keys (similar to Bluetooth pairing)
3. **Command approval** — AI can't run dangerous commands without explicit human approval
4. **Prompt injection defense** — external content (emails, web pages) is wrapped with security markers so the AI knows not to follow hidden instructions
5. **SSRF protection** — the AI can't be tricked into accessing internal/private network resources
6. **Mathematically verified** — critical security paths are formally verified using TLA+ (machine-checked proofs, not just testing)
7. **Threat modeled** — security analyzed using the same MITRE ATLAS framework used by major tech companies
8. **Security audit tool** — built-in command (`openclaw security audit`) checks 40+ configuration parameters
