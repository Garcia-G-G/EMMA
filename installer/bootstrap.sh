#!/usr/bin/env bash
# Bootstrap an Emma development environment.
set -euo pipefail
cd "$(dirname "$0")/.."

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required. Install with: brew install uv" >&2
  exit 1
fi

uv sync
echo "Bootstrap complete. Next: copy .env.example to .env and fill in keys."
