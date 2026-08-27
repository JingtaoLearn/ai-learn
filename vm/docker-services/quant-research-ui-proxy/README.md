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
only from the SSH endpoint at `127.0.0.1`; nginx replaces, rather than appends, forwarding values.
