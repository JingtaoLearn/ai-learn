# Nav Portal

Navigation dashboard that auto-discovers all running Docker services and displays them in a clean, searchable interface.

## How It Works

The portal connects to the Docker socket to inspect all running containers. Any container with a `VIRTUAL_HOST` environment variable is treated as a web service and shown on the dashboard.

Service metadata (name, description, category, emoji) is enriched from `services-meta.json`. Services not listed in the metadata file still appear with default values under the "Other" category.

## Features

- **Auto-discovery** — no manual registration needed; any container with `VIRTUAL_HOST` appears automatically
- **Live status** — shows running status and uptime for each service
- **Search** — filter services by name, description, or category (press `/` to focus)
- **Categories** — services grouped by Apps, Infrastructure, and Other
- **Preview detection** — containers or hosts with "preview" in the name get a badge
- **Dark/light mode** — toggle with theme button, preference saved in localStorage
- **Auto-refresh** — dashboard updates every 30 seconds
- **Responsive** — works on desktop and mobile

## Adding Metadata for New Services

Edit `services-meta.json` to add display information for a service:

```json
{
  "myapp.ai.jingtao.fun": {
    "name": "My App",
    "description": "Short description of the service",
    "category": "Apps",
    "emoji": "\ud83d\ude80"
  }
}
```

Rebuild and restart the container after editing.

## Local Development

```bash
cd projects/nav-portal
npm install
node server.js
```

Requires access to Docker socket at `/var/run/docker.sock`.

Open http://localhost:3000 in a browser.

## Deployment

```bash
cd projects/nav-portal
docker compose up -d --build
```

The portal will be available at `https://nav.${S_DOMAIN}`.

## Environment Variables

| Variable | Description |
|----------|-------------|
| `VIRTUAL_HOST` | Domain for nginx-proxy (`nav.${S_DOMAIN}`) |
| `VIRTUAL_PORT` | Internal port (`3000`) |
| `LETSENCRYPT_HOST` | Domain for SSL certificate |
| `LETSENCRYPT_EMAIL` | Email for Let's Encrypt |
| `SELF_CONTAINER` | Container name to exclude from listing (default: `nav-portal`) |
| `PORT` | Server listen port (default: `3000`) |

## File Structure

```
nav-portal/
\u251c\u2500\u2500 server.js            # Express API server
\u251c\u2500\u2500 public/
\u2502   \u2514\u2500\u2500 index.html       # Frontend dashboard
\u251c\u2500\u2500 services-meta.json   # Service display metadata
\u251c\u2500\u2500 package.json
\u251c\u2500\u2500 Dockerfile
\u251c\u2500\u2500 docker-compose.yml
\u251c\u2500\u2500 .dockerignore
\u2514\u2500\u2500 .gitignore
```
