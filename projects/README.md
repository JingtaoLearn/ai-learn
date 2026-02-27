# Projects

Self-developed projects and applications built within this repository. Unlike [`vm/docker-services/`](../vm/docker-services/) which uses pre-built Docker images, projects here contain **source code and custom Dockerfiles**.

## Key Characteristics

- **Source code included** - Projects are developed in this repository
- **Custom Docker builds** - Use `Dockerfile` to build custom images
- **HTTPS integration** - Integrate with nginx-proxy for automatic SSL
- **Full control** - Complete control over application code and dependencies

## Directory Structure

```
projects/
└── <project-name>/
    ├── src/                    # Application source code
    ├── Dockerfile              # Custom image build definition
    ├── docker-compose.yml      # Deployment configuration
    ├── README.md               # Project documentation
    └── ...                     # Additional project files
```

## Deployed Projects

| Project | Subdomain | Description |
|---------|-----------|-------------|
| [todo-list](todo-list/) | `todo.${S_DOMAIN}` | Todo list app with SQLite backend |
| [openclaw-explorer](openclaw-explorer/) | `oc.${S_DOMAIN}` | OpenClaw system prompt assembly flow visualizer |

## Adding a New Project

1. Create project directory: `projects/<project-name>/`
2. Add source code in `src/` or appropriate structure
3. Create `Dockerfile` for building custom image
4. Create `docker-compose.yml` with:
   - Image build configuration
   - nginx-proxy integration (`VIRTUAL_HOST`, `LETSENCRYPT_HOST`)
   - Environment variables from `S_DOMAIN`, `S_EMAIL`, etc.
5. Add `README.md` with project documentation
6. Update this README's project table

## nginx-proxy Integration

Projects should integrate with nginx-proxy for automatic HTTPS:

```yaml
environment:
  VIRTUAL_HOST: myproject.${S_DOMAIN}
  LETSENCRYPT_HOST: myproject.${S_DOMAIN}
  LETSENCRYPT_EMAIL: ${S_EMAIL}
```

## Difference from docker-services

| Aspect | docker-services | projects |
|--------|----------------|----------|
| Source | Pre-built public images | Source code in this repo |
| Build | `image: existing/image` | `build: ./` with Dockerfile |
| Control | Limited to configuration | Full application control |
| Examples | nginx-proxy, pastebin | Custom web apps, APIs |

---

[← Back to Repository Root](../)
