# OpenClaw "missing" Tag - Investigation Summary

## Problem

OpenClaw shows `maestro-anthropic` models with "missing" tag:

```
Model                                      Input      Ctx      Local Auth  Tags
maestro-anthropic/github-copilot/claude... -          -        -     -     default,missing
```

**Question:** Can we use a built-in provider to eliminate this tag?

## Root Cause

`maestro-anthropic` is a **custom provider** not in OpenClaw's built-in model registry (`@mariozechner/pi-ai` library). Built-in providers include: `anthropic`, `openai`, `openai-codex`, `github-copilot`, etc.

When OpenClaw lists models:
- Built-in providers: Loads metadata from registry → displays correctly
- Custom providers: No registry entry → shows "missing" tag

**However:** At runtime, models work perfectly. The "missing" tag is purely cosmetic.

## Attempted Solutions

### Option 1: Use `anthropic` provider with custom baseUrl

**Result:** ❌ Config validates, but OpenClaw bypasses baseUrl setting and connects to api.anthropic.com → 401 errors

### Option 2: Use `github-copilot` provider

**Result:** ❌ Requires OAuth authentication, incompatible with custom endpoints

### Option 3: Build API proxy

**Result:** ❌ OpenClaw's built-in provider implementations don't respect baseUrl overrides

## Conclusion

**Continue using `maestro-anthropic` custom provider.**

The "missing" tag is expected behavior and does not affect functionality:

- ✅ All API calls work correctly
- ✅ Model routing functions properly
- ✅ Fallback system operational
- ✅ Full feature parity

**Verification:**

```bash
# Models work perfectly
openclaw agent --to +number --message "test" --local

# Logs confirm correct usage
journalctl --user -u openclaw-gateway | grep "agent model"
# Output: agent model: maestro-anthropic/github-copilot/claude-opus-4.6-fast
```

## Recommendation

**Accept the "missing" tag.** It indicates you're using a custom API endpoint, which is exactly correct for this use case.

To eliminate the tag would require:
1. Contributing `maestro-anthropic` to upstream pi-ai library, or
2. Modifying OpenClaw to support true baseUrl overrides

Both require upstream changes and significant effort.
