# Note App

A note-taking web application with Markdown support, tags, pinning, and auto-save.

## Features

- Create, edit, and delete notes with Markdown content
- Markdown rendering with live preview (via marked.js)
- Tag management (add/remove tags, filter by tag)
- Pin important notes to the top
- Auto-save with debounced editing
- Search notes by title and content
- Sort by date created, date modified, or title
- Dark/light theme toggle
- Responsive design for mobile and desktop
- SQLite persistence with Docker volume

## Tech Stack

| Layer    | Technology                          |
|----------|-------------------------------------|
| Frontend | Pure HTML / CSS / JS (single SPA)   |
| Backend  | Node.js + Express + better-sqlite3  |
| Runtime  | Docker (node:20-alpine)             |
| Proxy    | nginx-proxy with ACME auto-cert     |

## Deployment

```bash
cd projects/note-app
docker compose up -d --build
```

Access at `https://note.${S_DOMAIN}`

## Environment Variables

| Variable         | Description                  |
|------------------|------------------------------|
| `VIRTUAL_HOST`   | Domain for nginx-proxy       |
| `VIRTUAL_PORT`   | Internal port (80)           |
| `LETSENCRYPT_HOST` | Domain for SSL certificate |
| `DB_PATH`        | SQLite database file path    |

## API Endpoints

| Method | Path                          | Description        |
|--------|-------------------------------|--------------------|
| GET    | `/api/notes`                  | List all notes     |
| POST   | `/api/notes`                  | Create a note      |
| PUT    | `/api/notes/:id`              | Update a note      |
| DELETE | `/api/notes/:id`              | Delete a note      |
| GET    | `/api/tags`                   | List all tags      |
| POST   | `/api/notes/:id/tags`         | Add tag to note    |
| DELETE | `/api/notes/:id/tags/:tagId`  | Remove tag from note |

## Database Schema

- **notes**: id, title, content, pinned, created_at, updated_at
- **tags**: id, name (unique)
- **note_tags**: note_id, tag_id (junction table)
