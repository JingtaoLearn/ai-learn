# Host Services

Services deployed directly on the VM host, outside of Docker containers. These services require direct system access or have specific requirements that make containerization impractical.

## Deployed Services

| Service | Description | Documentation |
| --- | --- | --- |
| [open-claw](open-claw/) | Self-hosted AI agent framework with multi-model support and maximum permissions | [README](open-claw/README.md) |

## When to Use Host Services

Use host services when:
- Service requires direct system access (privileged operations)
- Service manages system-level resources (systemd, networking)
- Performance requires native execution
- Service integrates with host tools (nvm, pipx, etc.)

For most other services, prefer **[Docker services](../docker-services/)** for easier isolation and deployment.

## Adding a New Host Service

1. Create service directory: `host-services/<service-name>/`
2. Add comprehensive `README.md` with:
   - Overview and prerequisites
   - Installation instructions
   - Configuration steps
   - Usage guide
3. Include configuration files and setup scripts
4. Update this README's service table

## Service Management

Most host services use systemd for process management:

```bash
# Check status
systemctl --user status <service-name>

# Start/stop service
systemctl --user start <service-name>
systemctl --user stop <service-name>

# Enable at boot
systemctl --user enable <service-name>

# View logs
journalctl --user -u <service-name> -f
```

## Current Services

### OpenClaw

Self-hosted AI agent framework with:
- Multi-model support (Claude Opus 4.6, GPT-5.3 Codex)
- Maximum permissions for full system access
- Systemd integration with automatic restart
- OAuth authentication for OpenAI

**See**: [open-claw/README.md](open-claw/README.md) for complete documentation.

---

[← Back to VM Infrastructure](../)
