# Docker Services

Docker Compose services deployed using **pre-built images from public registries** (Docker Hub, GitHub Container Registry, etc.). Each service is organized by category and integrates with nginx-proxy for automatic HTTPS.

> **Note**: This directory contains services using **existing Docker images**. For self-developed projects with custom builds, see [`/projects/`](../../projects/).

## Deployed Services

| Category | Service | Description | Documentation |
| --- | --- | --- | --- |
| `https-proxy-services` | [nginx-proxy](https-proxy-services/nginx-proxy/) | Reverse proxy with automatic HTTPS via Let's Encrypt | [README](https-proxy-services/nginx-proxy/README.md) |
| `paste-services` | [pastebin](paste-services/pastebin/) | Lightweight pastebin service | [README](paste-services/pastebin/README.md) |

## Directory Structure

```
docker-services/
├── <category-name>/
│   └── <service-name>/
│       ├── docker-compose.yml    # Required: service definition
│       ├── README.md              # Optional: service documentation
│       └── ...                    # Optional: configs, helper scripts
```

## Adding a New Service

See [CLAUDE.md](/CLAUDE.md#how-to-add-a-new-docker-service) for the step-by-step guide.

**Quick checklist:**
1. Use pre-built images (not custom Dockerfiles)
2. Create category folder if needed: `docker-services/<category>/`
3. Create service folder: `docker-services/<category>/<service-name>/`
4. Add `docker-compose.yml` using environment variables
5. For nginx-proxy integration, set `VIRTUAL_HOST` and `LETSENCRYPT_HOST`
6. Update this README's service table

## Environment Variables

All services rely on these environment variables (set via [`vm/scripts/03-set-env.sh`](../scripts/)):

- `S_DOMAIN` — Base domain for services
- `S_EMAIL` — Admin email for SSL certificates
- `S_CONTAINER_FOLDER_ACTIVE` — Mount path for active/runtime data
- `S_CONTAINER_FOLDER_STATIC` — Mount path for persistent/static data

## Common Commands

```bash
# Start a service
cd vm/docker-services/<category>/<service-name>
docker compose up -d

# Check service status
docker compose ps

# View logs
docker compose logs -f

# Stop service
docker compose down
```

---

[← Back to VM Infrastructure](../)
