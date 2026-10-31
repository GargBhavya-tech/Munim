#!/bin/sh
# Start command for the hosted demo. MUNIM_MODE picks the backend: scripted (default, no models, Judge Mode will not reply), offline (rule-based stand-in, needs the ML image), llm (Sarvam, needs the ML image and SARVAM_API_KEY).
# MUNIM_ENGLISH=1 makes every reply English.
set -e
case "${MUNIM_MODE:-scripted}" in
  llm)     FLAG="--llm" ;;
  offline) FLAG="--offline" ;;
  *)       FLAG="" ;;
esac
[ "${MUNIM_ENGLISH:-0}" = "1" ] && FLAG="$FLAG --english"
exec python -m core.api.dev_server --host 0.0.0.0 --port "${PORT:-8000}" --static-dir web/dist $FLAG
