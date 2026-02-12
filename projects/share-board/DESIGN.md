# Share Board - Technical Design Document

## 1. Overview

Share Board is a whiteboard application for creating and sharing visual content. A creator edits a whiteboard (images, text, freehand drawing), then generates a share link. Anyone with the link can view the board in read-only mode — no login required.

## 2. Tech Stack

| Layer | Choice | Rationale |
|-------|--------|-----------|
| **Frontend** | React + [tldraw](https://github.com/tldraw/tldraw) | tldraw is a production-ready whiteboard library with built-in support for freehand drawing, text, images, and a modern UI. It provides a complete canvas engine with undo/redo, zoom/pan, and export — avoiding the need to build canvas primitives from scratch. MIT licensed. |
| **Backend** | Node.js (Express) | Lightweight, pairs naturally with a React frontend, simple to containerize. Handles API routes, file uploads, and WebSocket connections. |
| **Real-time Sync** | WebSocket (ws) | When the creator edits a board, connected viewers see changes in real-time. The `ws` library is minimal and sufficient — no need for Socket.IO's overhead. |
| **Data Storage** | SQLite (via better-sqlite3) | Single-file database, zero configuration, perfect for a single-server deployment. Stores board metadata and snapshots. |
| **File Storage** | Local filesystem | Uploaded images stored on disk under a persistent volume. Simple, no external dependencies. |
| **Build Tool** | Vite | Fast builds, excellent React/TypeScript support, good dev experience. |
| **Language** | TypeScript (both frontend and backend) | Type safety across the full stack, shared type definitions. |
| **Containerization** | Docker (multi-stage build) | Build frontend + backend in one image, deploy via docker-compose with nginx-proxy integration. |

## 3. System Architecture

```
                         ┌─────────────────────────┐
                         │      nginx-proxy         │
                         │  (HTTPS termination)     │
                         └───────────┬─────────────┘
                                     │
                         ┌───────────▼─────────────┐
                         │    share-board container │
                         │                         │
                         │  ┌───────────────────┐  │
                         │  │  Express Server    │  │
                         │  │                   │  │
                         │  │  GET /            │  │
                         │  │  → serves React   │  │
                         │  │    SPA (static)   │  │
                         │  │                   │  │
                         │  │  /api/*           │  │
                         │  │  → REST endpoints │  │
                         │  │                   │  │
                         │  │  /ws              │  │
                         │  │  → WebSocket      │  │
                         │  │    (real-time)    │  │
                         │  └───────┬───────────┘  │
                         │          │              │
                         │  ┌───────▼───────────┐  │
                         │  │  SQLite DB        │  │
                         │  │  + uploaded files  │  │
                         │  └───────────────────┘  │
                         └─────────────────────────┘
```

**Request flow:**

1. **Creator** opens `https://board.${S_DOMAIN}` → loads React SPA
2. Creator draws/types/adds images → state managed by tldraw in the browser
3. Creator clicks **Save & Share** → POST /api/boards → server stores snapshot, returns board ID
4. Creator gets share link: `https://board.${S_DOMAIN}/view/{boardId}`
5. **Viewer** opens share link → loads React SPA in read-only mode
6. If creator is actively editing, viewer connects via WebSocket for live updates

## 4. Core Feature Design

### 4.1 Board Editing (Creator)

- tldraw provides the full editing experience out of the box: freehand drawing, shapes, text, image placement, zoom/pan
- The creator works locally in the browser — no authentication required to create
- Board state is the tldraw document snapshot (JSON)
- Images are uploaded to the server and referenced by URL in the tldraw document

### 4.2 Save & Share

- Creator clicks "Share" → frontend sends the full tldraw snapshot to the server
- Server generates a unique board ID (nanoid, 10 characters), stores the snapshot in SQLite
- Server returns the share URL to the creator
- Creator also receives an **edit token** (stored in localStorage) to allow future edits to this board

### 4.3 Read-Only Viewing

- Viewer opens `/view/{boardId}` → frontend loads the tldraw snapshot from the server
- tldraw renders in read-only mode (built-in support via `<Tldraw readOnly />` )
- No editing tools shown, just the canvas content with zoom/pan

### 4.4 Real-Time Sync (Live View)

- When a creator is editing a previously saved board, the frontend opens a WebSocket to `/ws?boardId={id}`
- Each tldraw document change is sent to the server as a diff
- Server broadcasts the diff to all connected viewers of that board
- Viewers apply the diff to their local tldraw instance, seeing changes in real-time
- If the creator is not online, the viewer simply sees the last saved snapshot (static)

### 4.5 Image Upload

- Creator drags/drops or pastes an image onto the canvas
- Frontend intercepts tldraw's asset upload, sends the file to `POST /api/boards/{boardId}/assets`
- Server stores the file on disk, returns a URL
- tldraw references this URL in its document state

## 5. Data Model

### 5.1 SQLite Schema

```sql
CREATE TABLE boards (
  id          TEXT PRIMARY KEY,          -- nanoid, 10 chars
  edit_token  TEXT NOT NULL UNIQUE,      -- nanoid, 24 chars, for creator auth
  snapshot    TEXT NOT NULL,             -- tldraw document JSON
  created_at  TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_boards_edit_token ON boards(edit_token);
```

### 5.2 File Storage Layout

```
/data/
├── db/
│   └── share-board.db          -- SQLite database
└── assets/
    └── {boardId}/
        └── {assetId}.{ext}     -- uploaded images
```

The `/data` directory maps to the Docker persistent volume (`${S_CONTAINER_FOLDER_STATIC}/share-board`).

## 6. API Design

### REST Endpoints

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| `POST` | `/api/boards` | Create a new board | None |
| `GET` | `/api/boards/:id` | Get board snapshot (read-only) | None |
| `PUT` | `/api/boards/:id` | Update board snapshot | Edit token (header) |
| `POST` | `/api/boards/:id/assets` | Upload an image asset | Edit token (header) |
| `GET` | `/api/assets/:boardId/:filename` | Serve an uploaded asset | None |

### API Details

**POST /api/boards**

```
Request:  { snapshot: <tldraw JSON> }
Response: { id: "abc123def0", editToken: "...", shareUrl: "https://board.example.com/view/abc123def0" }
```

**GET /api/boards/:id**

```
Response: { id: "abc123def0", snapshot: <tldraw JSON>, updatedAt: "2026-02-12T..." }
```

**PUT /api/boards/:id**

```
Headers:  X-Edit-Token: <editToken>
Request:  { snapshot: <tldraw JSON> }
Response: { ok: true }
```

**POST /api/boards/:id/assets**

```
Headers:      X-Edit-Token: <editToken>
Content-Type: multipart/form-data (file field: "file")
Response:     { url: "/api/assets/abc123def0/img_xyz.png" }
```

### WebSocket

**Endpoint:** `ws://host/ws?boardId={id}`

**Messages (server → client):**

```json
{ "type": "snapshot", "data": <tldraw JSON> }
{ "type": "update", "data": <tldraw diff> }
```

**Messages (client → server, creator only):**

```json
{ "type": "update", "editToken": "...", "data": <tldraw diff> }
```

## 7. Frontend Design

### 7.1 Routes

| Path | Component | Description |
|------|-----------|-------------|
| `/` | `<EditorPage>` | Create a new board |
| `/edit/:id` | `<EditorPage>` | Edit an existing board (requires edit token in localStorage) |
| `/view/:id` | `<ViewerPage>` | View a shared board (read-only) |

### 7.2 Key Components

```
src/
├── App.tsx                  -- Router setup
├── pages/
│   ├── EditorPage.tsx       -- tldraw editor + share button
│   └── ViewerPage.tsx       -- tldraw read-only viewer + live sync
├── components/
│   ├── ShareDialog.tsx      -- Modal showing share link + copy button
│   └── Toolbar.tsx          -- Custom toolbar overlay (share, new board)
├── hooks/
│   ├── useBoard.ts          -- Load/save board via API
│   ├── useLiveSync.ts       -- WebSocket connection for real-time updates
│   └── useAssetUpload.ts    -- Image upload handler for tldraw
├── lib/
│   ├── api.ts               -- API client functions
│   └── ws.ts                -- WebSocket client wrapper
└── types/
    └── board.ts             -- Shared type definitions
```

### 7.3 UI Design Principles

- **Minimal chrome**: tldraw provides its own toolbar; we add only a floating "Share" button and a top bar with the board title
- **Modern aesthetic**: Clean white background, subtle shadows, system font stack
- **Responsive**: Works on desktop (primary) and tablet; phone is view-only friendly
- **Share flow**: Click Share → dialog with copyable link → toast confirmation on copy

### 7.4 Editor Flow

1. User lands on `/` → blank tldraw canvas
2. User draws, types, adds images
3. User clicks "Share" → board is saved to server → share dialog appears
4. URL changes to `/edit/{id}` → future edits auto-save to the same board
5. Edit token stored in `localStorage` under key `board:{id}:editToken`

### 7.5 Viewer Flow

1. Viewer opens `/view/{id}` → board snapshot loaded from server
2. tldraw renders in read-only mode
3. WebSocket connection opened → if creator is live-editing, viewer sees real-time updates
4. If creator is offline, viewer sees the last saved state (static)

## 8. Deployment

### 8.1 Docker Setup

**Dockerfile** (multi-stage build):

```dockerfile
# Stage 1: Build frontend
FROM node:20-alpine AS frontend-build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

# Stage 2: Production
FROM node:20-alpine
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev
COPY --from=frontend-build /app/dist ./dist
COPY server/ ./server/
EXPOSE 3000
CMD ["node", "server/index.js"]
```

### 8.2 docker-compose.yml

```yaml
services:
  share-board:
    build: ./
    environment:
      - VIRTUAL_HOST=board.${S_DOMAIN}
      - VIRTUAL_PORT=3000
      - LETSENCRYPT_HOST=board.${S_DOMAIN}
      - LETSENCRYPT_EMAIL=${S_EMAIL}
      - BASE_URL=https://board.${S_DOMAIN}
    volumes:
      - ${S_CONTAINER_FOLDER_STATIC}/share-board:/app/data
    restart: always
    container_name: share-board
```

nginx-proxy auto-discovers the container and provisions an HTTPS certificate via Let's Encrypt.

### 8.3 WebSocket Proxy

nginx-proxy supports WebSocket proxying. The backend should handle the `Upgrade` header on the `/ws` path. No additional nginx configuration is needed — nginx-proxy passes WebSocket connections through when the backend responds with a proper `101 Switching Protocols`.

## 9. Project Directory Structure

```
projects/share-board/
├── REQUIREMENTS.md
├── DESIGN.md
├── README.md
├── package.json
├── tsconfig.json
├── vite.config.ts
├── Dockerfile
├── docker-compose.yml
├── .env.example                 -- S_DOMAIN, S_EMAIL placeholders
├── src/                         -- Frontend (React + tldraw)
│   ├── App.tsx
│   ├── main.tsx
│   ├── index.css
│   ├── pages/
│   │   ├── EditorPage.tsx
│   │   └── ViewerPage.tsx
│   ├── components/
│   │   ├── ShareDialog.tsx
│   │   └── Toolbar.tsx
│   ├── hooks/
│   │   ├── useBoard.ts
│   │   ├── useLiveSync.ts
│   │   └── useAssetUpload.ts
│   ├── lib/
│   │   ├── api.ts
│   │   └── ws.ts
│   └── types/
│       └── board.ts
└── server/                      -- Backend (Express + SQLite)
    ├── index.ts                 -- Entry point: Express + WebSocket setup
    ├── routes/
    │   ├── boards.ts            -- Board CRUD endpoints
    │   └── assets.ts            -- Asset upload/serve endpoints
    ├── ws/
    │   └── sync.ts              -- WebSocket real-time sync handler
    ├── db/
    │   └── index.ts             -- SQLite connection + schema init
    └── lib/
        └── auth.ts              -- Edit token validation helper
```

## 10. Security Considerations

- **No user authentication**: By design. Boards are public via share links.
- **Edit protection**: Only the creator (holding the edit token) can modify a board. The token is a 24-character random string — effectively unguessable.
- **File upload limits**: Max 10 MB per image, only image MIME types accepted (png, jpg, gif, webp, svg).
- **Rate limiting**: Basic rate limiting on board creation and asset upload (e.g., 10 boards/min, 50 uploads/min per IP) to prevent abuse.
- **No sensitive data**: The application stores no personal information. Board content is user-provided and public.
- **HTTPS**: Enforced via nginx-proxy + Let's Encrypt. WebSocket connections also secured (wss://).

## 11. Future Considerations (Out of Scope)

These are explicitly not part of the initial build, but noted for potential future work:

- **Board expiration**: Auto-delete boards after N days of inactivity
- **Password-protected boards**: Optional password for viewing
- **Collaborative editing**: Multiple editors on the same board (would require CRDT/OT)
- **Board listing**: Dashboard for creators to see all their boards
- **Export**: Download board as PNG/PDF
