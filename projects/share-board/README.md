# Share Board

A whiteboard application for creating and sharing visual content. Draw, type, add images, then share with a link - viewers see your board in real-time.

## Features

- **Freehand drawing** - Draw, sketch, and annotate freely
- **Text & shapes** - Add text, rectangles, arrows, and more
- **Image upload** - Drag and drop images onto the canvas
- **One-click sharing** - Generate a share link instantly
- **Real-time sync** - Viewers see live updates as you edit
- **Read-only view** - Shared links open in view-only mode
- **No login required** - Create and share without authentication

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Frontend | React + [tldraw](https://tldraw.dev) |
| Backend | Express + better-sqlite3 + ws |
| Language | TypeScript (full stack) |
| Build | Vite |
| Deploy | Docker (multi-stage build) |

## Local Development

### Prerequisites

- Node.js 20+
- npm

### Setup

```bash
# Install dependencies
npm install

# Start development server (frontend + backend)
npm run dev
```

The app will be available at:
- Frontend: http://localhost:5173
- Backend API: http://localhost:3000

The Vite dev server proxies `/api` and `/ws` requests to the backend.

### Build

```bash
npm run build
```

### Run production build locally

```bash
npm run preview
```

## Docker Deployment

### Quick Start

```bash
# Copy environment file and configure
cp .env.example .env

# Build and start
docker compose up -d
```

### Environment Variables

| Variable | Description | Example |
|----------|-------------|---------|
| `S_DOMAIN` | Base domain | `example.com` |
| `S_EMAIL` | SSL certificate email | `admin@example.com` |
| `S_CONTAINER_FOLDER_STATIC` | Persistent storage path | `/data/static` |
| `BASE_URL` | Full public URL | `https://board.example.com` |
| `PORT` | Server port (default: 3000) | `3000` |
| `DATA_DIR` | Data directory inside container | `/app/data` |

### nginx-proxy Integration

This service integrates with [nginx-proxy](https://github.com/nginx-proxy/nginx-proxy) for automatic HTTPS via Let's Encrypt. The `VIRTUAL_HOST` and `LETSENCRYPT_HOST` environment variables are set in `docker-compose.yml`.

### Data Persistence

All data is stored in a Docker volume mounted at `/app/data`:
- `data/db/share-board.db` - SQLite database
- `data/assets/{boardId}/` - Uploaded images

## API

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/boards` | Create a new board |
| `GET` | `/api/boards/:id` | Get board snapshot |
| `PUT` | `/api/boards/:id` | Update board (requires `X-Edit-Token` header) |
| `POST` | `/api/boards/:id/assets` | Upload image (requires `X-Edit-Token` header) |
| `GET` | `/api/assets/:boardId/:filename` | Serve uploaded image |
| `WS` | `/ws?boardId={id}` | Real-time sync |

## Project Structure

```
share-board/
├── src/                    Frontend (React + tldraw)
│   ├── pages/              EditorPage, ViewerPage
│   ├── components/         ShareDialog, Toolbar
│   ├── hooks/              useBoard, useLiveSync, useAssetUpload
│   ├── lib/                API client, WebSocket wrapper
│   └── types/              TypeScript type definitions
├── server/                 Backend (Express)
│   ├── routes/             Board CRUD, asset upload/serve
│   ├── ws/                 WebSocket real-time sync
│   ├── db/                 SQLite setup
│   └── lib/                Auth helpers
├── Dockerfile              Multi-stage Docker build
├── docker-compose.yml      Deployment configuration
└── vite.config.ts          Vite build configuration
```
