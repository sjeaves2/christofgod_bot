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
#
# Everything lives inside main(), called on the last line. That is not style.
# This script CHECKS OUT NEW CODE OVER ITSELF partway through, and bash reads a
# script incrementally, by byte offset — so a release that changes this file
# can make the running shell resume at the wrong offset and execute nonsense,
# mid-deploy, as root-ish. Wrapping the body in a function forces bash to parse
# the whole thing before any of it runs, so the version that started is the
# version that finishes. Do not unwrap this.

set -euo pipefail

main() {

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

# --upgrade-strategy eager, not plain --upgrade. pip upgrades only the
# packages NAMED in the file by default (--upgrade-strategy only-if-needed),
# so a transitive dependency freezes at whatever was current when it was first
# installed and never moves again. On 2026-10-20 that left the server on
# certifi from July — the CA bundle httpx verifies Telegram with — while CI,
# which installs fresh every run, had been testing against October's. Plain
# --upgrade moved pytz (named in the file) and nothing else.
#
# The effect of this line is that the server ends up with what a fresh install
# resolves to, which is what CI tested. The cost is that a release published
# after CI ran can land here untested; the import check below is what catches
# it, and a lockfile is the heavier answer if that ever bites.
echo "==> Installing dependencies"
"$VENV/bin/pip" install -q --upgrade --upgrade-strategy eager -r requirements.txt

# pip install never REMOVES. A dependency dropped from requirements.txt stays
# in the venv for good, and keeps its advisories with it — removing
# deep-translator on 2026-10-13 would otherwise have left requests, urllib3 and
# beautifulsoup4 behind, carrying the four CVEs the removal was meant to clear.
echo "==> Removing packages requirements.txt no longer needs"
orphans="$("$VENV/bin/python" deploy/prune_venv.py requirements.txt || true)"
if [ -n "$orphans" ]; then
    echo "$orphans" | sed "s/^/    - /"
    # shellcheck disable=SC2086
    "$VENV/bin/pip" uninstall -q -y $orphans || true
else
    echo "    nothing to remove"
fi

# Import check before restarting: catches a missing dependency or a syntax
# error while the old process is still happily running. It is also what
# verifies the pruning above — anything wrongly removed shows up here, before
# the running bot is touched.
echo "==> Verifying the new code imports"
if ! "$VENV/bin/python" -c "import bot" > /dev/null; then
    echo
    echo "New code fails to import — rolling back to $previous and leaving the"
    echo "running bot untouched."
    # reset, not checkout: `git checkout <sha>` would leave a detached HEAD, and
    # the next run would then see main already at origin/main, report "already
    # up to date", and quietly leave the broken files in place.
    git reset --hard --quiet "$previous"
    # A git reset does not undo an uninstall, so put the venv back too. Without
    # this, a bad prune would leave the bot unable to start on the NEXT restart
    # even though the code had been rolled back.
    #
    # NB what this does NOT do: it reinstalls anything missing, but it cannot
    # DOWNGRADE a package the eager upgrade above moved — pip is not told which
    # versions were there before. The running bot is safe either way, because
    # the import check happens before the restart and the old process keeps its
    # already-loaded modules. The exposure is the NEXT restart, which would pair
    # rolled-back code with upgraded packages. If that ever happens, pin the
    # offending package in requirements.txt and deploy again.
    echo "    restoring the virtualenv"
    "$VENV/bin/pip" install -q -r requirements.txt || true
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
# Wait for EITHER marker, and prefer "Application started".
#
# v0.22.1 widened this wait from 20s to 60s after a healthy deploy reported
# failure. That stopped the false alarms but treated a symptom: profiling on
# 2026-10-29 showed the bot is up and polling in about EIGHT seconds, and that
# "Long polling started" is not the moment polling starts at all.
#
# That line comes from this project's own httpx log filter, which fires on the
# first completed HTTP request. For an idle bot the first getUpdates is a LONG
# POLL, so the request does not complete until its timeout expires with no
# updates — measured 25.4s after polling had begun on one restart and 11s on
# another. It lags by a Telegram-controlled, variable amount.
#
# "Application started" is python-telegram-bot's own, and _application.py runs
# the updater coroutine BEFORE it:
#     loop.run_until_complete(updater_coroutine)   # start_polling completes
#     loop.run_until_complete(self.start())        # logs "Application started"
# So that line cannot appear until polling is actually running. It arrived at
# 8.19s on the restart measured.
#
# Both are logged at INFO, so a config/config.yaml with log.level above INFO
# hides both — hence the note in the failure message below.
#
# 60s is kept, but it is now generous headroom rather than a budget being
# squeezed: nothing is rolled back on this check either way, and the cost of
# being too short is a false failure that trains the reader to ignore a real
# one, which is exactly what this check exists to prevent.
POLL_WAIT_SECONDS=60
POLL_MARKERS="Application started|Long polling started"

echo "==> Waiting for the bot to start polling (up to ${POLL_WAIT_SECONDS}s)"
polling=""
for _ in $(seq 1 "$POLL_WAIT_SECONDS"); do
    if journalctl -u "$SERVICE_NAME" --since "$restart_at" --no-pager 2>/dev/null \
         | grep -qE "$POLL_MARKERS"; then
        polling=yes
        break
    fi
    sleep 1
done

if [ -z "$polling" ]; then
    echo
    echo "The service is running but never reported 'Application started' or"
    echo "'Long polling started' within ${POLL_WAIT_SECONDS}s."
    echo "It may be unable to reach api.telegram.org — check DNS and the token."
    echo "The bot has NOT been rolled back; it is up but possibly not receiving."
    echo
    echo "One false alarm to rule out: both lines are logged at INFO, so a"
    echo "config/config.yaml with log.level above INFO hides them and this check"
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
}

# Parsed in full before this line runs — see the note at the top.
main "$@"
