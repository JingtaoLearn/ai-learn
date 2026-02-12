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

**Prefer worktrees** over `git checkout` for branch switching. Worktrees let you work on multiple branches simultaneously without stashing or losing context.

### 1. Start a new feature (with worktree)

From the main repository, create a worktree with a new feature branch:

```bash
cd ~/ai-learn
git pull  # ensure main is up to date
git worktree add ../ai-learn-<short-name> -b feat/YYYYMMDD-feature-name main
```

This creates a new directory `~/ai-learn-<short-name>` checked out to the new branch, while `~/ai-learn` stays on its current branch undisturbed.

**Naming convention for worktree directories:** `ai-learn-<short-name>` (sibling to the main repo).

### 2. Develop and commit

Work in the worktree directory:

```bash
cd ~/ai-learn-<short-name>
# make changes...
git add <files>
git commit -m "Add git workflow skill"
```

Commit with clear messages in imperative mood.

### 3. Push and create a Pull Request

```bash
git push -u origin feat/YYYYMMDD-feature-name
gh pr create --title "<concise title>" --body "<summary of changes>"
```

Before creating the PR, review all commits on the branch (`git log main..HEAD`) to understand the full scope of changes. Then:

- **Title**: A concise summary of all changes in the PR (under 70 characters, imperative mood)
- **Body**: Bullet-point list of the key changes, covering all commits — not just the latest one

### 4. After PR is merged

Remove the worktree and clean up:

```bash
cd ~/ai-learn
git worktree remove ../ai-learn-<short-name>
git branch -d feat/YYYYMMDD-feature-name
git pull  # update main
```

The remote branch is automatically deleted on merge (GitHub setting).

### Worktree quick reference

```bash
git worktree list                          # list all worktrees
git worktree add <path> -b <branch> main   # create worktree with new branch
git worktree add <path> <existing-branch>  # create worktree for existing branch
git worktree remove <path>                 # remove a worktree
git worktree prune                         # clean up stale references
```

**Rules:**
- A branch can only be checked out in one worktree at a time
- Always `remove` worktrees when done (don't just `rm -rf`)
- Worktrees share the same `.git` — commits are visible across all worktrees

### Fallback: classic checkout (no worktree)

If worktrees are impractical (e.g., simple one-off fix on same directory):

```bash
git checkout main && git pull
git checkout -b feat/YYYYMMDD-feature-name
# ... work, commit, push, PR ...
git checkout main && git pull
git branch -d feat/YYYYMMDD-feature-name
```

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
