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
| `memory_search` | Memory semantic search |
| `memory_get` | Memory snippet retrieval |
| `cron` | Scheduled task execution |
| `tts` | Text-to-speech synthesis |

### Media Settings

```json
{
  "media": {
    "audio": {
      "enabled": true,
      "models": [
        {
          "type": "provider",
          "provider": "openai",
          "model": "gpt-4o-transcribe",
          "baseUrl": "https://litellm.us.jingtao.fun/v1",
          "headers": {
            "authorization": "Bearer ${S_LITELLM_API_KEY}"
          },
          "timeoutSeconds": 60
        }
      ]
    }
  }
}
```

| Setting | Description |
|---------|-------------|
| `media.audio.enabled` | Enable/disable audio transcription |
| `media.audio.models` | List of transcription model configurations |
| `model.provider` | Provider to use for transcription (e.g., `"openai"`) |
| `model.model` | Transcription model name (e.g., `"gpt-4o-transcribe"`) |
| `model.timeoutSeconds` | Timeout for transcription requests |

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

# AgentMail API key (for agentmail skill)
S_AGENTMAIL_API_KEY="your-agentmail-api-key-here"
```

| Variable | Purpose | Used By |
|----------|---------|---------|
| `S_LITELLM_API_KEY` | Authenticate with LiteLLM proxy | litellm provider, memory search |
| `OPENCLAW_GATEWAY_TOKEN` | Authenticate gateway access | openclaw.json gateway config |
| `S_DISCORD_BOT_TOKEN` | Discord bot authentication | channels.discord config |
| `S_AGENTMAIL_API_KEY` | AgentMail API authentication | skills.entries.agentmail |

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
    "token": "${OPENCLAW_GATEWAY_TOKEN}",
    "allowRequestSessionKey": true,
    "allowedSessionKeyPrefixes": [
      "hook:",
      "cc-task:"
    ]
  }
}
```

