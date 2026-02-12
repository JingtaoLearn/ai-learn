# OpenClaw Usage Guide

Common commands, workflows, and troubleshooting for OpenClaw.

## Service Management

### Check Status

```bash
# Check gateway status
systemctl --user status openclaw-gateway

# Check OpenClaw status
openclaw status

# Deep status check
openclaw status --deep
```

### Start/Stop Services

```bash
# Start
systemctl --user start openclaw-gateway

# Stop
systemctl --user stop openclaw-gateway

# Restart
systemctl --user restart openclaw-gateway

# Enable at boot
systemctl --user enable openclaw-gateway
```

### Enable Lingering

Allow services to persist after logout:

```bash
sudo loginctl enable-linger "$USER"
```

## Viewing Logs

### Gateway Logs

```bash
# Follow gateway logs
journalctl --user -u openclaw-gateway -f

# View recent gateway logs
journalctl --user -u openclaw-gateway --since "1 hour ago"

# Search for errors
journalctl --user -u openclaw-gateway | grep -i error
```

### Application Logs

```bash
# Today's log file
tail -f /tmp/openclaw/openclaw-$(date +%Y-%m-%d).log

# List all log files
ls -lh /tmp/openclaw/

# Search logs for specific content
grep "agent model" /tmp/openclaw/openclaw-$(date +%Y-%m-%d).log
```

## Model Management

### List Models

```bash
# List all available models
openclaw models list --all

# Filter by provider
openclaw models list --all | grep "litellm"
openclaw models list --all | grep "openai-codex"
```

### Test Model Access

```bash
# Check which model is being used
journalctl --user -u openclaw-gateway --since "1 minute ago" | grep "agent model"

# Test LiteLLM API directly
curl -s -X POST https://litellm.us.jingtao.fun/v1/chat/completions \
  -H "Authorization: Bearer ${S_LITELLM_API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-sonnet-4.5",
    "messages": [{"role": "user", "content": "Hello"}],
    "max_tokens": 50
  }' | jq -r '.choices[0].message.content'
```

## Channel Management

### List Channels

```bash
# List all channels
openclaw channels list

# Check channel status
openclaw channels status
```

### WhatsApp Channel

#### Setup

1. Enable WhatsApp plugin:
```bash
openclaw plugins enable whatsapp
systemctl --user restart openclaw-gateway
```

2. Add WhatsApp channel:
```bash
openclaw channels add --channel whatsapp --name "WhatsApp"
```

3. Link account (scan QR code):
```bash
openclaw channels login --channel whatsapp --verbose
```

#### Troubleshooting

**Not receiving messages:**

Check DM policy (default is "pairing" mode):
```bash
# Check current policy
openclaw config get channels.whatsapp.accounts.default.dmPolicy

# Allow all DMs (not recommended for production)
openclaw config set channels.whatsapp.accounts.default.dmPolicy open
systemctl --user restart openclaw-gateway

# Or manage allowlist
openclaw pairing list whatsapp
openclaw pairing approve whatsapp <code>
```

**Connection drops:**

Restart gateway to reconnect:
```bash
systemctl --user restart openclaw-gateway
sleep 5
openclaw status
```

#### Send Messages

```bash
# Send WhatsApp message
openclaw message send --channel whatsapp --target +1234567890 --message "Hello"
```

## Configuration Management

### View Configuration

```bash
# Show full configuration
openclaw config show

# Get specific value
openclaw config get gateway.bind
openclaw config get sandbox.mode
```

### Update Configuration

```bash
# Set configuration value
openclaw config set logging.level debug

# Restart to apply changes
systemctl --user restart openclaw-gateway
```

### Validate Configuration

```bash
# Run configuration doctor
openclaw doctor --fix

# Security audit
openclaw security audit --deep
```

## Updates

### Update OpenClaw

```bash
# Update to latest version
npm update -g openclaw

# Verify version
openclaw --version

# Regenerate gateway service file
openclaw gateway install

# Restart service
systemctl --user restart openclaw-gateway
```

## Common Workflows

### Initial Setup Workflow

```bash
# 1. Install OpenClaw
npm install -g openclaw

# 2. Run initial setup
openclaw configure

# 3. Install and start gateway
openclaw gateway install
systemctl --user enable --now openclaw-gateway

# 4. Configure OAuth for OpenAI Codex (fallback provider)
openclaw configure --section model

# 5. Enable lingering
sudo loginctl enable-linger "$USER"

# 5. Check status
openclaw status
```

### Daily Operations

```bash
# Check system status
openclaw status --deep

# View recent logs
journalctl --user -u openclaw-gateway --since "5 minutes ago"

# Monitor in real-time
journalctl --user -u openclaw-gateway -f
```

### Debugging Workflow

```bash
# 1. Check service status
systemctl --user status openclaw-gateway

# 2. View recent errors
journalctl --user -u openclaw-gateway | grep -i error

# 3. Check configuration
openclaw doctor --fix

# 4. Test model access
openclaw models list --all

# 5. Check environment variables
env | grep -E "S_LITELLM|OPENCLAW"
```

## Troubleshooting

### Gateway Won't Start

**Check logs:**
```bash
journalctl --user -u openclaw-gateway -n 50
```

**Common issues:**
- Missing environment variables: Check `S_LITELLM_API_KEY` and `OPENCLAW_GATEWAY_TOKEN`
- Port already in use: Check if port 18789 is available
- Configuration error: Run `openclaw doctor --fix`

### Models Not Available

**Check provider status:**
```bash
openclaw channels status
openclaw models list --all
```

**Common issues:**
- litellm: Verify `S_LITELLM_API_KEY` is set correctly
- openai-codex: Re-run OAuth: `openclaw configure --section model`
- Network connectivity: Test API endpoints directly

### Authentication Failures

**Rotate tokens:**
```bash
# Generate new token
openssl rand -hex 32

# Update environment variable
sudo vim /etc/environment  # Update OPENCLAW_GATEWAY_TOKEN

# Restart service
systemctl --user restart openclaw-gateway
```

### High Resource Usage

**Check resource usage:**
```bash
# CPU and memory
htop

# Process details
ps aux | grep openclaw

# Log file sizes
du -sh /tmp/openclaw/
```

**Solutions:**
- Reduce logging level: `openclaw config set logging.level warn`
- Clean old logs: `rm /tmp/openclaw/openclaw-*.log`
- Restart service: `systemctl --user restart openclaw-gateway`

## Next Steps

- **Installation**: See [INSTALLATION.md](INSTALLATION.md) for setup
- **Configuration**: See [CONFIGURATION.md](CONFIGURATION.md) for settings
- **Models**: See [MODELS.md](MODELS.md) for provider setup
- **Security**: See [SECURITY.md](SECURITY.md) for security best practices

---

[← Back to OpenClaw Overview](README.md)
