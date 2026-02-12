# OpenClaw Installation Guide

Complete installation steps for setting up OpenClaw on Ubuntu VMs.

## Prerequisites

### Node.js and npm

OpenClaw requires Node.js and npm. Install via nvm, fnm, or system package:

```bash
# Option 1: Using nvm (recommended)
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.0/install.sh | bash
source ~/.bashrc
nvm install --lts

# Option 2: Using fnm
curl -fsSL https://fnm.vercel.app/install | bash
source ~/.bashrc
fnm install --lts

# Option 3: System package (Ubuntu)
sudo apt update
sudo apt install nodejs npm
```

### npm Global Configuration

Configure npm to use a user-writable global prefix:

```bash
mkdir -p ~/.npm-global
npm config set prefix '~/.npm-global'
echo 'export PATH=~/.npm-global/bin:$PATH' >> ~/.bashrc
source ~/.bashrc
```

## Installation Steps

### 1. Install OpenClaw

```bash
npm install -g openclaw
```

Verify installation:

```bash
openclaw --version
```

### 2. Configure Environment Variables

Set required environment variables in `/etc/environment`:

```bash
# Edit /etc/environment (requires root)
sudo vim /etc/environment

# Add these lines:
OPENCLAW_API_KEY="your-maestro-api-key"
OPENCLAW_GATEWAY_TOKEN="your-secure-random-token"
```

**Note**: You can also use `vm/scripts/03-set-env.sh` to set these variables.

To generate a secure random token:

```bash
openssl rand -hex 32
```

### 3. Run Setup Script

The setup script creates symlinks and configures systemd services:

```bash
cd vm/host-services/open-claw
./setup.sh
```

The script will:
1. Validate environment variables are set
2. Back up existing configuration files
3. Create symlinks from `~/.openclaw/` to repo files
4. Install and enable the gateway service
5. Optionally install the proxy service

### 4. Configure OAuth for OpenAI Codex

Run the configuration wizard to authenticate with OpenAI:

```bash
openclaw configure --section model
```

Follow the prompts to:
1. Select the `openai-codex` provider
2. Complete OAuth login via browser
3. Verify authentication

### 5. Verify Installation

Check that services are running:

```bash
systemctl --user status openclaw-gateway
openclaw status
```

List available models:

```bash
openclaw models list --all
```

## Next Steps

- **Configuration**: See [CONFIGURATION.md](CONFIGURATION.md) for detailed settings
- **Model Setup**: See [MODELS.md](MODELS.md) for provider configuration
- **Usage**: See [USAGE.md](USAGE.md) for common commands

---

[← Back to OpenClaw Overview](README.md)
