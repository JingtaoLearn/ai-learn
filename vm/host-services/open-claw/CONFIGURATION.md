# OpenClaw Configuration Reference

Detailed explanation of `openclaw.json` settings and environment variables.

## Configuration File Location

The configuration file is stored at `~/.openclaw/openclaw.json` and symlinked to this repository for version control:

```
~/.openclaw/openclaw.json ←→ vm/host-services/open-claw/openclaw.json
```

Edits in either location are reflected in both.

## Maximum Permissions Configuration

OpenClaw is configured with **maximum permissions** for full system access. This is intentional for development and learning environments.

### Tools Enabled

All available tools are enabled in the configuration:

| Tool | Description |
|------|-------------|
| `exec` | Execute shell commands |
| `process` | Process management and control |
| `read` | Read files from filesystem |
| `write` | Write files to filesystem |
| `edit` | Edit existing files |
| `apply_patch` | Apply patches to files |
| `browser` | Browser automation (requires separate setup) |
| `web` | Web access |
| `web_fetch` | Fetch web content |
| `web_search` | Web search capability |
| `memory` | Memory and embedding search |
| `cron` | Scheduled task execution |

### Security Settings

```json
{
  "sandbox": {
    "mode": "off"
  },
  "elevated": {
    "enabled": true
  },
  "gateway": {
    "bind": "loopback"
  },
  "logging": {
    "redactSensitive": "tools"
  },
  "discovery": {
    "mdns": {
      "mode": "minimal"
    }
  }
}
```

| Setting | Value | Description |
|---------|-------|-------------|
| `sandbox.mode` | `"off"` | No sandboxing for maximum flexibility |
| `elevated.enabled` | `true` | Allows privileged command execution |
| `gateway.bind` | `"loopback"` | Restricts gateway to localhost only (127.0.0.1) |
| `logging.redactSensitive` | `"tools"` | Redacts sensitive data in tool logs |
| `discovery.mdns.mode` | `"minimal"` | Limits mDNS information disclosure |

## Environment Variables

### Required Variables

These must be set in `/etc/environment` or your shell profile:

```bash
# Maestro Anthropic API authentication
OPENCLAW_API_KEY="your-maestro-api-key-here"

# Gateway authentication token
OPENCLAW_GATEWAY_TOKEN="your-secure-random-token-here"
```

| Variable | Purpose | Used By |
|----------|---------|---------|
| `OPENCLAW_API_KEY` | Authenticate with Maestro Anthropic API | maestro-anthropic provider |
| `OPENCLAW_GATEWAY_TOKEN` | Authenticate gateway access | openclaw.json gateway config |

### Generating Secure Tokens

```bash
# Generate 64-character hex token
openssl rand -hex 32

# Generate 32-character base64 token
openssl rand -base64 24
```

## Configuration Sections

### Model Providers

Model providers are configured in the `providers` section:

```json
{
  "providers": {
    "maestro-anthropic": {
      "type": "anthropic",
      "endpoint": "https://maestro.us.jingtao.fun/api/anthropic",
      "apiKey": "${OPENCLAW_API_KEY}"
    },
    "openai-codex": {
      "type": "openai",
      "auth": "oauth"
    }
  }
}
```

See [MODELS.md](MODELS.md) for detailed provider configuration.

### Gateway Settings

```json
{
  "gateway": {
    "bind": "loopback",
    "port": 18789,
    "token": "${OPENCLAW_GATEWAY_TOKEN}"
  }
}
```

| Setting | Description |
|---------|-------------|
| `bind` | Network interface (`"loopback"` = 127.0.0.1 only) |
| `port` | Gateway listening port |
| `token` | Authentication token for gateway access |

### Logging Configuration

```json
{
  "logging": {
    "level": "info",
    "file": "/tmp/openclaw/openclaw-{date}.log",
    "redactSensitive": "tools"
  }
}
```

| Setting | Description |
|---------|-------------|
| `level` | Log level: `debug`, `info`, `warn`, `error` |
| `file` | Log file path (supports `{date}` placeholder) |
| `redactSensitive` | What to redact: `none`, `tools`, `all` |

## File Permissions

Secure your configuration with proper permissions:

```bash
# Secure directory
chmod 700 ~/.openclaw

# Secure config file
chmod 600 ~/.openclaw/openclaw.json
```

## Validation

Check configuration validity:

```bash
# Run configuration doctor
openclaw doctor --fix

# Deep security audit
openclaw security audit --deep

# View current configuration
openclaw config show
```

## Next Steps

- **Model Setup**: See [MODELS.md](MODELS.md) for provider authentication
- **Security**: See [SECURITY.md](SECURITY.md) for security considerations
- **Usage**: See [USAGE.md](USAGE.md) for common commands

---

[← Back to OpenClaw Overview](README.md)
