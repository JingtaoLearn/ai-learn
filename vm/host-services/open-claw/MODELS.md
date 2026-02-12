# OpenClaw Model Providers

Configuration and setup for LLM model providers in OpenClaw.

## Provider Architecture

OpenClaw uses two model providers with automatic fallback:

```
Primary:    maestro-anthropic → Claude Opus 4.6 (fast mode)
Fallback 1: maestro-anthropic → Claude Sonnet 4.5
Fallback 2: openai-codex     → GPT-5.3 Codex (OAuth)
```

## Provider Configuration

### maestro-anthropic (Primary)

Direct connection to Maestro Anthropic API endpoint providing Claude models.

**Configuration:**

```json
{
  "providers": {
    "maestro-anthropic": {
      "type": "anthropic",
      "endpoint": "https://maestro.us.jingtao.fun/api/anthropic",
      "apiKey": "${OPENCLAW_API_KEY}"
    }
  }
}
```

**Authentication:**
- Uses API key authentication
- Set `OPENCLAW_API_KEY` environment variable

**Available Models:**

| Model ID | Context | Speed | Role |
|----------|---------|-------|------|
| `maestro-anthropic/github-copilot/claude-opus-4.6-fast` | 200k | Fast | Primary |
| `maestro-anthropic/github-copilot/claude-sonnet-4.5` | 200k | Normal | Fallback 1 |
| `maestro-anthropic/github-copilot/claude-haiku-4.5` | 200k | Fast | Available |
| `maestro-anthropic/github-copilot/claude-opus-4.6` | 200k | Normal | Available |

### openai-codex (Fallback)

Direct connection to OpenAI API using OAuth authentication via ChatGPT Plus account.

**Configuration:**

```json
{
  "providers": {
    "openai-codex": {
      "type": "openai",
      "auth": "oauth"
    }
  }
}
```

**Authentication:**
- Uses OAuth (no API key needed)
- Requires ChatGPT Plus subscription
- Run `openclaw configure --section model` to authenticate

**Available Models:**

| Model ID | Context | Role |
|----------|---------|------|
| `openai-codex/gpt-5.3-codex` | 200k | Fallback 2 |

## Model Priority

Models are tried in this order:

1. **Primary**: `maestro-anthropic/github-copilot/claude-opus-4.6-fast`
2. **Fallback 1**: `maestro-anthropic/github-copilot/claude-sonnet-4.5`
3. **Fallback 2**: `openai-codex/gpt-5.3-codex`

If a model fails or is unavailable, OpenClaw automatically falls back to the next model in the priority list.

## Setup Instructions

### maestro-anthropic Setup

1. Obtain API key for Maestro Anthropic API
2. Set environment variable:

```bash
export OPENCLAW_API_KEY="your-api-key-here"
```

3. Verify connection:

```bash
# Test API endpoint directly
curl -s -X POST https://maestro.us.jingtao.fun/api/anthropic/v1/messages \
  -H "x-api-key: ${OPENCLAW_API_KEY}" \
  -H "Content-Type: application/json" \
  -H "anthropic-version: 2023-06-01" \
  -d '{
    "model": "github-copilot/claude-opus-4.6-fast",
    "messages": [{"role": "user", "content": "Hello"}],
    "max_tokens": 50
  }' | jq -r '.content[0].text'
```

### openai-codex Setup

1. Ensure you have a ChatGPT Plus subscription
2. Run the configuration wizard:

```bash
openclaw configure --section model
```

3. Follow the prompts:
   - Select `openai-codex` provider
   - Authorize via browser OAuth flow
   - Confirm authentication

4. Verify authentication:

```bash
openclaw channels list
openclaw models list --all | grep "openai-codex"
```

## Testing Models

### Check Available Models

```bash
# List all available models
openclaw models list --all

# Filter by provider
openclaw models list --all | grep "maestro-anthropic"
openclaw models list --all | grep "openai-codex"
```

### Test Model Routing

```bash
# Check current agent model
journalctl --user -u openclaw-gateway --since "1 minute ago" | grep "agent model"

# View gateway logs for model selection
journalctl --user -u openclaw-gateway -f
```

### Verify Provider Status

```bash
# Check authentication status
openclaw channels status

# Deep status check
openclaw status --deep
```

## Known Issues

### "missing" Tag Display

When running `openclaw models list`, maestro-anthropic models show a "missing" tag even though they are fully functional.

**This is expected behavior for custom API endpoints.**

**Root Cause:** `maestro-anthropic` is a custom provider not in OpenClaw's built-in registry. See [MISSING-TAG-ANALYSIS.md](MISSING-TAG-ANALYSIS.md) for complete technical details.

**Verification:**
```bash
# Models work normally
openclaw agent --to +number --message "test" --local

# Logs confirm usage
journalctl --user -u openclaw-gateway -n 20 | grep "agent model"
```

## Troubleshooting

### maestro-anthropic Issues

**Problem**: API requests failing

```bash
# Check environment variable is set
echo $OPENCLAW_API_KEY

# Test API endpoint directly (see Setup Instructions above)

# Check gateway logs
journalctl --user -u openclaw-gateway -f
```

### openai-codex Issues

**Problem**: OAuth authentication expired

```bash
# Re-authenticate
openclaw configure --section model

# Check channel status
openclaw channels list
```

**Problem**: Model not available

```bash
# Verify ChatGPT Plus subscription is active
# Check OpenAI service status
# Review gateway logs for error messages
journalctl --user -u openclaw-gateway -f
```

## Model Selection Strategy

OpenClaw selects models based on:
1. **Availability** - Is the provider responding?
2. **Priority** - Follow the configured priority order
3. **Context** - Match context window requirements
4. **Cost** - Prefer faster/cheaper models when appropriate

The fallback system ensures high availability even if the primary provider is unavailable.

## Next Steps

- **Configuration**: See [CONFIGURATION.md](CONFIGURATION.md) for detailed settings
- **Usage**: See [USAGE.md](USAGE.md) for common commands
- **Security**: See [SECURITY.md](SECURITY.md) for security considerations

---

[← Back to OpenClaw Overview](README.md)
