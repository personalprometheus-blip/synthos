#!/bin/bash
# qpush — push current synthos/ changes to GitHub in one command
# Usage: qpush
#        qpush "optional commit message"

cd ~/synthos 2>/dev/null || { echo "Error: ~/synthos not found"; exit 1; }

if git diff --quiet && git diff --staged --quiet; then
  echo "Nothing to push — working tree clean"
  exit 0
fi

MSG="${1:-Update $(date '+%Y-%m-%d %H:%M')}"
git add -A
git commit -m "$MSG"

if git push; then
  echo ""
  echo "✓ Pushed to GitHub"
else
  echo ""
  echo "✗ Push failed — check output above"
  exit 1
fi
