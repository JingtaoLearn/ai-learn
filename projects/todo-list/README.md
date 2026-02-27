# Todo List

A simple todo list application with backend data persistence and user authentication.

## Features

- **Authentication**: User registration and login with bcrypt password hashing
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

- **Frontend**: Pure HTML/CSS/JS, no dependencies
- **Backend**: Node.js + Express + better-sqlite3
- **Auth**: express-session + better-sqlite3-session-store + bcryptjs
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

### Data Persistence

Todo data and user accounts are stored in a SQLite database mounted as a Docker volume (`todo-data`). Data survives container restarts and rebuilds.

## API

### Auth Endpoints (no authentication required)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/auth/register` | Register a new user (`{ "username": "...", "password": "..." }`) |
| `POST` | `/api/auth/login` | Login (`{ "username": "...", "password": "..." }`) |
| `POST` | `/api/auth/logout` | Logout (destroys session) |
| `GET` | `/api/auth/me` | Check current session |

### Todo Endpoints (authentication required)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/todos` | List user's todos |
| `POST` | `/api/todos` | Create a todo (`{ "text": "..." }`) |
| `PUT` | `/api/todos/:id` | Update a todo (`{ "text": "...", "done": true/false }`) |
| `DELETE` | `/api/todos/:id` | Delete a todo |
| `GET` | `/api/tags` | List tags for user's todos |

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
