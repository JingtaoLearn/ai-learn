# share-hosting

Self-hosted static file share with UUID-only URLs. Scan-resistant: directory listing disabled, root returns 404, only whitelisted extensions served.

**Domain:** `share.${S_DOMAIN}` (e.g. `share.ai.jingtao.fun`)
**Allowed extensions:** `.html`, `.pdf`, `.md`, `.txt`
**Path format:** `/<uuid-v4>.<ext>` only — anything else → 404

## Deploy

```bash
cd vm/docker-services/share-hosting
docker compose up -d
```

Static files go in `${S_CONTAINER_FOLDER_STATIC}/share-hosting/` on the host.

## Share a file

```bash
UID=$(uuidgen | tr 'A-Z' 'a-z')
cp report.html /data_static/share-hosting/${UID}.html
echo "https://share.ai.jingtao.fun/${UID}.html"
```

URL is live immediately — no restart needed.

## Security properties

| Test | Result |
|---|---|
| `GET /<valid-uuid>.html` | 200 |
| `GET /` | 404 |
| `GET /index.html` (guess) | 404 |
| Uppercase UUID | 404 (case-sensitive) |
| Non-whitelist extension | 404 |
| Hidden files (`.env`) | denied |
| Directory listing | off (`autoindex off`) |
| Server version banner | hidden (`server_tokens off`) |

UUID v4 has 122 bits of entropy — brute-force enumeration is infeasible.

## Nginx gotcha

The `location ~* "..."` regex **must be quoted** — nginx otherwise parses `{8}` as a config block delimiter and fails with `unknown directive "8}-[0-9a-f]"`. Already handled in `nginx.conf`.

## When NOT to use this

- Images for chat/embed → use `image-hosting` (optimized for image MIME + long cache)
- Quick text/code paste → use `paste-services`
- Files needing pickup code / expiry UI → use a FileCodeBox-style service
