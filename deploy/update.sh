#!/usr/bin/env bash
#
# Pull the latest code and restart the bot. Run ON THE SERVER:
#
#   cd ~/christofgod_bot && ./deploy/update.sh
#
# Optionally pin a release:  ./deploy/update.sh v0.9.0
#
# Safe by design: it refuses to deploy a working tree with local edits, and
# verifies the new code imports before restarting, so a broken pull leaves the
# running bot alone rather than taking it down.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$REPO_DIR/.venv"
SERVICE_NAME="christofgod-bot"
TARGET="${1:-}"

cd "$REPO_DIR"

if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    echo "Working tree has local modifications — refusing to update."
    git status --short --untracked-files=no
    exit 1
fi

echo "==> Fetching"
git fetch --tags --quiet origin

previous="$(git rev-parse --short HEAD)"
if [ -n "$TARGET" ]; then
    echo "==> Checking out $TARGET"
    git checkout --quiet "$TARGET"
else
    echo "==> Fast-forwarding main"
    git checkout --quiet main
    git merge --ff-only --quiet origin/main
fi
current="$(git rev-parse --short HEAD)"

if [ "$previous" = "$current" ]; then
    echo "Already up to date ($current); nothing to do."
    exit 0
fi
echo "    $previous -> $current"

echo "==> Installing dependencies"
"$VENV/bin/pip" install -q -r requirements.txt

# Import check before restarting: catches a missing dependency or a syntax
# error while the old process is still happily running.
echo "==> Verifying the new code imports"
if ! "$VENV/bin/python" -c "import bot" > /dev/null; then
    echo
    echo "New code fails to import — rolling back to $previous and leaving the"
    echo "running bot untouched."
    # reset, not checkout: `git checkout <sha>` would leave a detached HEAD, and
    # the next run would then see main already at origin/main, report "already
    # up to date", and quietly leave the broken files in place.
    git reset --hard --quiet "$previous"
    echo "Rolled back. Fix the problem, push, then re-run ./deploy/update.sh"
    exit 1
fi

echo "==> Restarting $SERVICE_NAME"
sudo systemctl restart "$SERVICE_NAME"
sleep 3

if systemctl is-active --quiet "$SERVICE_NAME"; then
    echo "Bot is running at $current."
else
    echo "Bot did NOT come back up. Recent log:"
    journalctl -u "$SERVICE_NAME" --no-pager --lines=30
    exit 1
fi
