# Server Initialization Guide

Step-by-step guide for provisioning a fresh Ubuntu VM.

## Prerequisites

- A fresh Ubuntu 22.04+ server (cloud VM or local machine)
- Root access
- Internet connectivity

## Steps

### 1. Clone the repository

```bash
git clone https://github.com/JingtaoLearn/ai-learn.git
cd ai-learn/vm/scripts
```

### 2. Configure swap space

```bash
# Generic Linux (creates a swap file)
sudo bash 00-set-swap.sh --size 4096

# Azure VM (uses waagent ResourceDisk)
sudo bash 00-set-swap.sh --size 4096 --method azure
```

### 3. Install system tools

```bash
sudo bash 01-install-software.sh
```

Installs: htop, iftop, vim, tree, git, net-tools, iperf3, curl, and related packages.

### 4. Install Docker

```bash
sudo bash 02-install-docker.sh
```

Installs Docker CE, Docker CLI, containerd, and the Docker Compose V2 plugin.

### 5. Set environment variables

```bash
sudo bash 03-set-env.sh --domain example.com --email admin@example.com
```

This writes the following variables to `/etc/environment`:

| Variable | Description |
|----------|-------------|
| `S_DOMAIN` | Base domain for services |
| `S_EMAIL` | Email for Let's Encrypt certificates |
| `S_MACHINE_TYPE` | `cloud` or `local` |
| `S_CONTAINER_FOLDER_ACTIVE` | Mount path for active/runtime data |
| `S_CONTAINER_FOLDER_STATIC` | Mount path for persistent/static data |

Log out and back in (or `source /etc/environment`) to apply.

### 6. Start services

See [Docker Services](docker-services.md) for deploying nginx-proxy, pastebin, and other services.

## Idempotency

All scripts use flag files (`flags/<script-name>/<step>.done`) to track completed steps. Re-running a script safely skips already-completed steps.

To re-run a step, delete the corresponding flag file:

```bash
rm vm/scripts/flags/<script-name>/<step>.done
```
