#!/bin/sh
# Copy the design system (everything in design/ but preview.html) byte-for-byte
# into the two places that serve it: backend/static/ and _landing/ (its own repo,
# when checked out inside this one). tests/test_design_sync.py and
# _landing/.kamal/hooks/pre-build fail on drift.
set -eu
top=$(cd "$(dirname "$0")/.." && pwd)
cd "$top/design"
for dest in "$top/backend/static" "$top/_landing"; do
  [ -d "$dest" ] || continue
  find . -type f ! -name preview.html ! -name .DS_Store | while read -r f; do
    mkdir -p "$dest/$(dirname "$f")"
    cp "$f" "$dest/$f"
  done
  echo "design synced -> $dest"
done
