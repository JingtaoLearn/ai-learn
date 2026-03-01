# VM Infrastructure

Server infrastructure components for Ubuntu VMs. This directory contains everything needed to provision, configure, and deploy services on a virtual machine.

## Directory Structure

### [`scripts/`](scripts/)
Numbered shell scripts for VM provisioning and initialization. Run in order on a fresh Ubuntu installation to set up swap, install software, configure Docker, and set environment variables.

**See**: [scripts/README.md](scripts/) for detailed usage and script documentation.

### [`docker-services/`](docker-services/)
Docker Compose services deployed using **pre-built images from public registries** (Docker Hub, etc.). Organized by category, each service integrates with nginx-proxy for automatic HTTPS via Let's Encrypt.

**See**: [docker-services/README.md](docker-services/) for service catalog and deployment guide.

### [`host-services/`](host-services/)
Services deployed directly on the VM host without Docker containerization. Includes OpenClaw (Claude Code CLI) and other tools requiring direct system access.

**See**: [host-services/README.md](host-services/) for available services and installation guides.

### [`docs/`](docs/)
Infrastructure documentation covering authentication, networking, and operational guides.

- [Azure Cross-Tenant Auth](docs/azure-cross-tenant-auth.md) — Service Principal setup for cross-tenant resource access

## Environment Variables

All VM components rely on these environment variables (configured via `scripts/03-set-env.sh`):

- `S_DOMAIN` - Base domain for services
- `S_EMAIL` - Admin email for SSL certificates
- `S_CONTAINER_FOLDER_ACTIVE` - Mount path for active/runtime data
- `S_CONTAINER_FOLDER_STATIC` - Mount path for persistent/static data

## Quick Start

1. Run initialization scripts in order: [scripts/README.md](scripts/)
2. Deploy Docker services: [docker-services/README.md](docker-services/)
3. Configure host services: [host-services/README.md](host-services/)

---

[← Back to Repository Root](../)
