# Quant Research UI Proxy

This pre-built `nginx:alpine` sidecar publishes `quant.ai.jingtao.fun` through the existing
nginx-proxy network. It has no host port. Its only upstream is the encrypted tunnel bound to the
exact bridge gateway resolved by the ailearn tunnel service.

Start it only with the tunnel-generated environment file:

```bash
docker compose --env-file /run/user/$(id -u)/quant-research-gateway.env config
docker compose --env-file /run/user/$(id -u)/quant-research-gateway.env up -d
```

The upstream is the Trend Sentinel app ([`projects/quant-research-platform`](../../../projects/quant-research-platform/))
bound to zhlearn `127.0.0.1:18090`; the tunnel forwards the gateway port to it. The sidecar substitutes the tunnel-generated gateway
directly into both its upstream and trusted-proxy directive; there is no `host-gateway` alias or
second lookup. It accepts `X-Real-IP` only from that gateway and replaces, rather than appends,
forwarding values. The container healthcheck sends the public Host through nginx, the encrypted
tunnel, and the application `/health`; a local synthetic nginx response cannot satisfy it.

After the services are running, execute the same end-to-end acceptance probe from inside the proxy:

```bash
./check-health.sh /run/user/$(id -u)/quant-research-gateway.env
```

The probe accepts only the app's exact `{"status":"ok"}` application response.
