# Todo List

A todo list application with backend data persistence, multiple authentication methods, and an agent-friendly API.

## Features

- **Authentication**: Username/password login, plus optional Microsoft OAuth login (Azure AD)
- **Agent-Friendly API**: API key authentication for programmatic access by AI agents
- **Per-user data**: Each user sees only their own todos
- **Session-based auth**: 7-day sessions stored in SQLite
- Add, edit (double-click), and delete todos
- Mark todos as completed
- Filter by status (All / Active / Completed)
- Priority levels (High / Medium / Low)
- Due dates with overdue tracking
- Tags with filtering
- Search across todos and tags
- Keyboard shortcuts (press `?` to see list)
- Dark/light theme toggle
- Data persisted in SQLite database (survives browser/device changes)

## Tech Stack

- **Frontend**: Pure HTML/CSS/JS + MSAL.js (Microsoft auth)
- **Backend**: Node.js + Express + better-sqlite3
- **Auth**: express-session + bcryptjs (password), MSAL.js + jwks-rsa (Microsoft), API keys (agents)
- **Docker**: node:20-alpine with volume-mounted SQLite database
- **HTTPS**: Via nginx-proxy reverse proxy + ACME auto-cert

## Deployment

```bash
cd projects/todo-list
docker compose up -d --build
```

Accessible at: **https://todo.${S_DOMAIN}** (e.g. `https://todo.ai.jingtao.fun`)

### Environment Variables (docker-compose.yml)

Domain is configured via the `S_DOMAIN` environment variable (set in system environment, e.g. `S_DOMAIN=ai.jingtao.fun`).

| Variable | Value | Description |
|----------|-------|-------------|
| `VIRTUAL_HOST` | `todo.${S_DOMAIN}` | nginx-proxy reverse proxy domain |
| `VIRTUAL_PORT` | `80` | Container service port |
| `LETSENCRYPT_HOST` | `todo.${S_DOMAIN}` | ACME auto HTTPS certificate |
| `DB_PATH` | `/data/todos.db` | SQLite database file path |
| `SESSION_SECRET` | (optional) | Secret for session signing. Auto-generated if not set |
| `MS_CLIENT_ID` | (optional) | Azure AD App Registration client ID. Leave empty to disable Microsoft login |
| `MS_TENANT_ID` | `common` | Azure AD tenant ID. Use `common` for multi-tenant or a specific tenant ID |

### Microsoft Authentication Setup

To enable Microsoft login:

1. Register an app in [Azure AD App Registrations](https://portal.azure.com/#blade/Microsoft_AAD_RegisteredApps/ApplicationsListBlade)
2. Set the redirect URI to your app's origin (e.g. `https://todo.ai.jingtao.fun`)
3. Under "Authentication", enable "ID tokens" under "Implicit grant and hybrid flows"
4. Set `MS_CLIENT_ID` to your app's Application (client) ID
5. Set `MS_TENANT_ID` to your directory (tenant) ID, or leave as `common` for any Microsoft account

### Data Persistence

Todo data, user accounts, and API keys are stored in a SQLite database mounted as a Docker volume (`todo-data`). Data survives container restarts and rebuilds.

## API

### Public Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/health` | Health check (`{ "status": "ok", "timestamp": "..." }`) |
| `GET` | `/api/auth/config` | Get Microsoft auth configuration |

### Auth Endpoints (no authentication required)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/auth/register` | Register a new user (`{ "username": "...", "password": "..." }`) |
| `POST` | `/api/auth/login` | Login (`{ "username": "...", "password": "..." }`) |
| `POST` | `/api/auth/microsoft` | Login with Microsoft ID token (`{ "idToken": "..." }`) |
| `POST` | `/api/auth/logout` | Logout (destroys session) |
| `GET` | `/api/auth/me` | Check current session |

### API Key Management (session auth required)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/keys` | List API keys for current user |
| `POST` | `/api/keys` | Create a new API key (`{ "name": "..." }`) |
| `DELETE` | `/api/keys/:id` | Revoke an API key |

### Todo Endpoints (session or API key auth)

Authenticate via session cookie (browser) or `Authorization: Bearer <api-key>` header (agents).

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/todos` | List todos (supports query filters: `status`, `priority`, `tag`, `due`, `search`) |
| `POST` | `/api/todos` | Create a todo (`{ "text": "...", "priority": "high", "due_date": "2025-01-01", "tags": ["work"] }`) |
| `PUT` | `/api/todos/:id` | Update a todo (`{ "text": "...", "done": true, "priority": "low" }`) |
| `DELETE` | `/api/todos/:id` | Delete a todo |
| `GET` | `/api/tags` | List tags for user's todos |

#### Query Filters for GET /api/todos

| Parameter | Values | Description |
|-----------|--------|-------------|
| `status` | `active`, `done`, `completed` | Filter by completion status |
| `priority` | `high`, `medium`, `low` | Filter by priority |
| `tag` | tag name | Filter by tag |
| `due` | `overdue`, `today`, `upcoming`, `no-date` | Filter by due date |
| `search` | text | Search in todo text and tag names |

#### Agent Example (curl)

```bash
# Create a todo
curl -X POST https://todo.example.com/api/todos \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"text": "Review PR #42", "priority": "high", "tags": ["work"]}'

# List active high-priority todos
curl "https://todo.example.com/api/todos?status=active&priority=high" \
  -H "Authorization: Bearer YOUR_API_KEY"

# Mark a todo as done
curl -X PUT https://todo.example.com/api/todos/1 \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"done": true}'
```

## File Structure

```
todo-list/
├── README.md              # This file
├── server.js              # Express backend with SQLite
├── package.json           # Node.js dependencies
├── public/
│   └── index.html         # Frontend (HTML + CSS + JS)
├── Dockerfile             # node:20-alpine + app
└── docker-compose.yml     # Docker Compose deployment config
```

## Local Development

```bash
cd projects/todo-list
npm install
node server.js
```

Then open http://localhost:80 in your browser.
