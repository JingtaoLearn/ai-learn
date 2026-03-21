# Experience Management System (EMS)

A FastAPI service for storing and retrieving AI agent experiences with semantic search.

## Overview

EMS helps AI agents learn from past experiences by storing structured records of guardrails, practices, and lessons. It uses vector embeddings (via `sqlite-vec`) to enable semantic search, so agents can query "does this action match any known experiences?" before taking action.

## Architecture

- **FastAPI** — HTTP API framework
- **SQLite + sqlite-vec** — Persistent storage with native vector search
- **text-embedding-3-large** — 3072-dimensional embeddings via LiteLLM proxy
- **Docker** — Containerized deployment with nginx-proxy integration

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/health` | Health check |
| `POST` | `/api/check` | Check if an action matches any experiences |
| `POST` | `/api/learn` | Submit new experience draft |
| `POST` | `/api/learn/confirm` | Confirm and persist a draft |
| `GET` | `/api/experiences` | List all experiences (filterable) |
| `POST` | `/api/experiences` | Create experience directly |
| `GET` | `/api/experiences/{id}` | Get single experience |
| `PUT` | `/api/experiences/{id}` | Update experience |
| `DELETE` | `/api/experiences/{id}` | Soft-delete experience |
| `POST` | `/api/experiences/search` | Semantic + keyword hybrid search |
| `GET` | `/api/tags` | List all tags with counts |

Interactive docs available at `/docs` (Swagger UI) or `/redoc`.

## Data Model

```
Experience
├── id          UUID
├── content     Natural language description
├── category    guardrail | practice | lesson
├── severity    block | warn | info
├── tags        list[str]
├── scope       dict  (applicable context)
├── source      Origin identifier
├── status      active | deprecated | merged
├── created_at
└── updated_at
```

## Check Verdict Logic

- `block` — similarity >= 0.9 AND severity == "block"
- `warn` — similarity >= 0.8 (any severity)
- `pass` — no matches above threshold

## Getting Started

### Prerequisites

- Docker + Docker Compose V2
- nginx-proxy network: `docker network create nginx-proxy`

### Configuration

```bash
cp .env.example .env
# Edit .env and set EMS_EMBEDDING_API_KEY
```

### Run

```bash
docker compose up -d
```

### Seed initial data

```bash
docker compose exec experience-manager python scripts/seed.py
```

## Development

### Install dependencies

```bash
pip install -r requirements.txt
pip install pytest pytest-asyncio httpx
```

### Run tests

```bash
pytest tests/ -v
```

### Run locally

```bash
EMS_EMBEDDING_API_KEY=your-key EMS_DB_PATH=./local.db uvicorn app.main:app --reload --port 8100
```

## Project Structure

```
experience-manager/
├── app/
│   ├── main.py              FastAPI app + startup
│   ├── models.py            Pydantic models
│   ├── database.py          SQLite + sqlite-vec helpers
│   ├── embedding.py         Embedding client
│   ├── routers/
│   │   ├── check.py         POST /api/check
│   │   ├── learn.py         POST /api/learn, /confirm
│   │   ├── experiences.py   CRUD
│   │   ├── search.py        POST /api/experiences/search
│   │   ├── tags.py          GET /api/tags
│   │   └── health.py        GET /api/health
│   └── services/
│       ├── retrieval.py     Hybrid search logic
│       └── dedup.py         Duplicate/conflict detection
├── scripts/
│   └── seed.py              Seed data import
├── tests/                   pytest test suite
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── pyproject.toml
└── .env.example
```
