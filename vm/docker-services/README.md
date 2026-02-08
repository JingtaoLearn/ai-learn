# Docker Services

Docker Compose services deployed on the server, organized by category.

## Services

| Category | Service | Description |
| --- | --- | --- |
| `https-proxy-services` | `nginx-proxy` | Reverse proxy with automatic HTTPS via Let's Encrypt |
| `paste-services` | `pastebin` | Lightweight pastebin service |

## Adding a New Service

See [CLAUDE.md](/CLAUDE.md#how-to-add-a-new-docker-service) for the step-by-step guide.

## Environment Variables

All services rely on these environment variables (set via `vm/scripts/03-set-env.sh`):

- `S_DOMAIN` — Base domain
- `S_EMAIL` — Admin email for SSL certificates
- `S_CONTAINER_FOLDER_ACTIVE` — Path for active/runtime data
- `S_CONTAINER_FOLDER_STATIC` — Path for persistent/static data
