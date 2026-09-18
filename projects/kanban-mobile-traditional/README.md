# Kanban Mobile Traditional

Traditional single-agent implementation for the controlled read-only mobile Kanban viewer comparison.

## Build a safe example

```bash
python3 scripts/build.py --snapshot fixtures/example-snapshot.json
python3 scripts/check.py dist/index.html fixtures/example-snapshot.json
node --check dist/index.check.js
```

## Build a private real-data preview

Pass a sanitized snapshot from outside this public repository and keep the output untracked:

```bash
python3 scripts/build.py \
  --snapshot /private/path/board-snapshot.json \
  --output dist/index.html
```

The generated HTML is self-contained and has no backend or mutation surface. `.gitignore` excludes real snapshots and generated previews so Kanban content cannot enter the public repository.

`docker-compose.yml` binds only to loopback for optional inspection of the synthetic example. This comparison prototype has no authentication and must not be exposed through a public virtual host. Publish a real-data artifact only through an approved private-link surface.
