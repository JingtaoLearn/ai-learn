# VM Initialization Scripts

Numbered shell scripts for server provisioning. Run them in order on a fresh Ubuntu VM.

## Scripts

| Script | Description | Requires Root |
| --- | --- | --- |
| `00-set-swap.sh` | Configure swap space | Yes |
| `01-install-software.sh` | Install basic system tools (htop, vim, git, etc.), Python pip, pipx, and GitHub CLI | Yes |
| `02-install-docker.sh` | Install Docker CE and Docker Compose V2 plugin | Yes |
| `03-set-env.sh` | Set server environment variables in `/etc/environment` | Yes |
| `04-install-python-tools.sh` | Install Python tools (router-maestro) using pipx | No |

## Usage

```bash
# Run system scripts as root
sudo bash 00-set-swap.sh --size 4096
sudo bash 01-install-software.sh
sudo bash 02-install-docker.sh
sudo bash 03-set-env.sh --domain example.com --email admin@example.com

# Run user-level scripts as normal user
bash 04-install-python-tools.sh
```

## Installed Tools

**System-level (01-install-software.sh):**
- Basic tools: htop, vim, git, curl, etc.
- `python3-pip` - Python package installer
- `pipx` - Python applications in isolated environments
- `gh` - GitHub CLI

**User-level (04-install-python-tools.sh):**
- `router-maestro` - Multi-model LLM router with OpenAI-compatible API

## Verification

```bash
# Verify installations
gh --version
python3 -m pip --version
pipx --version
router-maestro --version
```

## Shared Library

`lib/common.sh` provides shared functions used by all scripts:

- `require_root` — exit if not running as root
- `check_flag <name>` — check if a step has already been executed
- `set_flag <name>` — mark a step as completed
