# Git Hooks

This directory contains Git hooks for the repository.

## Available Hooks

### pre-commit

**Purpose**: Automatically sync OpenClaw config and prevent committing sensitive data using **Gitleaks**

**Features**:
1. **Auto-sync**: Copies `~/.openclaw/openclaw.json` to `vm/host-services/open-claw/openclaw.json` before commit
2. **Gitleaks scanning**: Professional secret detection using [Gitleaks](https://github.com/gitleaks/gitleaks) with 100+ built-in rules:
   - API keys (AWS, OpenAI, GitHub, etc.)
   - Tokens and passwords
   - Private keys (RSA, SSH, etc.)
   - Database credentials
   - OAuth tokens
   - And many more...
3. **Commit blocking**: Prevents commit if secrets are detected
4. **Detailed reports**: Shows file path, line number, and rule ID

**Example blocked commit**:
```
Finding:     {"apiKey": "REDACTED"}
Secret:      REDACTED
RuleID:      generic-api-key
File:        config.json
Line:        1

❌ SECRETS DETECTED!

Please review the findings above and:
  1. Remove the secrets from your code
  2. Use environment variables instead: ${VARIABLE_NAME}
  3. Add false positives to .gitleaksignore if needed
```

**Handling false positives**:
If Gitleaks incorrectly flags safe content, add the fingerprint to `.gitleaksignore`:
```bash
# Copy the fingerprint from Gitleaks output
echo "path/to/file.json:rule-id:42" >> .gitleaksignore
```

## Installation

### First Time Setup (or After Clone)

```bash
./vm/scripts/hooks/install-hooks.sh
```

This will copy all hooks from `vm/scripts/hooks/` to `.git/hooks/` and make them executable.

### Manual Installation

```bash
cp vm/scripts/hooks/pre-commit .git/hooks/pre-commit
chmod +x .git/hooks/pre-commit
```

## Usage

Hooks run automatically when you execute Git commands:

- `pre-commit` → runs on `git commit`

No manual action needed once installed.

## Updating Hooks

If hooks are updated in the repo, re-run the install script:

```bash
./vm/scripts/hooks/install-hooks.sh
```

## Bypassing Hooks (Not Recommended)

In rare cases where you need to bypass hooks:

```bash
git commit --no-verify
```

⚠️ **Warning**: This skips all safety checks. Use only if you know what you're doing.
