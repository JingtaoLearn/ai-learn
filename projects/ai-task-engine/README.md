# AI Task Engine

AI Task-Driven Assistant System — a workflow engine that orchestrates multi-step AI tasks with Discord integration, EMS (Experience Management System) policy enforcement, and pluggable executor backends (mock and OpenClaw).

## Overview

The AI Task Engine lets you define reusable **workflow templates** as YAML files. When a task is started, the engine:

1. Creates a Discord category for the task and a channel per step
2. Checks each step against EMS before execution (policy enforcement)
3. Executes steps sequentially, routing to the appropriate executor (AI agent or automated command)
4. Waits for human approval on `human_confirm` steps
5. Advances through steps automatically on completion
6. Times out stuck steps and retries failed ones up to `maxRetries`

## Architecture

```
                    REST API / CLI
                         |
                    [ API Server ]
                         |
                   [ Task Runner ]  <-- orchestration core
                  /      |       \
         [EMS Check] [Executor] [Discord]
                         |
              [Mock] | [OpenClaw]
                         |
                  [ SQLite Storage ]
                  (workflows / tasks / steps / logs)
```

- **API Server** (`src/api/server.ts`) — Express REST API for managing tasks and steps
- **Task Runner** (`src/engine/task-runner.ts`) — Core orchestration: step activation, EMS checks, execution routing
- **State Machine** (`src/engine/state-machine.ts`) — Enforces valid status transitions for tasks and steps
- **Wake Scheduler** (`src/engine/wake-scheduler.ts`) — Cron job that detects and fails timed-out steps
- **Storage** (`src/storage/`) — SQLite via better-sqlite3 with auto-migration
- **Discord** (`src/integrations/discord.ts`) — Category/channel creation, step brief posting, status emoji updates
- **EMS** (`src/integrations/ems.ts`) — Pre-execution policy check (block / warn / pass)
- **Executor** (`src/integrations/executor/`) — Pluggable step execution backends

## Prerequisites

- Node.js 20+
- pnpm (`npm install -g pnpm`)
- SQLite (bundled via better-sqlite3)
- Discord bot token and guild ID (optional, required for Discord integration)

## Setup

```bash
# Clone and install
cd projects/ai-task-engine
pnpm install

# Configure environment
cp .env.example .env
# Edit .env with your values

# Build
pnpm build

# Run tests
pnpm test
```

## Workflow Files

Workflows are YAML files placed in the `workflows/` directory. They are loaded on daemon start or via `workflow sync`.

### Schema

```yaml
name: my-workflow          # Required. Unique workflow identifier.
description: What it does  # Optional.

steps:
  - name: step-name        # Required. Unique within workflow.
    goal: What to achieve  # Required. Passed to executor as the task.
    background: Context    # Optional. Additional context for the executor.
    rules:                 # Optional. Constraints for the executor.
      - rule-one
      - rule-two
    acceptance:            # Required. How completion is determined.
      type: human_confirm  # Waits for !approve via API/CLI/Discord
      # OR
      type: ai_self_check
      criteria: Completion criteria for the AI to self-verify
      # OR
      type: automated
      command: npm test    # Shell command; exit 0 = success
    timeout: 30m           # Optional. Format: Ns, Nm, Nh, Nd
    wakePolicy: dependency # Optional. default: dependency
    maxRetries: 0          # Optional. default: 0
```

### Example

See `workflows/bug-fix.yaml`, `workflows/feature-dev.yaml`, `workflows/research.yaml` for full examples.

## Running

### Daemon Mode (recommended)

```bash
# Start API server + wake scheduler
pnpm start
# or
task-engine start --port 3200
```

### Docker

```bash
cp .env.example .env
# Edit .env
docker compose up -d
```

## REST API Reference

Base URL: `http://localhost:3200`

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/health` | Health check |
| GET | `/api/workflows` | List loaded workflow templates |
| POST | `/api/tasks` | Create and start a task |
| GET | `/api/tasks` | List tasks (optional `?status=` filter) |
| GET | `/api/tasks/:id` | Get task with steps |
| POST | `/api/tasks/:id/cancel` | Cancel a task |
| POST | `/api/tasks/:id/steps/:stepId/approve` | Approve a human_confirm step |
| POST | `/api/tasks/:id/steps/:stepId/resume` | Resume a failed/blocked step |
| GET | `/api/tasks/:id/steps/:stepId/logs` | Get step event logs |

### Create Task

```bash
curl -X POST http://localhost:3200/api/tasks \
  -H 'Content-Type: application/json' \
  -d '{"workflow": "bug-fix", "name": "Fix login timeout", "description": "Users are timing out after 5 min"}'
