#!/usr/bin/env bash
set -euo pipefail

docker exec nginx-proxy nginx -s reload
