#!/usr/bin/env bash
# Fails if staged changes (or, with --all, tracked files) contain something that looks like a key.
set -euo pipefail
PATTERN='(\bsk[-_][A-Za-z0-9]{16,}|api_key[[:space:]]*=[[:space:]]*["'"'"']?[A-Za-z0-9_-]{12,})'
if [ "${1:-}" = "--all" ]; then
  hits=$(git ls-files -z | xargs -0 grep -InE -i "$PATTERN" -- 2>/dev/null | grep -v '^scripts/check_secrets.sh' || true)
else
  hits=$(git diff --cached -U0 | grep -E '^\+' | grep -v '^+++' | grep -E -i "$PATTERN" || true)
fi
if [ -n "$hits" ]; then
  echo "Possible secret detected:" >&2
  echo "$hits" >&2
  exit 1
fi
