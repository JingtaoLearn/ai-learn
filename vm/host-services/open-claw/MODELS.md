# OpenClaw Model Providers

Configuration and setup for LLM model providers in OpenClaw.

## Provider Architecture

OpenClaw uses the `litellm` provider to connect directly to a LiteLLM proxy (hosted at `litellm.us.jingtao.fun`), which routes requests to the upstream LLM backends.

```
OpenClaw (litellm/claude-opus-4.6-fast)
    ↓ OpenAI-compatible API
LiteLLM Proxy (litellm.us.jingtao.fun)
    ↓ Routes to backend providers
GitHub Copilot / Anthropic API
```

**Fallback chain:**

```
Primary:    litellm      → Claude Opus 4.6 Fast
Fallback 1: litellm      → Claude Sonnet 4.5
Fallback 2: openai-codex → GPT-5.3 Codex (OAuth)
```

## Provider Configuration

### litellm (Primary)

Connects directly to the LiteLLM proxy using the OpenAI-completions API format.

**In `openclaw.json`:**

```json
{
  "models": {
    "providers": {
      "litellm": {
        "baseUrl": "https://litellm.us.jingtao.fun/",
        "apiKey": "${S_LITELLM_API_KEY}",
        "api": "openai-completions",
        "models": [
          {
            "id": "claude-opus-4.6-fast",
            "name": "Claude Opus 4.6 Fast",
            "reasoning": true,
            "input": ["text", "image"],
            "contextWindow": 200000,
            "maxTokens": 64000
          },
          {
            "id": "claude-sonnet-4.5",
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
| `litellm/claude-opus-4.6-fast` | 200k | text+image | Primary |
| `litellm/claude-sonnet-4.5` | 200k | text+image | Fallback 1 |

### openai-codex (Fallback)

Direct connection to OpenAI API using OAuth authentication via ChatGPT Plus account.

```json
{
  "providers": {
    "openai-codex": {
      "baseUrl": "https://api.openai.com/v1",
      "api": "openai-completions",
      "models": [...]
    }
  }
}
```

**Authentication:** OAuth (no API key needed), requires ChatGPT Plus subscription.

| Model Ref | Context | Role |
|-----------|---------|------|
| `openai-codex/gpt-5.3-codex` | 200k | Fallback 2 |

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
