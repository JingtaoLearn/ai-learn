# Git Development Workflow

Standard git workflow for this repository. All changes to `main` must go through Pull Requests.

## Branch Naming

```
feat/YYYY-MM-DD-feature-name
```

Examples:
- `feat/2026-02-08-git-workflow-skill`
- `feat/2026-03-15-add-monitoring-service`

## Workflow

### 1. Start a new feature

Make sure you are on an up-to-date `main` branch, then create a feature branch:

```bash
git checkout main
git pull
git checkout -b feat/YYYY-MM-DD-feature-name
```

### 2. Develop and commit

Make changes, then commit with clear messages in imperative mood:

```bash
git add <files>
git commit -m "Add git workflow skill"
```

### 3. Push and create a Pull Request

```bash
git push -u origin feat/YYYY-MM-DD-feature-name
gh pr create --title "Add git workflow skill" --body "Summary of changes"
```

### 4. After PR is merged

Switch back to `main`, pull the latest changes, and clean up the feature branch:

```bash
git checkout main
git pull
git branch -d feat/YYYY-MM-DD-feature-name
```

The remote branch is automatically deleted on merge (GitHub setting).

## Rules

- **Never** push directly to `main` — it is protected by a branch ruleset
- One feature per branch
- Keep PRs focused and small when possible
- Use squash merge to keep `main` history clean
