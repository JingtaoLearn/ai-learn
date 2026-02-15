# OpenClaw Security Guide

## Maximum Permissions Configuration

This OpenClaw instance is configured with **maximum permissions** to provide full functionality. This document explains the security implications and best practices.

## Current Configuration Overview

### Enabled Tools

| Tool Category | Tools | Risk Level | Description |
|---------------|-------|------------|-------------|
| **File System** | `read`, `write`, `edit`, `apply_patch` | 🔴 High | Full file system access |
| **Execution** | `exec`, `process` | 🔴 Critical | Command and process execution |
| **Web** | `web`, `web_fetch`, `web_search` | 🟡 Medium | Web access and search (prompt injection risk) |
| **Browser** | `browser` | 🔴 High | Browser control with logged-in sessions |
| **Utility** | `memory`, `cron` | 🟡 Medium | Memory search and scheduled tasks |

### Security Settings

```json
{
  "sandbox": {
    "mode": "off",              // ⚠️ No sandboxing
    "workspaceAccess": "rw"     // Full read/write access
  },
  "elevated": {
    "enabled": true,            // ⚠️ Privileged commands allowed
    "allowFrom": {              // Configure trusted users here
      "whatsapp": [],
      "telegram": [],
      "discord": []
    }
  },
  "gateway": {
    "bind": "loopback"          // ✅ Localhost only (recommended)
  }
}
```

## Security Risks

### 1. Remote Code Execution (RCE)

**Risk:** `exec` and `process` tools can execute arbitrary commands on the host system.

**Mitigation:**
- Keep `gateway.bind: "loopback"` to restrict to localhost only
- Use strong authentication tokens
- Only add trusted users to `elevated.allowFrom`
- Monitor logs regularly: `journalctl --user -u openclaw-gateway`

### 2. File System Access

**Risk:** Can read/write any file the user has permissions for, including sensitive data.

**Mitigation:**
- Run OpenClaw as a dedicated user with limited permissions
- Use file system ACLs to restrict sensitive directories
- Enable `logging.redactSensitive` to prevent credential leaks in logs

### 3. Prompt Injection

**Risk:** `web_fetch` and `web_search` can introduce untrusted content that manipulates the agent.

**Mitigation:**
- Use strong models (Opus 4.6+) that are more resistant to prompt injection
- Be cautious when fetching content from unknown sources
- Consider disabling these tools if not needed

### 4. Browser Session Hijacking

**Risk:** Browser control accesses logged-in sessions and personal data.

**Mitigation:**
- Use a dedicated browser profile for OpenClaw (not your daily driver)
- Disable browser sync and password managers in the agent profile
- Consider keeping browser control disabled: add `"browser"` to `tools.deny`

## Elevated Permissions Configuration

### Allowing Specific Users

To grant elevated permissions to specific users, update `~/.openclaw/openclaw.json`:

```json
{
  "tools": {
    "elevated": {
      "enabled": true,
      "allowFrom": {
        "whatsapp": ["+1234567890"],      // Your trusted phone number
        "telegram": ["123456789"],        // Your Telegram user ID
        "discord": ["your-username"]      // Your Discord username
      }
    }
  }
}
```

### How to Find Your IDs

**WhatsApp:**
- Your phone number in international format (e.g., `+1234567890`)

**Telegram:**
- Use `openclaw channels list` to see your user ID
- Or check logs: `journalctl --user -u openclaw-gateway | grep "from user"`

**Discord:**
- Use your Discord username (without discriminator in new format)
- Or use your Discord user ID (numeric)

## Recommended Hardening Steps

### 1. Secure File Permissions

```bash
# Restrict OpenClaw directory
chmod 700 ~/.openclaw

# Secure configuration file
chmod 600 ~/.openclaw/openclaw.json

# Secure credentials
chmod 600 ~/.openclaw/credentials/*.json
chmod 600 ~/.openclaw/agents/*/agent/auth-profiles.json
```

### 2. Use Strong Authentication

```bash
# Generate strong random token (64 characters)
openssl rand -base64 48

# Add to /etc/environment
echo "OPENCLAW_GATEWAY_TOKEN=<generated-token>" | sudo tee -a /etc/environment
```

### 3. Enable Audit Logging

```bash
# Run security audit
openclaw security audit --deep

# Check for issues
openclaw doctor --fix
```

### 4. Network Binding

**Current:** `gateway.bind: "loopback"` - Localhost only (✅ Recommended)

**Alternatives:**
- `"loopback"` - Most secure, local access only
- `"lan"` - LAN access (requires strong auth + firewall)
- `"tailnet"` - Tailscale private network (good for remote access)
- ❌ Never use `"0.0.0.0"` without authentication

### 5. Regular Updates

```bash
# Update OpenClaw
npm update -g openclaw

# Regenerate gateway service
openclaw gateway install

# Restart services
systemctl --user restart openclaw-gateway
```

## Monitoring and Incident Response

### Monitor Logs

```bash
# Watch gateway logs
journalctl --user -u openclaw-gateway -f

# Check daily logs
tail -f /tmp/openclaw/openclaw-$(date +%Y-%m-%d).log
```

### Signs of Compromise

- Unexpected file modifications
- Unknown commands in logs
- Unusual network activity
- Failed authentication attempts
- Unknown users in channel logs

### Incident Response Checklist

1. **Contain immediately:**
   ```bash
   systemctl --user stop openclaw-gateway
   ```

2. **Rotate all credentials:**
   - Generate new `OPENCLAW_GATEWAY_TOKEN`
   - Update `S_LITELLM_API_KEY`
   - Regenerate channel credentials (WhatsApp, Telegram, etc.)

3. **Audit logs:**
   ```bash
   # Review recent activity
   journalctl --user -u openclaw-gateway --since "1 hour ago"

   # Check transcripts
   ls -la ~/.openclaw/agents/*/sessions/
   ```

4. **Review configuration:**
   ```bash
   openclaw security audit --deep
   cat ~/.openclaw/openclaw.json
   ```

5. **Restore from backup** if necessary

## Alternative Configurations

### Read-Only Agent (Lower Risk)

```json
{
  "tools": {
    "allow": ["read", "web_search"],
    "deny": ["exec", "write", "edit", "browser", "process"]
  },
  "sandbox": {
    "mode": "all",
    "workspaceAccess": "ro"
  }
}
```

### Sandboxed Agent (Balanced)

```json
{
  "tools": {
    "allow": ["exec", "read", "write", "edit"],
    "deny": ["browser"]
  },
  "sandbox": {
    "mode": "tools",
    "workspaceAccess": "rw"
  },
  "elevated": {
    "enabled": false
  }
}
```

### Public Agent (Minimal Risk)

```json
{
  "tools": {
    "allow": ["whatsapp", "telegram", "discord"],
    "deny": ["exec", "read", "write", "browser"]
  },
  "sandbox": {
    "mode": "all",
    "workspaceAccess": "none"
  }
}
```

## Questions and Support

- **Official Docs:** https://docs.openclaw.ai
- **Security Issues:** security@openclaw.ai
- **GitHub Discussions:** https://github.com/openclaw/openclaw/discussions

## Last Updated

2026-02-15 - Updated Discord security to reflect allowlist group policy and DM pairing
