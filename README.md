# ai-learn

AI-driven learning repository for server infrastructure, self-developed projects, and reusable skills. Primarily developed and maintained by Claude Code.

## Repository Structure

This repository is organized into three main areas:

### [`vm/`](vm/)
Server infrastructure management including:
- **[scripts/](vm/scripts/)** - Numbered initialization scripts for provisioning Ubuntu VMs
- **[docker-services/](vm/docker-services/)** - Services deployed using pre-built Docker images from public registries
- **[host-services/](vm/host-services/)** - Services deployed directly on the VM host (OpenClaw, etc.)

### [`projects/`](projects/)
Self-developed projects and applications built within this repository. Unlike docker-services which use pre-built images, projects contain source code and custom Dockerfiles. All projects integrate with nginx-proxy for HTTPS reverse proxy.

### [`skills/`](skills/)
Reusable Claude Code skills providing specialized knowledge, workflows, and automation scripts. Skills are symlinked to `~/.claude/skills/` for use across sessions.

## Quick Links

- **Getting Started**: [VM Initialization Guide](vm/scripts/)
- **Deploy Services**: [Docker Services Overview](vm/docker-services/)
- **OpenClaw Setup**: [OpenClaw Documentation](vm/host-services/open-claw/)
- **Repository Conventions**: [CLAUDE.md](CLAUDE.md)

## Key Features

- Idempotent provisioning scripts with flag-based execution tracking
- Automatic HTTPS via nginx-proxy and Let's Encrypt
- OpenClaw CLI with multi-model support (Claude Opus 4.6, GPT-5.3 Codex)
- All content in English
