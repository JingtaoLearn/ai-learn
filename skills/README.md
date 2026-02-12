# Skills

Reusable Claude Code skills organized by topic. Each skill provides specialized knowledge, workflows, and automation scripts that extend Claude Code's capabilities.

## What are Skills?

Skills are packages of instructions and resources that Claude Code can use to perform specific tasks. Each skill contains:
- **SKILL.md** - Instructions with YAML frontmatter (name, description)
- **scripts/** - Optional executable helper scripts
- **references/** - Optional reference documentation
- **assets/** - Optional templates, images, fonts

## Directory Structure

```
skills/
├── install-skills.sh           # Symlinks all skills to ~/.claude/skills/
├── <skill-name>/
│   ├── SKILL.md                # Required: instructions with YAML frontmatter
│   ├── scripts/                # Optional: executable helper scripts
│   ├── references/             # Optional: reference documentation
│   └── assets/                 # Optional: templates, images, fonts
└── README.md                   # This file
```

## Installation

Run the install script to create symlinks from `~/.claude/skills/` to this repository:

```bash
cd skills/
./install-skills.sh
```

The script:
- Automatically discovers all directories containing a `SKILL.md`
- Creates symlinks in `~/.claude/skills/`
- Is idempotent and safe to re-run

## Available Skills

| Skill | Description | Documentation |
|-------|-------------|---------------|
| [git-workflow](git-workflow/) | Standard git development workflow for the ai-learn repository | [SKILL.md](git-workflow/SKILL.md) |

## Using Skills

Once installed, Claude Code automatically loads skills and uses them when appropriate. You can also invoke skills explicitly:

```bash
# Claude Code recognizes relevant contexts
claude "Create a new feature branch"  # Uses git-workflow skill

# Or invoke directly
claude "/git-workflow"
```

## Adding a New Skill

1. Create a directory:
```bash
mkdir skills/<skill-name>
```

2. Add a `SKILL.md` with YAML frontmatter:
```yaml
---
name: skill-name
description: What this skill does and when to use it.
---

# Skill Instructions

[Your skill content here...]
```

3. Add optional resource directories:
```bash
mkdir skills/<skill-name>/scripts
mkdir skills/<skill-name>/references
mkdir skills/<skill-name>/assets
```

4. Install the skill:
```bash
./skills/install-skills.sh
```

5. Update this README's skills table

## Best Practices

- **Clear naming** - Use descriptive, hyphenated names
- **Focused scope** - Each skill should address one specific task or workflow
- **Good documentation** - Include examples and use cases in SKILL.md
- **Reusable scripts** - Make scripts parameterized and reusable
- **Version control** - Keep skills in this repo for team sharing

## Next Steps

- **Repository Conventions**: See [../CLAUDE.md](../CLAUDE.md) for repository guidelines
- **Git Workflow**: Use [git-workflow/](git-workflow/) skill for this repo's git practices

---

[← Back to Repository Root](../)
