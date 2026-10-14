#!/bin/sh
# pre-commit hook: commits carry the project identity, and no secrets.
# Install:  ln -sf ../../scripts/pre_commit.sh .git/hooks/pre-commit
want="Emma <hello@theemmafamily.com>"
for who in GIT_AUTHOR_IDENT GIT_COMMITTER_IDENT; do
  got=$(git var "$who" | sed 's/ [0-9]* [-+][0-9]*$//')
  if [ "$got" != "$want" ]; then
    echo "pre-commit: $who is '$got', expected '$want'" >&2
    echo "  git config user.name Emma; git config user.email hello@theemmafamily.com" >&2
    exit 1
  fi
done
exec "$(git rev-parse --show-toplevel)/scripts/check_secrets.sh"
