# Docker Services

Overview of all Docker Compose services and how to manage them.

## Available Services

| Category | Service | Directory | Description |
|----------|---------|-----------|-------------|
| `https-proxy-services` | nginx-proxy | `vm/docker-services/https-proxy-services/nginx-proxy/` | Reverse proxy with automatic HTTPS via Let's Encrypt |
| `paste-services` | pastebin | `vm/docker-services/paste-services/pastebin/` | Lightweight pastebin service |

## Starting a Service

```bash
cd vm/docker-services/<category>/<service-name>
docker compose up -d
```

## Common Operations

```bash
# Check running containers
docker compose ps

# View logs
docker compose logs -f

# Stop a service
docker compose down

# Restart a service
docker compose restart
```

## nginx-proxy

The nginx-proxy service provides automatic reverse proxying and HTTPS certificates for all other services.

**Start it first** before any other services that need HTTPS.

```bash
cd vm/docker-services/https-proxy-services/nginx-proxy
docker compose up -d
```

Other services integrate by setting these environment variables in their `docker-compose.yml`:

```yaml
environment:
  - VIRTUAL_HOST=myservice.${S_DOMAIN}
  - VIRTUAL_PORT=80
  - LETSENCRYPT_HOST=myservice.${S_DOMAIN}
```

To reload nginx configuration:

```bash
./vm/docker-services/https-proxy-services/nginx-proxy/reload-conf.sh
```

## Adding a New Service

1. Choose or create a category folder under `vm/docker-services/`
2. Create a service folder: `vm/docker-services/<category>/<service-name>/`
3. Add a `docker-compose.yml` using `S_DOMAIN`, `S_CONTAINER_FOLDER_*` env vars
4. If the service needs HTTPS, add `VIRTUAL_HOST` and `LETSENCRYPT_HOST` environment variables
5. Add supporting configs (`nginx.conf`, etc.) as needed
6. Update `vm/docker-services/README.md` with the new service