```

### Approve a Step

```bash
curl -X POST http://localhost:3200/api/tasks/<taskId>/steps/<stepId>/approve
```

## CLI Reference

```bash
# Start daemon
task-engine start [--port 3200]

# List workflows
task-engine workflow list

# Create and start a task
task-engine task create -w bug-fix -n "Fix login bug" [-d "Description"]

# List tasks
task-engine task list [--status active|pending|completed|failed|cancelled]

# Show task status and steps
task-engine task status <taskId>

# Cancel a task
task-engine task cancel <taskId>

# Approve a human_confirm step
task-engine step approve <taskId> <stepId>
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DISCORD_BOT_TOKEN` | — | Discord bot token (required for Discord integration) |
| `DISCORD_GUILD_ID` | — | Discord server/guild ID |
| `EMS_BASE_URL` | `http://127.0.0.1:8100` | EMS API base URL |
| `OPENCLAW_GATEWAY_URL` | `http://127.0.0.1:18789` | OpenClaw gateway URL |
| `DB_PATH` | `./data/task-engine.db` | SQLite database path |
| `API_PORT` | `3200` | REST API port |
| `EXECUTOR_MODE` | `mock` | Executor backend: `mock` or `openclaw` |
| `LOG_LEVEL` | `info` | Log verbosity |
| `WORKFLOWS_DIR` | `./workflows` | Directory to load workflow YAML files from |

## Discord Setup

1. Create a Discord bot at https://discord.com/developers/applications
2. Enable the following bot permissions:
   - Manage Channels
   - Send Messages
   - Read Message History
3. Enable Privileged Gateway Intents: Message Content Intent
4. Invite the bot to your server with the above permissions
5. Set `DISCORD_BOT_TOKEN` and `DISCORD_GUILD_ID` in `.env`

The bot creates one Discord category per task (named `📋 <task-short-id>-<task-name>`) and one text channel per step. Step channels are prefixed with a status emoji that updates as the step progresses.

## Step Status Lifecycle

```
pending → active → executing → validating → completed
                ↘ blocked ↗
                ↘ failed → retrying → active
```

- **pending**: Created, not yet started
- **active**: EMS-checked, Discord channel created, waiting for execution
- **executing**: Executor is running (AI or automated command)
- **validating**: AI self-check in progress
- **completed**: Step finished successfully
- **failed**: Step failed (permanently or temporarily)
- **blocked**: EMS blocked the step, or manual block
- **retrying**: Scheduled for retry after failure

### OpenClaw Executor

When `EXECUTOR_MODE=openclaw`, the engine dispatches AI steps via the OpenClaw gateway:

1. Posts the step brief to the Discord step channel
2. Triggers an isolated OpenClaw agent session via `POST /hooks/agent`
3. Polls the Discord channel every 10 seconds for a structured JSON response
4. Parses the response and marks the step complete

Required env vars: `OPENCLAW_HOOKS_TOKEN`, `OPENCLAW_GATEWAY_URL` (default: `http://127.0.0.1:18789`)

### Discord Commands

In step channels, human operators can use:

- `!approve` — Approve a `human_confirm` step and advance the task
- `!reject [reason]` — Reject a step with an optional reason (marks the task as failed)

### Discord Integration Toggle

Set `DISCORD_ENABLED=false` to disable Discord integration entirely (useful for testing without Discord credentials).

## Phase 2 Roadmap

- **OpenClaw full integration**: POST step briefs to OpenClaw sessions, poll for structured JSON output, handle multi-turn conversations
- **Discord message handler**: Listen for `!approve` messages in step channels, route to approval endpoint
- **Event wake policy**: Wake a step when a specific external event fires (webhook, file system, message)
- **Scheduled wake policy**: Wake steps on a cron schedule
- **EMS learn on completion**: Submit step outcomes back to EMS as experience drafts
- **Web UI**: Dashboard for task/step monitoring and approval
- **Parallel steps**: Allow steps within a workflow to run concurrently
