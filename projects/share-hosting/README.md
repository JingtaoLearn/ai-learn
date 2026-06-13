# share-hosting — UUID-only static file share

Self-hosted nginx serving files at **https://share.ai.jingtao.fun** with UUID-v4 path whitelisting and anti-scan guards.

## Why

For sharing one-off HTML reports / PDFs / markdown without exposing them via a guessable URL or third-party service. The UUID v4 path provides ~122 bits of entropy — brute-force scanning is infeasible.

## What it serves

- Only paths matching `^/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.(html|pdf|md|txt)$`
- Everything else → 404 (including `/`, `/index.html`, dotfiles, uppercase UUIDs, non-whitelist extensions)
- Directory listing off

## Architecture

| Component | Value |
|---|---|
| Image | `nginx:alpine` |
| Data mount | `/data_static/share-hosting/` → `/usr/share/nginx/html` (read-only) |
| Network | `nginx-proxy` bridge |
| TLS / vhost | `nginx-proxy` + `nginx-proxy-acme` via `VIRTUAL_HOST` env var |
| Subdomain | `share.ai.jingtao.fun` |

## Deploy

```bash
cd ~/ai-learn/projects/share-hosting
docker compose up -d
```

First request after fresh deploy may take 5-30s while letsencrypt-companion fetches a cert.

## Usage

```python
import uuid, shutil, os
uid = str(uuid.uuid4())
dst = f"/data_static/share-hosting/{uid}.html"
shutil.copy(local_path, dst)
os.chmod(dst, 0o644)  # nginx runs as a different user — must be world-readable
print(f"https://share.ai.jingtao.fun/{uid}.html")
```

See `~/.hermes/skills/devops/share-hosting/SKILL.md` for the full agent workflow.

## Related

- **licai** (`licai.ai.jingtao.fun`): Dedicated dashboard subdomain for wealth-products archive (same nginx pattern, structured paths instead of UUID).
