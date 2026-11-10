#!/usr/bin/env bash
#
# Daily snapshot of the UNTRACKED files in config/.
#
# Run by christofgod-config-backup.timer, or by hand:
#
#   ./deploy/backup-config.sh
#
# WHY THIS EXISTS
#
# The bot's own nightly backup (handlers/backup.py) is deliberately data/ only:
# config/config.yaml holds the live bot token, and admins.yaml and
# officials.yaml name real people, so none of the three may be sent through
# Telegram. That left them with NO backup at all.
#
# Found out the hard way on 2026-11-10. A `git checkout main` on the server —
# where the local `main` was months-stale pre-rewrite history that still
# TRACKED those two files — replaced them, and checking the release tag back
# out then deleted them. Both are gitignored, and git treats ignored files as
# expendable, so nothing warned and nothing failed. The only copy on the box
# was a one-off from 2026-11-04, so six days of changes went with them: an ops
# admin's `phone:` and an official's proxy `chat_id` binding. Both were
# reconstructed from data/users.yaml and the activity log, which worked only
# because those happen to record the same values.
#
# officials.yaml is also rewritten by the bot at RUNTIME (chat_id bindings from
# /sharecontact), so it drifts with no human edit to notice. That is why this
# runs daily rather than before risky operations.
#
# SCOPE
#
# Stays on this machine. These snapshots carry the bot token and personal data,
# so do not pipe them to Telegram, a chat, or off-box storage you do not
# control. Files are 0600 inside a 0700 directory.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$REPO_DIR/config"
# Overridable so a test run can write somewhere harmless.
DEST="${CONFIG_BACKUP_DIR:-$HOME/config-backups}"
KEEP_DAYS="${CONFIG_BACKUP_KEEP_DAYS:-14}"
OUT="$DEST/$(date +%F)"

mkdir -p "$OUT"
chmod 700 "$DEST" "$OUT"

shopt -s nullglob
copied=0
for f in "$SRC"/*.yaml; do
    base="$(basename "$f")"
    case "$base" in
        # Templates are tracked in git already, so they do not need a
        # snapshot. This is BELT AND BRACES: the `*.yaml` glob above already
        # cannot match `admins.yaml.example`, so deleting this line changes
        # nothing today and no test can kill it on its own — mutating the
        # glob alone does not show either, and only removing BOTH makes a
        # test fail (measured 2026-11-10). Kept because the glob is the
        # fragile half: widening it to pick up a `.yml` or a nested file is
        # an easy, reasonable-looking edit, and this line is what stops that
        # from quietly snapshotting the templates too.
        *.example) continue ;;
    esac
    cp -p "$f" "$OUT/$base"
    chmod 600 "$OUT/$base"
    copied=$((copied + 1))
done

if [ "$copied" -eq 0 ]; then
    # Never leave an empty dated directory behind: it looks exactly like a
    # good backup until the day someone needs it. A backup that reports
    # success with nothing in it is worse than no backup, because it stops
    # anyone looking for the real problem.
    rmdir "$OUT" 2>/dev/null || true
    echo "ERROR: no config files found in $SRC — nothing backed up" >&2
    exit 1
fi

# Manifest, so drift between snapshots is visible without diffing files that
# contain secrets.
#
# sha256sum is coreutils and absent on macOS; shasum ships with both. This
# only ever RUNS on the Linux server, but tests/test_config_backup.py executes
# this script for real on a developer's Mac too, and a test that has to be
# skipped on the machine where the code is written is a test that rots.
if command -v sha256sum > /dev/null 2>&1; then
    ( cd "$OUT" && sha256sum ./*.yaml > MANIFEST.sha256 )
else
    ( cd "$OUT" && shasum -a 256 ./*.yaml > MANIFEST.sha256 )
fi
chmod 600 "$OUT/MANIFEST.sha256"

# Retention. `-mtime +N` is strictly greater, so a snapshot exactly N days old
# is kept: KEEP_DAYS=14 retains 14 full days plus today. The name pattern
# limits the delete to directories this script created, so an unrelated
# directory under DEST is never removed.
find "$DEST" -mindepth 1 -maxdepth 1 -type d -name '20*-*-*' \
     -mtime +"$KEEP_DAYS" -exec rm -rf {} +

kept=$(find "$DEST" -mindepth 1 -maxdepth 1 -type d -name '20*-*-*' | wc -l)
echo "config backup ok: $copied file(s) -> $OUT; $kept snapshot(s) retained"
