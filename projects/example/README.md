# example

A minimal static-site demo deployed at `example.${S_DOMAIN}`.

Purpose: a capability demo — scaffold → containerize → deploy via nginx-proxy
with auto-HTTPS, end-to-end, in one shot.

## Stack

- nginx:alpine serving a single static HTML page
- nginx-proxy + acme-companion for VHOST routing and TLS

## Local dev

```bash
docker compose up -d --build
```

Then visit https://example.ai.jingtao.fun
