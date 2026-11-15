#!/usr/bin/env bash
# zhlearn 127.0.0.1:18095 -> (ailearn) -> Feng 127.0.0.1:18095. Exits if either leg drops.
set -euo pipefail
opts=(-N -T -o BatchMode=yes -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3)
ssh "${opts[@]}" -L 127.0.0.1:18095:127.0.0.1:18095 feng-learn &
ssh "${opts[@]}" -R 127.0.0.1:18095:127.0.0.1:18095 zhlearn &
wait -n
kill 0
