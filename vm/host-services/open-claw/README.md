# OpenClaw

Self-hosted AI agent framework with multi-model support. Runs directly on the VM host (not in Docker).

## Overview

OpenClaw provides an AI agent gateway with access to multiple LLM providers:
- **Primary**: Claude Opus 4.6 Fast via LiteLLM proxy (GitHub Copilot backend)
- **Fallback**: Claude Sonnet 4.5 (LiteLLM) → GPT-5.3 Codex / GPT-5.2-codex (OpenAI OAuth)

The system runs as a systemd user service:
- **openclaw-gateway** (port 18789) - Main agent gateway

## Documentation

- **[Installation Guide](INSTALLATION.md)** - System requirements, npm installation, and initial setup
- **[Configuration Reference](CONFIGURATION.md)** - Detailed `openclaw.json` settings and environment variables
- **[Model Providers](MODELS.md)** - Provider setup and architecture
- **[Security Guide](SECURITY.md)** - Security considerations and best practices
- **[Usage Guide](USAGE.md)** - Common commands, service management, and troubleshooting

## Quick Start

```bash
# 1. Install OpenClaw
npm install -g openclaw

# 2. Run initial setup wizard
openclaw configure

# 3. Configure OAuth for OpenAI Codex (fallback provider)
openclaw configure --section model

# 4. Install and start gateway service
openclaw gateway install
systemctl --user enable --now openclaw-gateway

# 5. Check status
openclaw status
```

## Architecture

```
OpenClaw Gateway (18789) ──┬─→ LiteLLM Proxy (litellm.us.jingtao.fun)  ← PRIMARY
                           │
                           └─→ OpenAI API (openai-codex, OAuth)         ← FALLBACK
```

## File Structure

```
open-claw/
├── README.md                              # This file - overview and navigation
├── INSTALLATION.md                        # Installation steps
├── CONFIGURATION.md                       # Configuration reference
├── MODELS.md                              # Model providers and routing
├── SECURITY.md                            # Security considerations
├── USAGE.md                               # Usage guide and commands
├── openclaw.example.json                  # Example config (reference only, not used by OpenClaw)
└── systemd/
    └── openclaw-gateway.service.example   # Gateway service template
```

> **Note:** `openclaw.example.json` is a **sanitized example** of the configuration for reference purposes. The actual configuration used by OpenClaw lives at `~/.openclaw/openclaw.json`. Sensitive values (API keys, tokens) are replaced with `${ENV_VAR}` placeholders in the example.

## Key Features

- **Direct LiteLLM integration** - Connects to LiteLLM proxy using OpenAI-compatible API
- **Multi-model fallback** - Automatic fallback between providers
- **Maximum permissions** - Full system access (exec, file ops, browser, web)
- **Systemd integration** - User service with automatic restart

---

[← Back to Host Services](../)
