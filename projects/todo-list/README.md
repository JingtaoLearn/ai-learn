# Todo List

A simple todo list application with backend data persistence.

## Features

- Add, edit (double-click), and delete todos
- Mark todos as completed
- Filter by status (All / Active / Completed)
- Dark/light theme toggle
- Data persisted in SQLite database (survives browser/device changes)
- Cross-browser and cross-device access to the same data

## Tech Stack

- **Frontend**: Pure HTML/CSS/JS, no dependencies
- **Backend**: Node.js + Express + better-sqlite3
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

### Data Persistence

Todo data is stored in a SQLite database mounted as a Docker volume (`todo-data`). Data survives container restarts and rebuilds.

## API

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/todos` | List all todos |
| `POST` | `/api/todos` | Create a todo (`{ "text": "..." }`) |
| `PUT` | `/api/todos/:id` | Update a todo (`{ "text": "...", "done": true/false }`) |
| `DELETE` | `/api/todos/:id` | Delete a todo |

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
