# CLAUDE.md

## Repository Overview

ai-learn is a learning-oriented infrastructure repository under the JingtaoLearn GitHub organization. It manages server provisioning scripts, Docker-based services, host-deployed services, projects, and skill artifacts.

This repository is AI-driven — primarily developed and maintained by Claude Code. All files must be written in English.

## Repository Structure

```
ai-learn/
├── vm/                  Server infrastructure
│   ├── scripts/         Server initialization scripts (numbered for execution order)
│   ├── docker-services/ Docker Compose services (two-level: category/service/)
│   └── host-services/   Services deployed directly on VM host
├── projects/            Standalone projects
├── skills/              Reusable knowledge, scripts, templates
└── docs/                Documentation (mirrors folder structure above)
```

## Conventions

### Language

All files, comments, variable names, commit messages, and documentation must be in **English**.

### Shell Scripts

- Shebang: `#!/usr/bin/env bash`
- Always use: `set -euo pipefail`
- Source shared library: `source "$(dirname "$0")/lib/common.sh"`
- Use flag-based idempotent execution (see `vm/scripts/lib/common.sh`)
- Scripts requiring root must verify with `require_root`
- Numbered prefixes indicate execution order: `00-`, `01-`, `02-`, `03-`

### Docker Services

- Directory pattern: `vm/docker-services/<category>/<service-name>/`
- Each service **must** have a `docker-compose.yml`
- Optional supporting files: `nginx.conf`, `Dockerfile`, helper scripts, `README.md`
- Use environment variables for domain, email, and paths:
  - `S_DOMAIN` — Base domain
  - `S_EMAIL` — Admin email for certificates
  - `S_CONTAINER_FOLDER_ACTIVE` — Mount path for active/runtime data
  - `S_CONTAINER_FOLDER_STATIC` — Mount path for persistent/static data
- Services integrating with nginx-proxy must set `VIRTUAL_HOST`, `VIRTUAL_PORT`, and `LETSENCRYPT_HOST`
- Use `docker compose` (V2 plugin syntax) in all scripts and documentation
- Do **not** include the deprecated `version:` field in docker-compose.yml

### Host Services

- Directory pattern: `vm/host-services/<service-name>/`
- Each service should have a `README.md` with installation and configuration instructions
- Store configuration files and deployment scripts alongside the README

### Environment Variables

- **Never** commit `.env` files containing real secrets
- Always provide `.env.example` with placeholder values
- Server-level env vars are set in `/etc/environment` via `vm/scripts/03-set-env.sh`

### Documentation

- Every folder with meaningful content should have a `README.md`
- Documentation in `docs/` mirrors the main folder structure
- All documentation in English

### Git Workflow

- The `main` branch is protected — all changes go through Pull Requests
- Use `.gitkeep` to preserve empty directories needed at runtime
- Never commit: certificates, private keys, `.env` files with real values, runtime data
- Commit message style: imperative mood, concise first line

## How To: Add a New Docker Service

1. Choose or create a category folder under `vm/docker-services/`
2. Create service folder: `vm/docker-services/<category>/<service-name>/`
3. Add `docker-compose.yml` using env vars for `S_DOMAIN`, `S_CONTAINER_FOLDER_*`
4. If the service needs nginx-proxy integration, add `VIRTUAL_HOST` and `LETSENCRYPT_HOST`
5. Add any supporting configs (`nginx.conf`, etc.)
6. Add a `README.md` in the service folder with deployment instructions
7. Update `vm/docker-services/README.md` service list

## How To: Add a New Project

1. Create folder: `projects/<project-name>/`
2. Include a `README.md` with purpose, setup, and usage
3. Add corresponding docs in `docs/projects/` if needed

## How To: Add a New Skill

1. Create folder or file: `skills/<topic>/`
2. Include clear descriptions and usage examples
3. Skills should be self-contained and reusable

## Common Commands

```bash
# Start a docker service
cd vm/docker-services/<category>/<service-name>
docker compose up -d

# Check service status
docker compose ps

# View logs
docker compose logs -f

# Reload nginx-proxy config
./vm/docker-services/https-proxy-services/nginx-proxy/reload-conf.sh
```
