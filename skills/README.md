# Skills

Reusable Claude Code skills organized by topic. Each skill is a directory containing a `SKILL.md` with YAML frontmatter (`name` and `description`) and optional bundled resources (`scripts/`, `references/`, `assets/`).

## Structure

```
skills/
├── install-skills.sh           # Symlinks all skills to ~/.claude/skills/
├── <skill-name>/
│   ├── SKILL.md                # Required: instructions with YAML frontmatter
│   ├── scripts/                # Optional: executable helper scripts
│   ├── references/             # Optional: reference documentation
│   └── assets/                 # Optional: templates, images, fonts
└── README.md
```

## Installation

Run the install script to create symlinks from `~/.claude/skills/` to this repository:

```bash
./skills/install-skills.sh
```

The script automatically discovers all directories containing a `SKILL.md` and symlinks them. It is idempotent and safe to re-run.

## Available Skills

| Skill | Description |
|-------|-------------|
| [git-workflow](git-workflow/) | Standard git development workflow for the ai-learn repository |

## Adding a New Skill

1. Create a directory: `skills/<skill-name>/`
2. Add a `SKILL.md` with YAML frontmatter:
   ```yaml
   ---
   name: skill-name
   description: What this skill does and when to use it.
   ---
   ```
3. Add optional resource directories (`scripts/`, `references/`, `assets/`)
4. Run `./skills/install-skills.sh` to install
