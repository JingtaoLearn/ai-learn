# OpenClaw Configuration Reference

Detailed explanation of `openclaw.json` settings and environment variables.

## Configuration File Location

The actual configuration file used by OpenClaw is located at:

```
~/.openclaw/openclaw.json
```

The file `openclaw.example.json` in this directory is a **sanitized reference copy** — it shows the configuration structure with placeholder values (e.g., `${S_LITELLM_API_KEY}`) instead of real secrets. It is **not** used by OpenClaw directly.

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
| `browser` | Browser automation (Playwright, headless Chromium) |
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

# Discord bot token
S_DISCORD_BOT_TOKEN="your-discord-bot-token-here"
```

| Variable | Purpose | Used By |
|----------|---------|---------|
| `S_LITELLM_API_KEY` | Authenticate with LiteLLM proxy | litellm provider |
| `OPENCLAW_GATEWAY_TOKEN` | Authenticate gateway access | openclaw.json gateway config |
| `S_DISCORD_BOT_TOKEN` | Discord bot authentication | channels.discord config |

### Generating Secure Tokens

```bash
# Generate 64-character hex token
openssl rand -hex 32

# Generate 32-character base64 token
openssl rand -base64 24
```

## Browser Settings

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
| `executablePath` | Path to Chromium/Chrome binary (Playwright-managed) |
| `headless` | Run browser in headless mode (no display) |
| `noSandbox` | Disable Chrome sandbox (required for some VM environments) |
| `defaultProfile` | Default browser profile name |

## Hooks

```json
{
  "hooks": {
    "enabled": true,
    "path": "/hooks",
    "token": "${OPENCLAW_GATEWAY_TOKEN}"
  }
}
```

| Setting | Description |
|---------|-------------|
| `enabled` | Enable/disable webhook endpoint |
| `path` | URL path for the hooks endpoint |
| `token` | Authentication token for incoming webhooks |

## Gateway Settings

```json
{
  "gateway": {
    "port": 18789,
    "mode": "local",
    "bind": "loopback",
    "auth": {
      "mode": "token",
      "token": "${OPENCLAW_GATEWAY_TOKEN}"
    },
    "tailscale": {
      "mode": "off",
      "resetOnExit": false
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
      "models": {
        "litellm/github-copilot/claude-opus-4.6-fast": {},
        "litellm/github-copilot/claude-sonnet-4.5": {},
        "openai-codex/gpt-5.3-codex": {},
        "openai-codex/gpt-5.2-codex": {}
      },
      "workspace": "/home/jingtao/.openclaw/workspace",
      "compaction": {
        "mode": "safeguard"
      },
      "heartbeat": {
        "every": "5m",
        "activeHours": {
          "start": "08:00",
          "end": "24:00",
          "timezone": "Asia/Shanghai"
        },
        "target": "discord",
        "to": "channel:<channel-id>"
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
| `heartbeat.every` | Heartbeat polling interval (e.g., `"5m"`, `"30m"`) |
| `heartbeat.activeHours` | Time window for heartbeats (`start`/`end` in HH:MM, with `timezone`) |
| `heartbeat.target` | Channel to deliver heartbeat responses (e.g., `"discord"`) |
| `heartbeat.to` | Specific channel/user target (e.g., `"channel:<id>"`) |
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

## Channels

```json
{
  "channels": {
    "discord": {
      "name": "Discord",
      "enabled": true,
      "token": "${S_DISCORD_BOT_TOKEN}",
      "allowBots": true,
      "groupPolicy": "allowlist",
      "streaming": "off",
      "dmPolicy": "pairing",
      "allowFrom": ["1471352977691250891"],
      "guilds": {
        "1471415768955490418": {
          "requireMention": false,
          "users": ["1471352977691250891", "1471678943999426643", "1471680872607518932"],
          "channels": {
            "*": { "enabled": true },
            "1471752874982768794": { "requireMention": true, "enabled": true }
          }
        }
      }
    }
  }
}
```

| Setting | Description |
|---------|-------------|
| `enabled` | Enable/disable the channel |
| `token` | Bot token for Discord |
| `allowBots` | Whether to process messages from other bots |
| `groupPolicy` | Guild access policy (`"allowlist"` — only listed guilds are allowed) |
| `streaming` | Response streaming mode (`"off"` to disable streaming responses) |
| `dmPolicy` | DM policy (`"pairing"` — requires allowFrom match) |
| `allowFrom` | User IDs allowed to initiate DMs |
| `guilds.<id>.requireMention` | Whether bot must be @mentioned to respond (default for guild) |
| `guilds.<id>.users` | Allowed user IDs in the guild |
| `guilds.<id>.channels` | Per-channel overrides (e.g., require mention in specific channels) |

## Commands

```json
{
  "commands": {
    "native": "auto",
    "nativeSkills": "auto",
    "restart": true,
    "ownerDisplay": "raw"
  }
}
```

| Setting | Description |
|---------|-------------|
| `native` | Native command handling mode (`"auto"` for automatic detection) |
| `nativeSkills` | Native skill command handling mode (`"auto"` for automatic detection) |
| `restart` | Enable the `/restart` command for restarting OpenClaw |
| `ownerDisplay` | How to display the owner in command responses (`"raw"` for unformatted) |

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
