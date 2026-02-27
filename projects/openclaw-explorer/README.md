# OpenClaw Explorer

A multi-page web application for exploring and presenting how OpenClaw works under the hood.

## Features

- **Topic-based navigation** — Landing page with cards linking to individual exploration topics
- **System Prompt Assembly** — Deep dive into the 26-section prompt pipeline, entry points, modes, channels, plugins, and config knobs
- Dark/light theme toggle (persists across pages via localStorage)
- Collapsible detail sections
- Responsive design with sidebar navigation on topic pages

## Tech Stack

- **Server**: Express.js static file server
- **Frontend**: Multi-page HTML with shared CSS and JS (no build step)
- **Runtime**: Node.js 20 (Alpine)

## Local Development

```bash
npm install
node server.js
# Visit http://localhost:80
```

## Deployment

Uses nginx-proxy integration with subdomain `oc.${S_DOMAIN}`:

```bash
docker compose up -d
```

## Project Structure

```
openclaw-explorer/
├── server.js                  # Express static server (port 80)
├── public/
│   ├── index.html             # Directory/landing page — lists all topics
│   ├── css/
│   │   └── style.css          # Shared styles
│   ├── js/
│   │   └── main.js            # Shared JS (theme toggle, sidebar, collapsibles)
│   └── pages/
│       └── system-prompt.html # System Prompt Assembly topic
├── package.json               # express dependency
├── Dockerfile                 # node:20-alpine
├── docker-compose.yml         # oc.${S_DOMAIN} subdomain
└── README.md                  # This file
```

## Adding a New Topic

1. Create `public/pages/<topic-name>.html` following the `system-prompt.html` template
2. Add a card to `public/index.html`
3. No server changes needed
