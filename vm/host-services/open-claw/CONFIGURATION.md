# OpenClaw Configuration Reference

Detailed explanation of `openclaw.json` settings and environment variables.

## Configuration File Location

The configuration file is stored at `~/.openclaw/openclaw.json` and symlinked to this repository for version control:

```
~/.openclaw/openclaw.json ←→ vm/host-services/open-claw/openclaw.json
```

Edits in either location are reflected in both.

## Model Providers

OpenClaw uses the `litellm` provider to connect directly to a LiteLLM proxy. See [MODELS.md](MODELS.md) for full architecture details.

```json
{
  "models": {
    "mode": "merge",
    "providers": {
      "litellm": {
        "baseUrl": "https://litellm.us.jingtao.fun/",
        "apiKey": "${S_LITELLM_API_KEY}",
        "api": "openai-completions",
        "models": [...]
      },
      "openai-codex": {
        "baseUrl": "https://api.openai.com/v1",
        "api": "openai-completions",
        "models": [...]
      }
    }
  }
}
```

### litellm Provider Settings

| Setting | Value | Description |
|---------|-------|-------------|
| `baseUrl` | `https://litellm.us.jingtao.fun/` | LiteLLM proxy endpoint |
| `apiKey` | `${S_LITELLM_API_KEY}` | API key for LiteLLM proxy authentication |
| `api` | `openai-completions` | OpenAI-compatible API format |
| `models` | `[...]` | Explicitly defined model catalog with metadata |

### openai-codex Provider Settings

| Setting | Value | Description |
|---------|-------|-------------|
| `baseUrl` | `https://api.openai.com/v1` | Direct OpenAI API endpoint |
| `api` | `openai-completions` | API format |
| Authentication | OAuth | Via ChatGPT Plus subscription, no API key needed |

### Model Fallback Chain

```json
{
  "agents": {
    "defaults": {
      "model": {
        "primary": "litellm/claude-opus-4.6-fast",
        "fallbacks": [
          "litellm/claude-sonnet-4.5",
          "openai-codex/gpt-5.3-codex"
        ]
      }
    }
  }
}
```

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
# API key for LiteLLM proxy authentication
S_LITELLM_API_KEY="your-api-key-here"

# Gateway authentication token
OPENCLAW_GATEWAY_TOKEN="your-secure-random-token-here"
```

| Variable | Purpose | Used By |
|----------|---------|---------|
| `S_LITELLM_API_KEY` | Authenticate with LiteLLM proxy | litellm provider |
| `OPENCLAW_GATEWAY_TOKEN` | Authenticate gateway access | openclaw.json gateway config |

### Generating Secure Tokens

```bash
# Generate 64-character hex token
openssl rand -hex 32

# Generate 32-character base64 token
openssl rand -base64 24
```

## Gateway Settings

```json
{
  "gateway": {
    "port": 18789,
    "mode": "local",
    "bind": "loopback",
    "auth": {
      "token": "${OPENCLAW_GATEWAY_TOKEN}"
    },
    "remote": {
      "token": "${OPENCLAW_GATEWAY_TOKEN}"
    }
  }
}
```

| Setting | Description |
|---------|-------------|
| `port` | Gateway listening port (18789) |
| `mode` | Gateway mode (`"local"` for single-machine) |
| `bind` | Network interface (`"loopback"` = 127.0.0.1 only) |
| `auth.token` | Authentication token for local gateway access |
| `remote.token` | Authentication token for remote gateway access |

## Agent Settings

```json
{
  "agents": {
    "defaults": {
      "workspace": "/home/jingtao/.openclaw/workspace",
      "compaction": {
        "mode": "safeguard"
      },
      "maxConcurrent": 4,
      "subagents": {
        "maxConcurrent": 8
      }
    }
  }
}
```

| Setting | Description |
|---------|-------------|
| `workspace` | Default agent workspace directory |
| `compaction.mode` | Context compaction strategy (`"safeguard"`) |
| `maxConcurrent` | Maximum concurrent agents |
| `subagents.maxConcurrent` | Maximum concurrent subagents per agent |

## Plugins

```json
{
  "plugins": {
    "slots": {
      "memory": "memory-core"
    },
    "entries": {
      "whatsapp": {
        "enabled": true
      }
    }
  }
}
```

| Plugin | Description |
|--------|-------------|
| `memory-core` | Memory and embedding search plugin |
| `whatsapp` | WhatsApp integration entry point |

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

## File Permissions

Secure your configuration with proper permissions:

```bash
# Secure directory
chmod 700 ~/.openclaw

# Secure config file
chmod 600 ~/.openclaw/openclaw.json
```

## Next Steps

- **Model Setup**: See [MODELS.md](MODELS.md) for provider architecture
- **Security**: See [SECURITY.md](SECURITY.md) for security considerations
- **Usage**: See [USAGE.md](USAGE.md) for common commands

---

[← Back to OpenClaw Overview](README.md)
