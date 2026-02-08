# VM

Server infrastructure — everything that runs on a virtual machine.

## Structure

| Folder | Description |
| --- | --- |
| `scripts/` | Server initialization scripts (swap, software install, Docker, env vars) |
| `docker-services/` | Docker Compose services organized by category |
| `host-services/` | Services deployed directly on the VM host (non-Docker) |
