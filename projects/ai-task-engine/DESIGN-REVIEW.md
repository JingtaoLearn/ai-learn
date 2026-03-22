# Design Review: AI Task-Driven Assistant System

**Issue:** [#93 — AI Task-Driven Assistant System](https://github.com/JingtaoLearn/ai-learn/issues/93)
**Reviewed by:** Claude Code
**Date:** 2026-03-22
**Status:** Pre-implementation review — do not build until critical issues are resolved

---

## Overview

The design describes a workflow engine where a human defines multi-step tasks, AI agents execute each step in isolated Discord channels, and EMS provides rule enforcement. The concept is sound. The Discord-as-Kanban pattern is novel and the state machine approach is the right architecture. However, several fundamental questions are unanswered that would cause a re-architecture mid-build if left unresolved.

---

## Critical Issues (must resolve before building)

### 1. The AI agent execution mechanism is entirely undefined

This is the most critical gap. The issue says "AI agent executes in isolated session within that channel" — but HOW?

**The question:** When a step becomes `active`, what exactly does the engine do?

- Option A: The engine calls the Claude API directly, constructs a system prompt from goal+background+rules, and posts AI responses to Discord
- Option B: The engine notifies a human to start a Claude Code session in that channel's context
- Option C: The engine dispatches to the OpenClaw gateway (port 18789), which handles the AI call

Each option has radically different architecture implications:
- Option A means the engine IS the AI client — it must handle streaming, tool calls, long-running conversations
- Option B means the engine is a coordinator only — humans are still in the loop for every step
- Option C creates a dependency on OpenClaw whose Discord integration is not documented

**The issue notes:** "existing OpenClaw Discord integration" — but OpenClaw is documented as a gateway on port 18789 with no Discord bot functionality in `vm/host-services/open-claw/`. This dependency may not exist yet.

**Resolution required:** Define the agent execution contract precisely. Who calls the AI API, what model, what system prompt template, how is conversation state maintained within a step session, and how does the engine know the agent is "done" vs. still working?

---

### 2. Step completion detection has no defined mechanism

The state machine transitions `executing → validating → done`, but there is no specification for HOW the engine detects that an AI agent has finished executing.

**Missing:** A completion signal protocol. Options include:
- AI posts a structured JSON block with a defined schema as its final message
- AI uses a specific Discord reaction or slash command
- Engine polls on a timeout and asks the AI "are you done?"
- AI calls a webhook endpoint to signal completion with structured output

Without this, the `output` field (which is passed as `background` to the next step) cannot be reliably extracted. The entire pipeline stalls.

---

### 3. Acceptance criteria validation is a conflated concept

The design mixes three fundamentally different validation types under one `acceptance` field:

| Example | Validation Type | Who evaluates? |
|---------|----------------|----------------|
| "Root cause analysis report produced" | Subjective AI self-assessment | AI (unreliable) |
| "Human confirms the plan" | Human gate | Human via Discord |
| "Tests pass" | Automated execution | Engine runs tests somehow |
| "PR review approved" | External system event | GitHub webhook |

The state machine cannot handle these uniformly. Each type requires different infrastructure. The `acceptance` field needs to be typed:

```yaml
acceptance:
  type: human_confirm | ai_self_check | automated_test | external_event
  # type-specific fields...
```

Phase 1 cannot realistically implement all four. Leaving this untyped blocks implementation.

---

### 4. State persistence is completely absent from the design

The design specifies a state machine with transitions, timeouts, retries, and wake mechanisms — but nowhere does it specify WHERE task and step state is stored.

If the Node.js process restarts (crash, deploy, VM reboot), all in-flight task state is lost. The engine has no way to resume.

This is not optional infrastructure. A persistence layer must be defined before writing a single line of the state machine. Given the existing codebase uses SQLite (EMS), SQLite via `better-sqlite3` or Prisma is the natural choice.

Without persistence, the engine cannot:
- Resume after crash
- Query "which tasks are currently active?"
- Implement timeouts (requires knowing when a step started)
- Implement scheduled wakes (requires knowing what to wake and when)

---

### 5. EMS "rules" concept does not match EMS's actual data model

The step model has `rules: [do-not-modify-code, no-new-dependencies]` — named string identifiers.

EMS stores **experiences** with semantic content, not named rules. The EMS API (`POST /api/check`) takes a natural language `action` and returns a verdict (block/warn/pass) based on vector similarity. There is no `GET /api/rules/do-not-modify-code` endpoint.

**The gap:** How does a rule name like `do-not-modify-code` become an EMS check? Two options:
- Option A: Rule names are just tags; the engine searches EMS for experiences tagged with that rule name
- Option B: Rule names are template strings that become natural language queries to `/api/check`

Neither option is specified. Without resolving this, the "EMS integration" step in Phase 1 cannot be built.

---

### 6. Discord server limits make the current design non-scalable

Discord hard limits per server:
- **500 channels maximum** (including all channels in all categories)
- **50 categories maximum**

The current design creates 1 category + (N+1) channels per task. A workflow with 6 steps uses 7 channels. After ~70 tasks, the server hits the category limit. After ~70 tasks with 6 steps each, it hits the channel limit.

The design mentions "category archived" at task completion but does not specify the archival mechanism. Discord has no native archiving — only deletion or moving to a hidden category. Deletion loses history. Neither is a clean solution.

**Resolution required:** Define the channel lifecycle and archival strategy. Consider:
- Using threads within a single task channel instead of separate channels per step
- Defining a maximum concurrent active tasks limit
- Specifying a cleanup/archival process (e.g., export to file, then delete)

---

## Warnings (should address, not blocking)

### W1. Five wake mechanisms is too many for Phase 1

The design lists five wake types: timeout, dependency, scheduled, event, manual.

**Event wake** (PR merged, CI done) requires inbound webhooks from GitHub, CI systems, etc. This is a separate integration project. Building this for Phase 1 adds significant scope with low priority return.

**Scheduled wake** (`wakePolicy: scheduled:daily`) requires the engine to maintain a persistent cron-style scheduler across restarts. Without the persistence layer (Critical Issue #4), this cannot work.

**Recommendation:** Phase 1 should implement only:
1. **Timeout wake** — simple: compare `step.startedAt + step.timeout` to now
2. **Dependency wake** — trivial: when step N completes, trigger step N+1
3. **Manual wake** — Discord message in channel forwarded to agent

Defer event wake and scheduled wake to Phase 2.

---

### W2. EMS write-back of human guidance is under-specified

The flow says: "Human provides guidance → Guidance written back to EMS (system learns)."

Human guidance in Discord is free-form text. Converting it to a structured EMS experience (with category, severity, tags, scope) requires either:
- Another LLM call to structure the human text
- A structured input format the human must follow
- Manual review before EMS commit (EMS already supports draft → confirm workflow)

The EMS `POST /api/learn` + `POST /api/learn/confirm` flow exists for this. The design should explicitly use the draft-confirm workflow, with the engine calling `/api/learn` and notifying the human to confirm via `/api/learn/confirm` before the experience becomes active.

---

### W3. Concurrent task handling is not addressed

Can two tasks run simultaneously? The design does not say.

Concurrent tasks share one Discord server. Two tasks with identically named steps (e.g., both have `#s1-analyze`) would create naming conflicts in Discord channel creation. The naming convention `#s1-{step-name}` is not globally unique — it only unique within a category.

More importantly: concurrent AI agents executing simultaneously create EMS query contention (benign, but worth noting), and if the engine is single-threaded, it must handle interleaved step events correctly.

---

### W4. Retry policy backoff and escalation path are vague

The step model includes `retryPolicy: { maxRetries, backoffStrategy }` but the design doesn't specify:
- What "escalation" means when max retries are exhausted (create a human-alert message? Halt the whole task? Mark as permanently `failed`?)
- Whether retry re-uses the same channel or creates a new attempt sub-channel
- How prior failure context is injected into retry attempts (as part of `background`?)

---

### W5. The workflow template storage location is not specified

The design references YAML workflow definitions with a clear schema, but doesn't say where templates live:
- Files on disk in `projects/ai-task-engine/workflows/`?
- In the database?
- Both (files as source of truth, DB as runtime cache)?

If files, hot-reload behavior needs definition. If database, a management API or CLI is needed.

---

## Suggestions (nice-to-have improvements)

### S1. Separate Discord into a notification layer, not the state layer

Discord is excellent for human-facing observability. It is not reliable as state storage (messages can be deleted, bots can fail, the API has rate limits). The engine should treat Discord as a write-only notification sink.

**Recommended pattern:**
- SQLite DB is the source of truth for all state
- Discord channels/messages are derived views, reconstructable from DB state
- If Discord sync fails, log and continue — don't fail the step

---

### S2. Type the acceptance criteria

Define a typed acceptance schema from the start:

```yaml
acceptance:
  type: human_confirm         # Human reacts or types a command in the channel
  # OR
  type: ai_self_check
  criteria: "Root cause analysis report is present in output"
  # OR
  type: automated
  command: "npm test"
  successExitCode: 0
  # OR
  type: external_event
  event: "github:pr-review-approved"
  prPattern: "fix/.*"
```

Phase 1 should implement only `human_confirm` and `ai_self_check`. This alone covers the majority of the example workflow steps.

---

### S3. Consider Discord threads for step isolation instead of channels

Discord threads within a `#task-overview` channel provide:
- Step isolation (each step = one thread)
- No channel count limits (threads don't count against the 500 limit)
- Native archival (threads auto-archive)
- Better UX (overview channel shows all threads grouped by task)

The trade-off is slightly less visual separation than dedicated channels. But the scalability benefit outweighs the cosmetic difference.

---

### S4. Define a CLI for workflow management

For Phase 1, a simple CLI (`task-engine`) is more practical than a Discord-command interface:

```
task-engine workflow list
task-engine task create --workflow bug-fix --name "Fix login timeout"
task-engine task status <task-id>
task-engine step approve <task-id> <step-id>
```

Discord can be the notification layer; the CLI is the control plane. This avoids having to implement Discord command parsing in Phase 1.

---

### S5. Define the structured output format for step completion

Step `output` is passed as `background` to the next step. Define a standard output envelope:

```json
{
  "summary": "Brief human-readable summary",
  "artifacts": [
    { "type": "file", "path": "src/auth.ts" },
    { "type": "url", "value": "https://..." }
  ],
  "metadata": {},
  "completedAt": "ISO8601"
}
```

This prevents the next step from receiving an unstructured wall of text as its background context.

---

### S6. Add an idempotency guarantee for Discord operations

The engine should check whether a category/channel already exists before creating it. Network errors, retries, and restarts could cause duplicate creation attempts. Discord channel names within a category must be unique anyway, but the engine should handle the "already exists" case gracefully without erroring.

---

## Missing Requirements (things not specified that need answers)

| # | Question | Why it matters |
|---|----------|----------------|
| MR1 | What is the AI execution mechanism? (Claude API, OpenClaw gateway, human-driven?) | Determines 60% of the implementation |
| MR2 | How does the engine detect step completion? (Structured message, slash command, webhook?) | Required for state machine to advance |
| MR3 | What is the human confirmation UX? (Emoji reaction, typed "approve", slash command?) | Required for human-gate acceptance type |
| MR4 | Where is task/step state persisted? (SQLite file? PostgreSQL? In-memory only?) | Required for reliability and timeout tracking |
| MR5 | Are concurrent tasks supported? If so, what's the max? | Determines Discord channel management strategy |
| MR6 | What happens when EMS is unavailable? (Block all steps? Proceed with warning?) | Required for error handling design |
| MR7 | Where do workflow YAML templates live on disk? | Required for template loading design |
| MR8 | How do named rules (e.g., `do-not-modify-code`) map to EMS queries? | Required for EMS integration design |
| MR9 | What is the retry context injection format? (How does failure info reach the retry attempt?) | Required for retry implementation |
| MR10 | Is there a maximum steps-per-workflow limit? | Required for Discord channel budget planning |
| MR11 | Does the engine run as a daemon service (systemd user service)? Or on-demand? | Required for deployment design |
| MR12 | What Discord bot permissions are required? (Manage Channels, Manage Threads, Send Messages, etc.) | Required for bot setup documentation |

---

## Recommended Tech Stack & Architecture

### Runtime

Node.js + TypeScript (as specified). Good fit for event-driven architecture and Discord.js.

### Dependencies

| Component | Recommended Library | Rationale |
|-----------|-------------------|-----------|
| Discord | `discord.js` v14 | Industry standard, well-maintained |
| Database | `better-sqlite3` + hand-rolled migrations | Consistent with EMS's SQLite approach; simple, no ORM needed for Phase 1 |
| Scheduler | `node-cron` | Lightweight cron for timeout checks and scheduled wakes |
| Workflow parser | `js-yaml` | Parse workflow YAML templates |
| Claude API | `@anthropic-ai/sdk` | If Option A (engine calls Claude directly) |
| HTTP client | Native `fetch` (Node 18+) | For EMS API calls |
| Schema validation | `zod` | Validate workflow YAML, step output envelopes |

### Recommended Layered Architecture

```
ai-task-engine/
├── src/
│   ├── engine/
│   │   ├── state-machine.ts     # Core: step status transitions
│   │   ├── task-runner.ts       # Orchestrates step execution
│   │   └── wake-scheduler.ts    # Timeout + dependency wake logic
│   ├── integrations/
│   │   ├── discord.ts           # Discord category/channel/message management
│   │   ├── ems.ts               # EMS /check and /learn calls
│   │   └── ai-agent.ts          # AI API call (Claude or OpenClaw)
│   ├── storage/
│   │   ├── db.ts                # SQLite connection
│   │   └── repositories/        # Task, Step, Workflow CRUD
│   ├── workflow/
│   │   └── loader.ts            # Parse and validate YAML templates
│   └── index.ts                 # Entry point (daemon mode)
├── workflows/                   # YAML workflow template files
├── data/                        # SQLite database file (gitignored)
├── Dockerfile
├── docker-compose.yml
└── README.md
```

### SQLite Schema (minimum viable)

```sql
CREATE TABLE tasks (
  id TEXT PRIMARY KEY,
  workflow_name TEXT NOT NULL,
  name TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  discord_category_id TEXT,
  current_step_index INTEGER DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE steps (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(id),
  step_index INTEGER NOT NULL,
  name TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  discord_channel_id TEXT,
  started_at TEXT,
  completed_at TEXT,
  output_json TEXT,           -- structured output for next step
  retry_count INTEGER DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
```

---

## Estimated Complexity & Recommended Phase 1 Scope Adjustment

### Current Phase 1 scope: over-specified

The issue's Phase 1 includes five wake mechanisms, EMS write-back, Discord lifecycle management, acceptance validation, and an unspecified agent execution mechanism. This is 3-4 months of engineering for one person.

### Recommended Phase 1 (MVP that actually ships)

**Build:**

1. **Workflow YAML loader** — Parse and validate workflow templates from `workflows/` directory
2. **SQLite persistence** — Task and step state storage with basic CRUD
3. **Discord integration (channels only)** — Create category, create step channel, post step brief, update channel name with emoji status
4. **Two wake mechanisms** — Timeout wake (poll-based) + manual wake (Discord message forwarded to agent)
5. **EMS read-only integration** — Before each step, call `/api/check` with step goal as action; block if verdict is `block`, warn and continue if `warn`
6. **Human confirmation acceptance type** — Human types `!approve` in channel, engine advances step to `done`
7. **Step output passing** — AI posts a structured JSON block; engine parses and injects as next step's background

**Defer to Phase 2:**

- EMS write-back of learnings (requires structured human input or LLM intermediary)
- Event wake (requires external webhooks)
- Scheduled wake (requires persistent cron with restart resilience)
- `automated_test` and `external_event` acceptance types
- Trust gradient (no-op in Phase 1 — all steps require human confirmation)
- Workflow template marketplace or sharing

### Complexity estimate for recommended Phase 1

| Component | Effort |
|-----------|--------|
| SQLite schema + repositories | Small |
| Workflow YAML loader + validation | Small |
| State machine (transitions + timeout detection) | Medium |
| Discord integration (categories, channels, messages) | Medium |
| EMS `/check` integration | Small |
| Human confirmation (`!approve` command) | Small |
| Step output parsing + injection | Small |
| Wake scheduler (timeout + dependency) | Medium |
| Agent execution (Claude API call) | Medium–Large (depends on resolution of MR1) |
| End-to-end integration + testing | Large |

**Honest assessment:** Even this reduced scope is substantial. The agent execution mechanism (MR1) is the largest unknown. If it's just a Claude API call with a structured prompt, it's Medium effort. If it requires integrating with OpenClaw's streaming API and managing multi-turn conversation state, it's Large.

**Recommendation:** Resolve MR1, MR2, MR3, and MR4 in a design session before writing any code. Then prototype the state machine + SQLite layer first (no Discord, no AI) with unit tests. Add Discord and EMS integration once the core machine is proven.
