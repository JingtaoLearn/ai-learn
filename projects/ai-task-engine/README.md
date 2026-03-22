# AI Task Engine

AI Task-Driven Assistant System — a monorepo containing three services:
- **engine** — Node.js workflow orchestration engine with Discord, EMS, and OpenClaw integration
- **web** — React dashboard for task/step monitoring and approval
- **ems** — Python FastAPI Experience Management System for AI agent policy enforcement

## Monorepo Structure

```
projects/ai-task-engine/
├── engine/              Node.js workflow engine (TypeScript)
│   ├── src/             Source code
│   ├── tests/           Unit tests
│   ├── workflows/       YAML workflow templates
│   ├── package.json
│   ├── tsconfig.json
│   └── Dockerfile
├── web/                 React dashboard (Vite + Tailwind)
│   ├── src/
│   ├── nginx.conf
│   ├── package.json
│   └── Dockerfile
├── ems/                 Experience Management System (FastAPI + Python)
│   ├── app/
│   ├── tests/
│   ├── requirements.txt
│   └── Dockerfile
├── docker-compose.yml   Unified deployment (all 3 services)
├── .env.example         All env vars documented
└── .env                 Local config (gitignored)
```

## Services

### engine (port 3200)

Orchestrates multi-step AI workflows. When a task starts, the engine:

1. Creates a Discord category and channel per step
2. Checks each step against EMS before execution (policy enforcement)
3. Executes steps via the configured executor (OpenClaw AI agent or mock)
4. Waits for human approval on `human_confirm` steps
5. Retries failed steps up to `maxRetries`, times out stuck steps

### web (port 80)

React SPA dashboard served by nginx. Nginx proxies `/api/*` to `engine:3200`.

Supports optional Microsoft Entra ID (Azure AD) login via MSAL — configure `VITE_AZURE_CLIENT_ID` to enable.

### ems (port 8100, internal only)

FastAPI service for storing and retrieving AI agent experiences with semantic search. Engine calls EMS before each step to check if the action should be blocked, warned, or allowed.

## Configuration

```bash
cp .env.example .env
# Edit .env with your values
```

### All Environment Variables

| Variable | Service | Description |
|----------|---------|-------------|
| `DISCORD_BOT_TOKEN` | engine | Discord bot token |
| `DISCORD_GUILD_ID` | engine | Discord server/guild ID |
| `OPENCLAW_GATEWAY_URL` | engine | OpenClaw gateway URL (default: `http://host.docker.internal:18789`) |
| `OPENCLAW_HOOKS_TOKEN` | engine | OpenClaw auth token |
| `EXECUTOR_MODE` | engine | `openclaw` or `mock` (default: `openclaw`) |
| `API_AUTH_TOKEN` | engine | Bearer token for engine API (empty = no auth) |
| `EMS_EMBEDDING_API_KEY` | ems | API key for embedding model |
| `EMS_EMBEDDING_BASE_URL` | ems | Embedding API base URL |
| `EMS_EMBEDDING_MODEL` | ems | Embedding model name |
| `EMS_LLM_API_KEY` | ems | API key for LLM (defaults to embedding key) |
| `EMS_LLM_BASE_URL` | ems | LLM API base URL |
| `EMS_LLM_MODEL` | ems | LLM model name |
| `EMS_AUTH_TOKEN` | ems | Bearer token for EMS API (empty = no auth) |
| `VITE_AZURE_CLIENT_ID` | web | Azure AD client ID (empty = auth disabled) |
| `VITE_AZURE_TENANT_ID` | web | Azure AD tenant ID (empty = `common`) |
| `VITE_API_AUTH_TOKEN` | web | Same as `API_AUTH_TOKEN`, baked into web build |
| `S_DOMAIN` | web | Base domain for HTTPS routing |
| `S_EMAIL` | web | Email for Let's Encrypt certificates |

## Running Locally

### Engine

```bash
cd engine
npm install
cp ../.env.example ../.env   # then edit .env
npm run dev                  # ts-node src/index.ts
# or
npm run build && npm start
```

### Web

```bash
cd web
npm install
npm run dev     # http://localhost:5173 (proxies /api to localhost:3200)
```

### EMS

```bash
cd ems
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8100 --reload
```

## Docker Deployment

```bash
cp .env.example .env
# Edit .env with production values

docker compose up -d
# Web accessible at https://task.ai.<S_DOMAIN>
```

Build args for the web service (`VITE_*` vars) are passed at build time and baked into the static bundle:

```bash
docker compose build web
docker compose up -d
```

## Authentication

### API Bearer Token

Set `API_AUTH_TOKEN` (engine) and `EMS_AUTH_TOKEN` (ems) to enforce Bearer token auth. Leave empty to disable.

All requests (except `GET /api/health`) must include:
```
Authorization: Bearer <token>
```

Set `VITE_API_AUTH_TOKEN` to the same value as `API_AUTH_TOKEN` so the web dashboard can authenticate.

### Microsoft Entra ID (Azure AD) — Web Dashboard

1. Register an app in Azure Portal → App registrations
2. Set the redirect URI to your dashboard URL (e.g. `https://task.ai.example.com`)
3. Note the Application (client) ID and Directory (tenant) ID
4. Set in `.env`:
   ```
   VITE_AZURE_CLIENT_ID=<client-id>
   VITE_AZURE_TENANT_ID=<tenant-id>
   ```
5. Rebuild the web service: `docker compose build web && docker compose up -d web`

If `VITE_AZURE_CLIENT_ID` is empty, MSAL auth is skipped.

## Workflow Files

Workflows are YAML files in `engine/workflows/`. See `engine/workflows/bug-fix.yaml` for a full example.

```yaml
name: my-workflow
description: What it does

steps:
  - name: step-name
    goal: What to achieve
    background: Optional context
    rules:
      - constraint one
    acceptance:
      type: human_confirm    # OR ai_self_check OR automated
    timeout: 30m
    maxRetries: 0
```

## REST API Reference

Base URL: `http://localhost:3200`
Auth header: `Authorization: Bearer <API_AUTH_TOKEN>` (if set)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/health` | Health check (no auth) |
| GET | `/api/workflows` | List workflow templates |
| POST | `/api/tasks` | Create and start a task |
| GET | `/api/tasks` | List tasks (`?status=` filter) |
| GET | `/api/tasks/:id` | Get task with steps |
| POST | `/api/tasks/:id/cancel` | Cancel a task |
| POST | `/api/tasks/:id/steps/:stepId/approve` | Approve a human_confirm step |
| POST | `/api/tasks/:id/steps/:stepId/resume` | Resume a failed/blocked step |
| GET | `/api/tasks/:id/steps/:stepId/logs` | Get step event logs |

## Step Status Lifecycle

```
pending → active → executing → validating → completed
                ↘ blocked ↗
                ↘ failed → retrying → active
```
