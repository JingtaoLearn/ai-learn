---
name: git-workflow
description: Standard git development workflow for the ai-learn repository. Use when creating branches, committing code, pushing changes, or creating Pull Requests. Triggers on any git workflow question, branch naming, PR creation, or merge process within this project.
---

# Git Development Workflow

All changes to `main` must go through Pull Requests.

## Branch Naming

```
feat/YYYYMMDD-feature-name
```

Examples:
- `feat/20260208-git-workflow-skill`
- `feat/20260315-add-monitoring-service`

## Workflow

### 1. Start a new feature

From an up-to-date `main` branch, create a feature branch:

```bash
git checkout main
git pull
git checkout -b feat/YYYYMMDD-feature-name
```

### 2. Develop and commit

Commit with clear messages in imperative mood:

```bash
git add <files>
git commit -m "Add git workflow skill"
```

### 3. Push and create a Pull Request

```bash
git push -u origin feat/YYYYMMDD-feature-name
gh pr create --title "<concise title>" --body "<summary of changes>"
```

Before creating the PR, review all commits on the branch (`git log main..HEAD`) to understand the full scope of changes. Then:

- **Title**: A concise summary of all changes in the PR (under 70 characters, imperative mood)
- **Body**: Bullet-point list of the key changes, covering all commits — not just the latest one

### 4. After PR is merged

Switch back to `main`, pull latest, and clean up:

```bash
git checkout main
git pull
git branch -d feat/YYYYMMDD-feature-name
```

The remote branch is automatically deleted on merge (GitHub setting).

## Git Hooks

Before the first commit, verify the pre-commit hook is installed:

```bash
test -x .git/hooks/pre-commit && echo "Hook installed" || echo "Hook MISSING"
```

If the hook is missing, copy it from the repository or inform the user to set it up.

This repository has a `pre-commit` hook that runs automatically on every commit. It performs:

1. **OpenClaw config sync** -- copies system config into the repo if changed
2. **Gitleaks secret scanning** -- scans staged files for leaked secrets (API keys, tokens, passwords)

If the hook fails:

- **Secret detected**: Remove the secret from code, use environment variables instead, or add false positives to `.gitleaksignore`. Do NOT use `--no-verify` to bypass.
- **Gitleaks not installed**: Install it (`apt install gitleaks` or see https://github.com/gitleaks/gitleaks). Inform the user if the tool is missing.
- **Other errors**: Read the hook output carefully, fix the issue, re-stage files, and commit again. Always create a NEW commit after fixing -- never amend the previous one unless explicitly asked.

## Rules

- **Never** push directly to `main` -- it is protected by a branch ruleset
- One feature per branch
- Keep PRs focused and small when possible
- Use squash merge to keep `main` history clean
