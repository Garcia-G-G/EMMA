#!/bin/sh
# commit-msg hook: reject tool attribution and the old "Prompt NN" naming.
# Install:  ln -sf ../../scripts/check_commit_msg.sh .git/hooks/commit-msg
msg="$1"
if grep -qiE '^\s*Co-Authored-By:.*(claude|anthropic|codex|copilot|gpt|openai)' "$msg"; then
  echo "commit-msg: tool attribution trailer is not allowed" >&2; exit 1
fi
if grep -qE '\bPrompts? [0-9]' "$msg"; then
  echo "commit-msg: use 'Step NN', not 'Prompt NN'" >&2; exit 1
fi
exit 0
