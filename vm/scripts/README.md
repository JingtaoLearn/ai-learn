# VM Initialization Scripts

Numbered shell scripts for server provisioning. Run them in order on a fresh Ubuntu VM.

## Scripts

| Script | Description |
| --- | --- |
| `00-set-swap.sh` | Configure swap space |
| `01-install-software.sh` | Install basic system tools (htop, vim, git, etc.) |
| `02-install-docker.sh` | Install Docker CE and Docker Compose V2 plugin |
| `03-set-env.sh` | Set server environment variables in `/etc/environment` |

## Usage

```bash
# Run as root
sudo bash 00-set-swap.sh --size 4096
sudo bash 01-install-software.sh
sudo bash 02-install-docker.sh
sudo bash 03-set-env.sh --domain example.com --email admin@example.com
```

## Shared Library

`lib/common.sh` provides shared functions used by all scripts:

- `require_root` — exit if not running as root
- `check_flag <name>` — check if a step has already been executed
- `set_flag <name>` — mark a step as completed
