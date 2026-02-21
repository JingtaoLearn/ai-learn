# OpenClaw Configuration Reference

Detailed explanation of `openclaw.json` settings and environment variables.

## Configuration File Location

The actual configuration file used by OpenClaw is located at:

```
~/.openclaw/openclaw.json
```

The file `openclaw.example.json` in this directory is a **sanitized reference copy** — it shows the configuration structure with `<REDACTED>` placeholders instead of real secrets. It is **not** used by OpenClaw directly.

To modify OpenClaw's configuration, edit `~/.openclaw/openclaw.json` directly or use `openclaw config`.

## Model Providers

See [MODELS.md](MODELS.md) for detailed provider architecture, model catalog, and setup instructions.

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
```

| Variable | Purpose | Used By |
|----------|---------|---------|
| `S_LITELLM_API_KEY` | Authenticate with LiteLLM proxy | litellm provider |

> **Note:** Discord bot token, gateway token, and hooks token are configured directly in `openclaw.json` (not via environment variables). The example config shows `<REDACTED>` for these values.

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
      "mode": "token",
      "token": "<REDACTED>"
    },
    "tailscale": {
      "mode": "off",
      "resetOnExit": false
    },
    "remote": {
      "token": "<REDACTED>"
    }
  }
}
```

| Setting | Description |
|---------|-------------|
| `port` | Gateway listening port (18789) |
| `mode` | Gateway mode (`"local"` for single-machine) |
| `bind` | Network interface (`"loopback"` = 127.0.0.1 only) |
| `auth.mode` | Authentication mode (`"token"`) |
| `auth.token` | Authentication token for local gateway access |
| `tailscale.mode` | Tailscale integration (`"off"` = disabled) |
| `remote.token` | Authentication token for remote gateway access |

## Agent Settings

```json
{
  "agents": {
    "defaults": {
      "model": {
        "primary": "litellm/github-copilot/claude-opus-4.6-fast",
        "fallbacks": [
          "litellm/github-copilot/claude-sonnet-4.5",
          "openai-codex/gpt-5.3-codex",
          "openai-codex/gpt-5.2-codex"
        ]
      },
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
| `model.primary` | Primary model (`litellm/github-copilot/claude-opus-4.6-fast`) |
| `model.fallbacks` | Fallback models in priority order |
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
      },
      "discord": {
        "enabled": true
      }
    }
  }
}
```

| Plugin | Description |
|--------|-------------|
| `memory-core` | Memory and embedding search plugin |
| `whatsapp` | WhatsApp messaging integration |
| `discord` | Discord messaging integration |

## Hooks

```json
{
  "hooks": {
    "enabled": true,
    "path": "/hooks",
    "token": "<REDACTED>"
  }
}
```

| Setting | Description |
|---------|-------------|
| `enabled` | Enable/disable webhook endpoint |
| `path` | URL path for incoming hooks |
| `token` | Authentication token for hook requests |

## Browser

```json
{
  "browser": {
    "enabled": true,
    "executablePath": "/home/jingtao/.cache/ms-playwright/chromium-1208/chrome-linux64/chrome",
    "headless": true,
    "noSandbox": true,
    "defaultProfile": "openclaw"
  }
}
```

| Setting | Description |
|---------|-------------|
| `enabled` | Enable/disable browser automation |
| `executablePath` | Path to Chromium binary (Playwright-managed) |
| `headless` | Run browser without GUI |
| `noSandbox` | Disable Chrome sandbox (needed for some server environments) |
| `defaultProfile` | Default browser profile name |

## Channels

```json
{
  "channels": {
    "discord": {
      "name": "Discord",
      "enabled": true,
      "token": "<REDACTED>",
      "groupPolicy": "allowlist",
      "dm": {
        "policy": "pairing",
        "allowFrom": ["<user-id>"]
      },
      "guilds": {
        "<guild-id>": {
          "requireMention": false,
          "users": ["<user-id-1>", "<bot-id-1>", "<bot-id-2>"],
          "channels": {
            "*": { "enabled": true },
            "<channel-id>": { "enabled": true, "requireMention": true }
          }
        }
      },
      "allowBots": true
    }
  }
}
```

| Setting | Description |
|---------|-------------|
| `enabled` | Enable/disable the channel |
| `token` | Bot token for Discord |
| `groupPolicy` | Guild access policy (`"allowlist"` restricts to listed guilds) |
| `dm.policy` | DM handling policy (`"pairing"` for paired DM sessions) |
| `dm.allowFrom` | User IDs allowed to DM the bot |
| `guilds.<id>.requireMention` | Whether bot must be @mentioned to respond (default for guild) |
| `guilds.<id>.users` | Allowed user/bot IDs in this guild |
| `guilds.<id>.channels` | Per-channel overrides (enable/disable, requireMention) |
| `allowBots` | Whether to process messages from other bots |

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
