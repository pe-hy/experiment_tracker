#!/usr/bin/env bash
# Install the /track-experiment skill for Claude Code, at user scope so it works
# from any project directory.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILLS="${CLAUDE_SKILLS_DIR:-$HOME/.claude/skills}"
TOKEN_FILE="${TRACKER_TOKEN_FILE:-$HOME/.config/exptracker/token}"

mkdir -p "$SKILLS"

# A symlink, so `git pull` in this repo updates the installed skill. Point it at
# the repo rather than copying, or the two drift apart silently.
if [ -e "$SKILLS/track-experiment" ] && [ ! -L "$SKILLS/track-experiment" ]; then
  echo "error: $SKILLS/track-experiment exists and is not a symlink." >&2
  echo "Move it aside first; refusing to overwrite a real directory." >&2
  exit 1
fi
ln -sfn "$REPO/skill/track-experiment" "$SKILLS/track-experiment"
echo "installed: $SKILLS/track-experiment -> $REPO/skill/track-experiment"

if [ ! -f "$TOKEN_FILE" ]; then
  echo ""
  echo "No token yet. Create a fine-grained PAT scoped to ONLY this tracker repo,"
  echo "with 'Contents: Read and write':"
  echo "    https://github.com/settings/personal-access-tokens/new"
  echo "then:"
  echo "    mkdir -p \"$(dirname "$TOKEN_FILE")\" && chmod 700 \"$(dirname "$TOKEN_FILE")\""
  echo "    printf '%s' '<token>' > \"$TOKEN_FILE\" && chmod 600 \"$TOKEN_FILE\""
else
  perms="$(stat -c '%a' "$TOKEN_FILE" 2>/dev/null || echo '?')"
  echo "token: $TOKEN_FILE (mode $perms)"
  [ "$perms" = "600" ] || echo "  warning: expected mode 600, run: chmod 600 $TOKEN_FILE"
fi

echo ""
echo "Check it works:   python3 $REPO/scripts/track.py --list"
echo "Then in any project, ask Claude to \"track this run\" or run /track-experiment."
