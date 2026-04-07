# Wedding Seating Planner

Wedding banquet seating arrangement tool (婚礼喜宴座位安排).

## Overview

A mobile-first web application for planning wedding banquet seating. Guests can be assigned to tables with specific seat positions (主陪, 副陪, 1席-8席).

## Features

- Add/remove guests to an unassigned pool
- Create/delete tables with custom labels
- Assign guests to tables with optional seat labels
- 10-seat layout per table: 主陪, 副陪, 1席-8席
- Color-coded table status (green=full, yellow=under, red=over)
- Mobile-first responsive design
- Chinese UI

## Tech Stack

- **Backend**: Node.js + Express
- **Frontend**: Vanilla HTML/CSS/JS (single file, no build step)
- **Storage**: Single JSON file (`/data/seating.json`)

## Deployment

```bash
docker compose up -d --build
```

Accessible at: https://seat.ai.jingtao.fun

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | /api/data | Get full seating data |
| POST | /api/guests | Add guest |
| PUT | /api/guests/:id | Update guest |
| DELETE | /api/guests/:id | Delete guest |
| POST | /api/tables | Add table |
| PUT | /api/tables/:id | Update table |
| DELETE | /api/tables/:id | Delete table (empty only) |
| POST | /api/guests/:id/assign | Assign guest to table |
| POST | /api/guests/:id/unassign | Remove guest from table |

## Data Storage

All data is stored in `/data/seating.json`. Writes use atomic replacement (temp file + rename) with a serialized promise queue to prevent concurrent writes.
