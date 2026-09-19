#!/usr/bin/env bash
# Verify nothing secret is about to be committed, then set up the repo.
#
#   bash scripts/init_repo.sh
#
# Run it from the repo root. It refuses to proceed if it finds a credential.
set -euo pipefail

echo "scanning for credentials..."

# Google AI Studio keys, AWS access key ids, and generic long secrets.
PATTERNS='AIza[0-9A-Za-z_\-]{30,}|AKIA[0-9A-Z]{16}|aws_secret_access_key|-----BEGIN [A-Z ]*PRIVATE KEY-----'

FOUND=$(grep -rIEn "$PATTERNS" . \
  --exclude-dir=.git \
  --exclude-dir=node_modules \
  --exclude-dir=.venv \
  --exclude-dir=.next \
  --exclude-dir=.aws-sam \
  --exclude-dir=out \
  --exclude=init_repo.sh 2>/dev/null || true)

if [ -n "$FOUND" ]; then
  echo
  echo "REFUSING TO CONTINUE -- possible credentials found:" >&2
  echo "$FOUND" >&2
  echo >&2
  echo "Remove them, then run this again." >&2
  exit 1
fi
echo "  clean"

for f in env.json infra/env.json samconfig.toml web/.env.local body.json; do
  if [ -f "$f" ] && ! grep -qF "$(basename "$f")" .gitignore; then
    echo "WARNING: $f exists and may not be ignored" >&2
  fi
done

if [ ! -d .git ]; then
  git init -b main
fi

git add -A
echo
echo "files staged: $(git diff --cached --name-only | wc -l)"
echo "fixture size: $(du -sh evals/fixtures 2>/dev/null | cut -f1 || echo 0)"
echo
echo "Sanity check before committing -- none of these should appear:"
git diff --cached --name-only | grep -E "\.env|env\.json|samconfig|node_modules|\.venv" || echo "  (clean)"
echo
echo "Next:"
echo "  git commit -m 'a11y-agent: agentic accessibility remediation'"
echo "  gh repo create a11y-agent --public --source=. --push"
