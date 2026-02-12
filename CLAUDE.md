# CLAUDE.md

Instructions for Claude Code when working with this repository.

## Repository Context

**ai-learn** is a learning-oriented infrastructure repository managing server provisioning, Docker services, host services, self-developed projects, and reusable skills. This repository is AI-driven and maintained primarily by Claude Code.

**Key principle**: All files must be written in **English** (code, comments, docs, commit messages).

## Repository Structure

```
ai-learn/
├── vm/                  Server infrastructure
│   ├── scripts/         Numbered initialization scripts (00-, 01-, 02-...)
│   ├── docker-services/ Pre-built image services (category/service/)
│   └── host-services/   Services deployed directly on host
├── projects/            Self-developed projects with custom builds
└── skills/              Claude Code skills (symlinked to ~/.claude/skills/)
```

## Coding Conventions

### Shell Scripts

**Required patterns:**
- Shebang: `#!/usr/bin/env bash`
- Safety: `set -euo pipefail`
- Shared library: `source "$(dirname "$0")/lib/common.sh"`
- Idempotency: Use flag-based execution (see `vm/scripts/lib/common.sh`)
- Root check: Call `require_root` for scripts needing root
- Naming: Numbered prefix for execution order (`00-`, `01-`, `02-`)

**Reference**: `vm/scripts/lib/common.sh` for shared functions

### Docker Services (Pre-built Images)

**Purpose**: Deploy services using **existing Docker images** from public registries

**Directory pattern**: `vm/docker-services/<category>/<service-name>/`

**Required files:**
- `docker-compose.yml` (mandatory)
- `README.md` (recommended)

**Environment variables** (use these in docker-compose.yml):
- `S_DOMAIN` - Base domain
- `S_EMAIL` - Admin email for SSL certificates
- `S_CONTAINER_FOLDER_ACTIVE` - Mount path for runtime data
- `S_CONTAINER_FOLDER_STATIC` - Mount path for persistent data

**nginx-proxy integration** (for HTTPS services):
```yaml
environment:
  VIRTUAL_HOST: myservice.${S_DOMAIN}
  VIRTUAL_PORT: 80
  LETSENCRYPT_HOST: myservice.${S_DOMAIN}
  LETSENCRYPT_EMAIL: ${S_EMAIL}
```

**Rules:**
- Use `docker compose` (V2 plugin syntax), not `docker-compose`
- Do NOT include deprecated `version:` field in docker-compose.yml
- Use pre-built images only (no custom Dockerfiles here)

**Reference**: `vm/docker-services/README.md` for service catalog

### Projects (Self-developed Applications)

**Purpose**: Self-developed projects with **source code and custom Dockerfiles**

**Directory pattern**: `projects/<project-name>/`

**Key difference from docker-services**:
| Aspect | docker-services | projects |
|--------|----------------|----------|
| Images | Pre-built public images | Custom-built via Dockerfile |
| Source | Not included | Full source code included |
| Build | `image: existing/image` | `build: ./` |

**Required files:**
- Source code in appropriate structure
- `Dockerfile` for custom build
- `docker-compose.yml` for deployment
- `README.md` with project documentation

**Integration**: Projects also use nginx-proxy for HTTPS (same pattern as docker-services)

**Reference**: `projects/README.md` for guidelines

### Host Services

**Purpose**: Services requiring direct host access (systemd, privileged operations)

**Directory pattern**: `vm/host-services/<service-name>/`

**Required files:**
- `README.md` with installation and configuration instructions
- Configuration files and setup scripts

**When to use**:
- Service needs direct system access
- Service manages system resources (systemd, networking)
- Performance requires native execution

**Example**: OpenClaw (AI agent with full system permissions)

**Reference**: `vm/host-services/README.md` for service list

### Environment Variables

**Rules:**
- NEVER commit `.env` files with real secrets
- ALWAYS provide `.env.example` with placeholder values
- Server-level vars go in `/etc/environment` via `vm/scripts/03-set-env.sh`

### Documentation

**Rules:**
- Every significant folder MUST have a `README.md`
- Place README directly alongside the code it documents
- Use relative links between documentation files
- All documentation in English

**Reference**: Root `README.md` for navigation structure

### Git Workflow

**Branch protection**:
- `main` branch is protected
- All changes via Pull Requests

**Commit conventions**:
- Message style: Imperative mood, concise first line
- Example: "Add docker service for pastebin" not "Added a pastebin service"

**Never commit**:
- Certificates, private keys
- `.env` files with real values
- Runtime data or logs
- Large binaries

**Use `.gitkeep`**: For empty directories needed at runtime

**Reference**: `skills/git-workflow/` for detailed workflow

## Adding New Components

### Adding a Docker Service

1. Choose or create category: `vm/docker-services/<category>/`
2. Create service directory: `vm/docker-services/<category>/<service-name>/`
3. Add `docker-compose.yml` using `S_DOMAIN`, `S_EMAIL`, etc.
4. For HTTPS: Add `VIRTUAL_HOST` and `LETSENCRYPT_HOST` variables
5. Add supporting configs if needed
6. Create `README.md` in service folder
7. Update `vm/docker-services/README.md` service table

**Remember**: Use pre-built images only, no custom Dockerfiles

### Adding a Project

1. Create directory: `projects/<project-name>/`
2. Add source code in appropriate structure
3. Create `Dockerfile` for custom build
4. Create `docker-compose.yml` for deployment
5. Add `README.md` with project documentation
6. Update `projects/README.md` project table

**Remember**: Projects contain source code and custom builds

### Adding a Skill

1. Create directory: `skills/<skill-name>/`
2. Add `SKILL.md` with YAML frontmatter:
   ```yaml
   ---
   name: skill-name
   description: What this skill does and when to use it.
   ---
   ```
3. Add optional directories: `scripts/`, `references/`, `assets/`
4. Run `./skills/install-skills.sh` to symlink
5. Update `skills/README.md` skills table

**Reference**: `skills/README.md` for skill structure

### Adding a Host Service

1. Create directory: `vm/host-services/<service-name>/`
2. Add comprehensive documentation (split into multiple files if complex)
3. Include configuration files and setup scripts
4. Use systemd user services when appropriate
5. Update `vm/host-services/README.md` service table

**Example**: See `vm/host-services/open-claw/` for multi-file documentation structure

## Common Patterns

### Multi-file Documentation

For complex services, split documentation into focused files:
- `README.md` - Overview and navigation
- `INSTALLATION.md` - Installation steps
- `CONFIGURATION.md` - Configuration reference
- `USAGE.md` - Usage guide
- `SECURITY.md` - Security considerations

**Example**: `vm/host-services/open-claw/`

### Service Management (systemd)

```bash
# User services (no sudo needed)
systemctl --user status <service>
systemctl --user start/stop/restart <service>
journalctl --user -u <service> -f
```

### Docker Service Management

```bash
cd vm/docker-services/<category>/<service-name>
docker compose up -d
docker compose ps
docker compose logs -f
docker compose down
```

## Important References

**Detailed configurations and guides:**
- OpenClaw configuration: `vm/host-services/open-claw/`
- Environment setup: `vm/scripts/03-set-env.sh`
- Script conventions: `vm/scripts/lib/common.sh`
- Docker service patterns: `vm/docker-services/README.md`
- Project guidelines: `projects/README.md`
- Skills system: `skills/README.md`

**User-facing documentation:**
- Repository overview: `README.md`
- Git workflow: `skills/git-workflow/SKILL.md`
