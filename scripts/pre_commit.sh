#!/bin/sh
# pre-commit hook: commits carry the project identity, a UTC date, name no place,
# and no secrets.
# Install:  ln -sf ../../scripts/pre_commit.sh .git/hooks/pre-commit
want="Emma <hello@theemmafamily.com>"
for who in GIT_AUTHOR_IDENT GIT_COMMITTER_IDENT; do
  got=$(git var "$who" | sed 's/ [0-9]* [-+][0-9]*$//')
  if [ "$got" != "$want" ]; then
    echo "pre-commit: $who is '$got', expected '$want'" >&2
    echo "  git config user.name Emma; git config user.email hello@theemmafamily.com" >&2
    exit 1
  fi
  # a local offset in the date is a location hint (AGENTS.md rule 2)
  case $(git var "$who") in
    *" +0000") ;;
    *) echo "pre-commit: $who has a non-UTC offset; commit with TZ=UTC git commit ..." >&2
       exit 1 ;;
  esac
done
top=$(git rev-parse --show-toplevel)
python3 "$top/scripts/check_public_copy.py" --staged || exit 1
exec "$top/scripts/check_secrets.sh"
