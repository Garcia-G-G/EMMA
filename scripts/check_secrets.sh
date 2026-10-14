#!/bin/sh
# Secret scan for commits, pushes and the tracked tree.
#   check_secrets.sh            staged changes  (pre-commit hook)
#   check_secrets.sh --tree     every tracked file (tests/test_no_secrets.py)
#   check_secrets.sh --push     refs on stdin, as git gives them (pre-push hook)
# Install:  ln -sf ../../scripts/check_secrets.sh .git/hooks/pre-commit
#           ln -sf ../../scripts/check_secrets.sh .git/hooks/pre-push
# Known test fixtures live in .secrets-allow, one exact matched value per line.
# Uses gitleaks for the staged scan when it is installed.
set -eu
top=$(git rev-parse --show-toplevel)
allow="$top/.secrets-allow"
[ -f "$allow" ] || allow=/dev/null

mode=staged
case "${0##*/}" in pre-push) mode=push ;; esac
case "${1:-}" in --tree) mode=tree ;; --push) mode=push ;; esac

# git diff/log -p  ->  file:line:added-content
diff_lines() {
  awk '
    /^\+\+\+ / { f = substr($0, 5); sub(/^b\//, "", f); next }
    /^@@ /     { split($3, a, ","); ln = substr(a[1], 2) + 0; next }
    /^\+/      { if (f != "/dev/null") print f ":" ln ":" substr($0, 2); ln++ }
  '
}

scan() {
  awk -v allowfile="$allow" '
    BEGIN {
      while ((getline l < allowfile) > 0) if (l !~ /^#/ && l != "") ok[l] = 1
      B = "(^|[^A-Za-z0-9_-])"
      n = 0
      p[++n] = B "sk-[A-Za-z0-9_-]{20,}";             name[n] = "OpenAI key"
      p[++n] = B "sk_[A-Za-z0-9]{20,}";               name[n] = "ElevenLabs/Stripe key"
      p[++n] = B "re_[A-Za-z0-9_]{20,}";              name[n] = "Resend key"
      p[++n] = B "ghp_[A-Za-z0-9]{30,}";              name[n] = "GitHub token"
      p[++n] = B "AKIA[0-9A-Z]{16}";                  name[n] = "AWS key"
      p[++n] = B "whsec_[A-Za-z0-9]{10,}";            name[n] = "webhook secret"
      p[++n] = B "AIza[0-9A-Za-z_-]{35}";             name[n] = "Google key"
      p[++n] = B "eyJ[A-Za-z0-9_-]{10,}\\.eyJ[A-Za-z0-9_-]{10,}\\.[A-Za-z0-9_-]{10,}"; name[n] = "JWT"
      p[++n] = "-----BEGIN [A-Z ]*PRIVATE KEY-----";  name[n] = "private key"
      p[++n] = "[Pp][Aa][Ss][Ss][Ww][Oo][Rr][Dd][ \t]*=[ \t]*[\"\047][^\"\047]{4,}"; name[n] = "literal password"
      bad = 0
    }
    {
      # file:line:content -> location and the text to scan
      i = index($0, ":"); j = index(substr($0, i + 1), ":")
      loc = substr($0, 1, i + j - 1); rest = substr($0, i + j + 1)
      for (k = 1; k <= n; k++) {
        s = rest
        while (match(s, p[k])) {
          tok = substr(s, RSTART, RLENGTH); s = substr(s, RSTART + RLENGTH)
          sub(/^[^A-Za-z0-9_-]/, "", tok)
          if (!(tok in ok)) {
            printf "secret scan: %s: %s (%s...)\n", loc, name[k], substr(tok, 1, 8)
            bad = 1
          }
        }
      }
    }
    END { exit bad }
  '
}

env_files() {
  grep -E '(^|/)\.env[^/]*$' | grep -vE '(^|/)\.env\.example$' |
    sed 's/^/secret scan: .env file must not be committed: /' || true
}

fail=0
case $mode in
  staged)
    if command -v gitleaks >/dev/null 2>&1; then
      gitleaks protect --staged --no-banner || fail=1
    fi
    git diff --cached -U0 --no-color --diff-filter=AM | diff_lines | scan || fail=1
    names=$(git diff --cached --name-only --diff-filter=AM | env_files)
    ;;
  tree)
    git -C "$top" grep -nIiE 'sk-|sk_|re_|ghp_|AKIA|whsec_|AIza|eyJ|PRIVATE KEY|password' |
      scan || fail=1
    names=$(git -C "$top" ls-files | env_files)
    ;;
  push)
    zero=0000000000000000000000000000000000000000
    names=
    while read -r _lref lsha _rref rsha; do
      [ "$lsha" = "$zero" ] && continue          # branch deletion
      if [ "$rsha" = "$zero" ] || ! git cat-file -e "$rsha" 2>/dev/null; then
        range="$lsha --not --remotes"
      else
        range="$rsha..$lsha"
      fi
      # every commit being pushed, so a secret added then removed still counts
      # shellcheck disable=SC2086
      git log -p -U0 --no-color --no-merges --format= $range | diff_lines | scan || fail=1
      # shellcheck disable=SC2086
      names="$names$(git log --name-only --diff-filter=AM --format= $range | env_files)"
    done
    ;;
esac
if [ -n "$names" ]; then echo "$names"; fail=1; fi
if [ $fail -ne 0 ]; then
  echo "secret scan: blocked. A key in a commit is a release blocker: rotate it first, then purge." >&2
  echo "A test fixture can be allowed by its exact value in .secrets-allow." >&2
fi
exit $fail