| Setting | Description |
|---------|-------------|
| `enabled` | Enable/disable webhook endpoint |
| `path` | URL path for the hooks endpoint |
| `token` | Authentication token for incoming webhooks |
| `allowRequestSessionKey` | Allow incoming hooks to specify a session key for routing |
| `allowedSessionKeyPrefixes` | List of allowed session key prefixes (e.g., `"hook:"`, `"cc-task:"`) |

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
        "primary": "litellm/github-copilot/claude-opus-4.6",
        "fallbacks": [
          "litellm/github-copilot/claude-sonnet-4.5",
          "openai-codex/gpt-5.3-codex",
          "openai-codex/gpt-5.2-codex"
        ]
      },
      "models": {
        "litellm/github-copilot/claude-opus-4.6": {},
        "litellm/github-copilot/claude-sonnet-4.5": {},
        "openai-codex/gpt-5.3-codex": {},
        "openai-codex/gpt-5.2-codex": {}
      },
      "workspace": "/home/jingtao/.openclaw/workspace",
      "compaction": {
        "mode": "safeguard"
      },
      "heartbeat": {
        "every": "30m",
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
| `model.primary` | Primary model (`litellm/github-copilot/claude-opus-4.6`) |
| `model.fallbacks` | Fallback models in priority order |
| `workspace` | Default agent workspace directory |
| `compaction.mode` | Context compaction strategy (`"safeguard"`) |
| `heartbeat.every` | Heartbeat polling interval (e.g., `"30m"`) |
| `heartbeat.activeHours` | Time window for heartbeats (`start`/`end` in HH:MM, with `timezone`) |
| `heartbeat.target` | Channel to deliver heartbeat responses (e.g., `"discord"`) |
| `heartbeat.to` | Specific channel/user target (e.g., `"channel:<id>"`) |
| `maxConcurrent` | Maximum concurrent agents |
| `subagents.maxConcurrent` | Maximum concurrent subagents per agent |

## Memory Search

```json
{
  "agents": {
    "defaults": {
      "memorySearch": {
        "sources": ["memory", "sessions"],
        "experimental": { "sessionMemory": true },
        "provider": "openai",
        "remote": {
          "baseUrl": "https://litellm.us.jingtao.fun/v1/",
          "apiKey": "${S_LITELLM_API_KEY}"
        },
        "model": "text-embedding-3-large",
        "sync": {
          "watch": true,
          "sessions": { "deltaBytes": 100000, "deltaMessages": 50 }
        },
        "query": {
          "hybrid": {
            "enabled": true,
            "vectorWeight": 0.7,
            "textWeight": 0.3,
            "candidateMultiplier": 4,
            "mmr": { "enabled": true, "lambda": 0.7 },
            "temporalDecay": { "enabled": true, "halfLifeDays": 30 }
          }
        },
        "cache": { "enabled": true, "maxEntries": 50000 }
      }
    }
  }
}
```

| Setting | Description |
|---------|-------------|
| `sources` | Memory sources to index (`memory` = MEMORY.md + memory/*.md, `sessions` = session transcripts) |
| `provider` | Embedding provider (`"openai"` via LiteLLM proxy) |
| `model` | Embedding model (`"text-embedding-3-large"`) |
| `sync.watch` | Watch for file changes and re-index automatically |
| `sync.sessions` | Session indexing thresholds (deltaBytes / deltaMessages) |
| `query.hybrid.enabled` | Enable hybrid search (vector + text) |
| `query.hybrid.vectorWeight` | Weight for vector similarity (0.7) |
| `query.hybrid.textWeight` | Weight for text/keyword matching (0.3) |
| `query.hybrid.mmr` | Maximal Marginal Relevance for diversity |
| `query.hybrid.temporalDecay` | Time-based relevance decay (30-day half-life) |
| `cache` | Query result cache (up to 50k entries) |

## Skills

```json
{
  "skills": {
    "install": { "nodeManager": "npm" },
    "entries": {
      "agentmail": {
        "enabled": true,
        "env": { "AGENTMAIL_API_KEY": "${S_AGENTMAIL_API_KEY}" }
      }
    }
  }
}
```

| Setting | Description |
|---------|-------------|
| `install.nodeManager` | Package manager for skill dependencies (`"npm"`) |
| `entries.<name>.enabled` | Enable/disable a specific skill |
| `entries.<name>.env` | Environment variables injected for the skill |

## Plugins

```json
{
  "plugins": {
    "slots": {
      "memory": "memory-core"
    },
    "entries": {
      "memory-core": {
        "enabled": true
      },
      "whatsapp": {
        "enabled": true
      },
      "discord": {
        "enabled": true
      },
      "msteams": {
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
| `msteams` | Microsoft Teams messaging integration |

## Channels

```json
{
  "channels": {
    "discord": {
      "name": "Discord",
      "enabled": true,
      "token": "${S_DISCORD_BOT_TOKEN}",
      "allowBots": true,
      "groupPolicy": "open",
      "streaming": "off",
      "dmPolicy": "open",
      "allowFrom": ["*"],
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
| `groupPolicy` | Guild access policy (`"open"` — any guild is allowed) |
| `streaming` | Response streaming mode (`"off"` to disable streaming responses) |
| `dmPolicy` | DM policy (`"open"` — anyone can DM) |
| `allowFrom` | User IDs allowed to initiate DMs (`["*"]` for all users) |
| `guilds.<id>.requireMention` | Whether bot must be @mentioned to respond (default for guild) |
| `guilds.<id>.users` | Allowed user IDs in the guild |
| `guilds.<id>.channels` | Per-channel overrides (e.g., require mention in specific channels) |

## Messages

```json
{
  "messages": {
    "ackReactionScope": "group-mentions",
    "tts": {
      "auto": "tagged",
      "mode": "final",
      "provider": "openai",
      "maxTextLength": 4000,
      "timeoutMs": 30000,
      "openai": {
        "apiKey": "${S_LITELLM_API_KEY}",
        "model": "gpt-4o-mini-tts",
        "voice": "alloy"
      },
      "modelOverrides": {
        "enabled": true,
        "allowText": true,
        "allowVoice": true
      }
    }
  }
}
```

| Setting | Description |
|---------|-------------|
| `ackReactionScope` | When to add ack reactions (`"group-mentions"` — only in groups when mentioned) |
| `tts.auto` | TTS trigger mode (`"tagged"` — only when `[[tts:...]]` tags are present) |
| `tts.mode` | TTS processing mode (`"final"` — generate audio from final response) |
| `tts.provider` | TTS provider (`"openai"`) |
| `tts.maxTextLength` | Maximum text length for TTS (4000 chars) |
| `tts.timeoutMs` | TTS generation timeout in milliseconds |
| `tts.openai.model` | OpenAI TTS model (`"gpt-4o-mini-tts"`) |
| `tts.openai.voice` | Default voice (`"alloy"`, options: alloy, ash, coral, echo, fable, nova, onyx, sage, shimmer) |
| `tts.modelOverrides.enabled` | Allow model override tags in messages |
| `tts.modelOverrides.allowText` | Allow text content override via tags |
| `tts.modelOverrides.allowVoice` | Allow voice selection override via tags |

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

## Memory

```json
{
  "memory": {
    "citations": "auto"
  }
}
```

| Setting | Description |
|---------|-------------|
| `citations` | Citation mode for memory search results (`"auto"` for automatic) |

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
