#!/usr/bin/env bash
#
# Copy the gitignored config and data from this laptop to the server.
# Run ON YOUR LAPTOP, from the repository root:
#
#   ./deploy/migrate.sh ubuntu@<server-ip>
#
# Copies:  config/config.yaml, config/admins.yaml, config/officials.yaml, data/
# Skips:   logs/  — deliberately. logs/bot.log has historically contained the
#          live bot token, and logs/bot_activity.log is polluted with test-run
#          entries that would distort /stats on the new host. Starting with
#          empty logs is the point.

set -euo pipefail

REMOTE="${1:-}"
REMOTE_DIR="${2:-christofgod_bot}"

if [ -z "$REMOTE" ]; then
    echo "Usage: ./deploy/migrate.sh user@host [remote-dir]"
    echo "   e.g. ./deploy/migrate.sh ubuntu@203.0.113.10"
    exit 1
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

for f in config/config.yaml config/admins.yaml config/officials.yaml; do
    if [ ! -f "$f" ]; then
        echo "Missing $f — nothing to migrate. Aborting."
        exit 1
    fi
done

echo "==> Files to copy"
du -sh config data 2>/dev/null || true
echo
echo "    NOT copying logs/ (see the note at the top of this script)."
echo

read -r -p "Copy to ${REMOTE}:${REMOTE_DIR}/ ? [y/N] " reply
case "$reply" in
    [yY]|[yY][eE][sS]) ;;
    *) echo "Aborted."; exit 1 ;;
esac

echo "==> Ensuring the remote directories exist"
ssh "$REMOTE" "mkdir -p '$REMOTE_DIR/config' '$REMOTE_DIR/data'"

echo "==> Copying config"
scp config/config.yaml config/admins.yaml config/officials.yaml "$REMOTE:$REMOTE_DIR/config/"

echo "==> Copying data"
# The data directory is small (tens of KB); -r keeps it simple and dependency-free.
scp -r data/. "$REMOTE:$REMOTE_DIR/data/"

echo "==> Tightening permissions on the token-bearing config"
ssh "$REMOTE" "chmod 600 '$REMOTE_DIR/config/config.yaml'"

cat <<EOF

Migration complete.

Next, on the server:
  cd $REMOTE_DIR && ./deploy/setup.sh

Reminder: stop the bot on this laptop before starting it on the server.
Two instances polling the same token cause Telegram "Conflict" errors — the
bot now alerts ops admins about those immediately.
EOF
