# Quant Research SSH Tunnel

This user service runs on **ailearn** and exposes Feng's loopback-only quant UI only on the
existing `nginx-proxy` bridge gateway. Traffic between ailearn and Feng is carried by SSH.

## Configuration

1. Copy `quant-research-tunnel.env.example` to
   `/home/ailearn/.config/quant-research-tunnel.env` and set the Feng SSH host. Do not place keys or
   secrets in the repository.
2. Install `quant-research-tunnel.service` as an ailearn systemd user unit.
3. Start the tunnel before the proxy Compose service.
4. The script writes the single inspected bridge address to
   `/run/user/<uid>/quant-research-gateway.env`. Start the proxy with that exact file:

   ```bash
   docker compose --env-file /run/user/$(id -u)/quant-research-gateway.env config
   docker compose --env-file /run/user/$(id -u)/quant-research-gateway.env up -d
   ```

The script fails if `nginx-proxy` has no gateway, multiple gateways, or an unsafe wildcard,
loopback, or reserved address. SSH uses strict host-key checking, batch mode, keepalives, and
`ExitOnForwardFailure`; it never falls back to another interface.
