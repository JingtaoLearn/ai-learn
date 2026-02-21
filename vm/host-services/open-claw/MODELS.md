# OpenClaw Model Providers

Configuration and setup for LLM model providers in OpenClaw.

## Provider Architecture

OpenClaw uses a single provider:

- **litellm** — connects to a LiteLLM proxy (`litellm.us.jingtao.fun`), which routes to upstream LLM backends (GitHub Copilot / Anthropic API)

OpenAI Codex models are available as fallbacks via auth profiles but no longer configured as a separate provider block.

```
OpenClaw Gateway (18789)
    └─→ LiteLLM Proxy (litellm.us.jingtao.fun)  ← PRIMARY
            ↓
        GitHub Copilot / Anthropic API
```

**Fallback chain (as configured):**

```
Primary:    litellm/github-copilot/claude-opus-4.6-fast
Fallback 1: litellm/github-copilot/claude-sonnet-4.5
Fallback 2: openai-codex/gpt-5.3-codex
Fallback 3: openai-codex/gpt-5.2-codex
```

## Provider Configuration

### litellm (Primary)

Connects to the LiteLLM proxy using the OpenAI-completions API format.

```json
{
  "models": {
    "providers": {
      "litellm": {
        "baseUrl": "https://litellm.us.jingtao.fun/",
        "apiKey": "<REDACTED>",
        "api": "openai-completions",
        "models": [
          {
            "id": "github-copilot/claude-opus-4.6-fast",
            "name": "Claude Opus 4.6 Fast",
            "reasoning": true,
            "input": ["text", "image"],
            "contextWindow": 200000,
            "maxTokens": 64000
          },
          {
            "id": "github-copilot/claude-sonnet-4.5",
            "name": "Claude Sonnet 4.5",
            "reasoning": true,
            "input": ["text", "image"],
            "contextWindow": 200000,
            "maxTokens": 64000
          }
        ]
      }
    }
  }
}
```

**Available Models:**

| Model Ref | Context | Input | Role |
|-----------|---------|-------|------|
| `litellm/github-copilot/claude-opus-4.6-fast` | 200k | text+image | Primary |
| `litellm/github-copilot/claude-sonnet-4.5` | 200k | text+image | Fallback 1 |

### openai-codex (Fallback)

Direct connection to OpenAI API using OAuth authentication via ChatGPT Plus account. Configured via auth profiles (not as a provider block in the main config).

**Authentication:** OAuth (no API key needed), requires ChatGPT Plus subscription.

| Model Ref | Context | Role |
|-----------|---------|------|
| `openai-codex/gpt-5.3-codex` | 200k | Fallback 2 |
| `openai-codex/gpt-5.2-codex` | — | Fallback 3 |

## Services

A single systemd user service manages the system:

| Service | Port | Description |
|---------|------|-------------|
| `openclaw-gateway` | 18789 | Main OpenClaw agent gateway |

```bash
# Check status
systemctl --user status openclaw-gateway

# Restart
systemctl --user restart openclaw-gateway

# View gateway logs
journalctl --user -u openclaw-gateway -f
```

## Testing

### Check Models

```bash
openclaw models list
```

### OAuth Expired (openai-codex)

```bash
openclaw configure --section model
```

## Next Steps

- **Configuration**: See [CONFIGURATION.md](CONFIGURATION.md) for detailed settings
- **Usage**: See [USAGE.md](USAGE.md) for common commands
- **Security**: See [SECURITY.md](SECURITY.md) for security considerations

---

[← Back to OpenClaw Overview](README.md)
