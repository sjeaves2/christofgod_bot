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
# Bound the log search to this restart, so an earlier run's success cannot be
# mistaken for this one's.
restart_at="$(date '+%Y-%m-%d %H:%M:%S')"
sudo systemctl restart "$SERVICE_NAME"

if ! systemctl is-active --quiet "$SERVICE_NAME"; then
    echo "Bot did NOT come back up. Recent log:"
    journalctl -u "$SERVICE_NAME" --no-pager --lines=30
    exit 1
fi

# `systemctl is-active` is NOT evidence that the bot works. During the
# 2026-09-04 outage it reported "active" for 87 minutes while DNS was dead and
# the bot sat retrying an unresolvable hostname, reaching nobody. The process
# was alive and useless. Wait for proof it is actually talking to Telegram.
echo "==> Waiting for Telegram polling to start"
polling=""
for _ in $(seq 1 20); do
    if journalctl -u "$SERVICE_NAME" --since "$restart_at" --no-pager 2>/dev/null \
         | grep -q "Long polling started"; then
        polling=yes
        break
    fi
    sleep 1
done

if [ -z "$polling" ]; then
    echo
    echo "The service is running but never reported 'Long polling started'."
    echo "It may be unable to reach api.telegram.org — check DNS and the token."
    echo "The bot has NOT been rolled back; it is up but possibly not receiving."
    echo
    echo "One false alarm to rule out: that line is logged at INFO, so a"
    echo "config/config.yaml with log.level above INFO hides it and this check"
    echo "fails on a perfectly healthy bot."
    journalctl -u "$SERVICE_NAME" --since "$restart_at" --no-pager --lines=30
    exit 1
fi

echo "Bot is running at $current and polling Telegram."

# Surface a config problem the bot reports at startup. Not a deploy failure —
# the bot runs fine — but it means some upcoming service has a placeholder or
# missing join link, and the admin DM is easy to miss.
if journalctl -u "$SERVICE_NAME" --since "$restart_at" --no-pager 2>/dev/null \
     | grep -i "unusable join link"; then
    echo
    echo "^ Fix these with /setservicelink, then run /backup."
fi
