# Quant Research UI Proxy

This pre-built `nginx:alpine` sidecar publishes `quant.ai.jingtao.fun` through the existing
nginx-proxy network. It has no host port. Its only upstream is the encrypted tunnel bound to the
exact bridge gateway resolved by the ailearn tunnel service.

Start it only with the tunnel-generated environment file:

```bash
docker compose --env-file /run/user/$(id -u)/quant-research-gateway.env config
docker compose --env-file /run/user/$(id -u)/quant-research-gateway.env up -d
```

The application itself remains bound to Feng `127.0.0.1:8090`. Uvicorn trusts forwarded headers
only from the SSH endpoint at `127.0.0.1`. The sidecar substitutes the tunnel-generated gateway
directly into both its upstream and trusted-proxy directive; there is no `host-gateway` alias or
second lookup. It accepts `X-Real-IP` only from that gateway and replaces, rather than appends,
forwarding values. The container healthcheck sends the public Host through nginx, the encrypted
tunnel, and Feng's application `/health`; a local synthetic nginx response cannot satisfy it.

After the services are running, execute the same end-to-end acceptance probe from inside the proxy:

```bash
./check-health.sh /run/user/$(id -u)/quant-research-gateway.env
```

The probe accepts only Feng's exact `{"status":"ok"}` application response.
