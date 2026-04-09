# Wedding Prep

A mobile-first wedding item preparation and checklist web app. Each project gets a unique UUID-based URL — knowing the URL is the only credential needed to access it.

## Features

- Create named projects with unguessable UUID URLs
- Add, edit, and delete preparation items
- Filter by venue, person in charge, or status
- Color-coded status badges
- Mobile-first, touch-friendly UI with Chinese labels
- JSON file-based storage with atomic writes

## Item Fields

| Field | Type | Description |
|-------|------|-------------|
| name | string | Item name |
| quantity | number | Quantity needed |
| venue | enum | Location: 丰县, 婚房, 婚礼现场, 宴会厅, 埠口家 |
| person | enum | Person in charge: 张景涛, 渠琪, 丛领兹 |
| status | enum | Status: 采买中, 待发货, 已收货, 已就绪 |
| nextCheckDate | date | Next check-in date (optional) |
| notes | string | Free-text notes (optional) |

## API

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/projects` | Create a new project |
| GET | `/api/projects/:uuid` | Get project with items |
| POST | `/api/projects/:uuid/items` | Add an item |
| PUT | `/api/projects/:uuid/items/:itemId` | Update an item |
| DELETE | `/api/projects/:uuid/items/:itemId` | Delete an item |

No list/enumerate endpoint exists by design — UUID is the access credential.

## Deployment

```bash
docker compose up -d
```

Accessible at `https://prep.${S_DOMAIN}`.

Data is persisted in the `wedding-prep-data` Docker volume at `/data/projects.json`.

## Development

```bash
npm install
npm run dev
```

Server runs on port 3000 with `--watch` for auto-reload.
