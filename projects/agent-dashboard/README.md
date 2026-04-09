# Agent Dashboard

Live visualization dashboard for the Multi-Agent Orchestration system (Zhang ↔ Workers).

## Overview

A single-page web app that monitors worker agent sessions, the Knowledge Base, and orchestrator state in real time. Auto-refreshes every 5 seconds and falls back to demo/mock data if the data files are not yet present.

## Features

- **Stats bar** — running / blocked / complete worker counts, KB entry count, ticket count
- **Flow diagram** — animated SVG showing the 6-phase orchestration cycle (Dispatch → Workers → Detect → KB Query → Steer → Complete)
- **Worker cards** — one card per session with status badge, task description, elapsed time, and expandable event timeline
- **Orchestrator log** — scrollable activity feed derived from `orchestrator-state.json` and live status changes
- **Knowledge Base panel** — expandable KB entries with problem / solution / source
- **Architecture diagram** — static SVG of the full system topology

## Data Sources

| API endpoint | Source file |
|---|---|
| `GET /api/workers` | `~/.openclaw/agents/worker/sessions/*.jsonl` |
| `GET /api/kb` | `~/.openclaw/workspace/memory/kb/*.md` |
| `GET /api/orchestrator-state` | `~/.openclaw/workspace/subagent-tracking/orchestrator-state.json` |

The server also checks for `~/.openclaw/workspace/subagent-tracking/dashboard-state.json` — if present, its `workers` array is used instead of parsing JSONL files directly (useful for the orchestrator to push consolidated state).

## Running Locally

```bash
cd projects/agent-dashboard
node server.js
# Open http://localhost:3847
```

## Docker Deployment

```bash
cd projects/agent-dashboard
docker compose up -d
```

Requires the `nginx-proxy` external network and the following env vars in `/etc/environment`:
- `S_EMAIL` — admin email for Let's Encrypt

The workspace directories are mounted read-only into the container.

## Stack

- **Server**: Node.js 22 (plain `http` module, no frameworks)
- **Frontend**: Single `index.html` with embedded CSS + vanilla JS (no build step)
