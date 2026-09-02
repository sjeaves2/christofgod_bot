"""Nightly backup of data/ — zipped and sent to ops admins by direct message.

The bot's YAML stores are the only copy of who has registered and what
appointments exist. On a single cloud VM, provider snapshots restore a whole
disk but do not let you open a file and check what was in it, so this sends a
small archive you can actually inspect (and restore from) each night.

A backup is only sent when data/ has actually changed since the last one, so a
message in your chat means something happened that night — with a heartbeat
copy every FORCE_AFTER_DAYS regardless, so continued silence still distinguishes
"nothing changed" from "the bot stopped working".

Scope is deliberately data/ only. config/config.yaml carries the live bot token
and must never be sent through Telegram; admins.yaml and officials.yaml are in
git and recoverable from there.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import zipfile
from datetime import datetime, time, timedelta

import storage
from common import now_tz
from error_reporting import ops_chat_ids
from permissions import admin_only, user_info
from settings import DATA_DIR, GEN_DIR, TZ, activity
from telegram import InputFile, Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

# 3am church time: after the day's activity, before anyone is awake to notice
# the brief disk read.
BACKUP_HOUR = 3
BACKUP_MINUTE = 0
BACKUP_TIME = time(hour=BACKUP_HOUR, minute=BACKUP_MINUTE, tzinfo=TZ)

# Telegram caps bot document uploads at 50 MB; data/ is measured in kilobytes,
# so this is a sanity rail rather than a real constraint.
MAX_UPLOAD_BYTES = 45 * 1024 * 1024

# Where the last backup's fingerprint is remembered. Deliberately NOT inside
# data/ — writing it there would itself count as a change every night.
STATE_FILE = GEN_DIR / "backup_state.json"

# Send anyway if this many days have passed with no change. Without it, "no
# backup arrived" would be ambiguous: either nothing changed, or the bot is
# broken. A periodic heartbeat keeps silence meaningful.
FORCE_AFTER_DAYS = 7


def data_fingerprint() -> "tuple[str, list[str]]":
    """SHA-256 over the *contents* of data/, plus the file names covered.

    Hashing contents rather than the zip matters: a zip embeds each file's
    modification time, so a file rewritten with identical content would change
    the archive bytes and look like new data when nothing had actually changed.
    """
    digest = hashlib.sha256()
    members: list[str] = []
    for path in sorted(DATA_DIR.glob("*")):
        if not path.is_file():
            continue
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
        members.append(path.name)
    return digest.hexdigest(), members


def load_backup_state() -> dict:
    """Last recorded fingerprint and send time; {} when there is no history."""
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_backup_state(fingerprint: str, when: datetime) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(
            json.dumps({"fingerprint": fingerprint, "sent_at": when.isoformat()}),
            encoding="utf-8")
    except OSError as exc:
        # Losing the state only means an extra backup next run — never fatal.
        logger.warning("Could not record backup state: %s", exc)


def backup_needed(fingerprint: str, now: "datetime | None" = None) -> "tuple[bool, str]":
    """Decide whether tonight's backup is worth sending; returns (needed, why)."""
    now = now or now_tz()
    state = load_backup_state()
    if not state:
        return True, "first backup"
    if state.get("fingerprint") != fingerprint:
        return True, "data changed"
    try:
        last = datetime.fromisoformat(state["sent_at"])
        if last.tzinfo is None:
            last = TZ.localize(last)
    except (KeyError, ValueError, TypeError):
        return True, "unreadable backup state"
    if now - last >= timedelta(days=FORCE_AFTER_DAYS):
        return True, f"unchanged, but {FORCE_AFTER_DAYS} days since the last copy"
    return False, f"no changes since {last.strftime('%Y-%m-%d')}"


def build_backup(now: "datetime | None" = None) -> "tuple[bytes, str, list[str]]":
    """Zip every file in data/ in memory.

    Returns (zip_bytes, filename, member_names). Built in memory because the
    archive is tiny and it avoids leaving a copy of the congregation's data
    lying around in /tmp.
    """
    now = now or now_tz()
    buffer = io.BytesIO()
    members: list[str] = []
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(DATA_DIR.glob("*")):
            if path.is_file():
                zf.write(path, arcname=f"data/{path.name}")
                members.append(path.name)
    payload = buffer.getvalue()
    filename = f"christofgod-data-{now.strftime('%Y-%m-%d')}.zip"
    return payload, filename, members


def verify_backup(payload: bytes) -> "str | None":
    """Return the name of the first corrupt member, or None if the zip is sound.

    A backup that cannot be opened is worse than no backup, because it looks
    like protection. Cheap to check at this size, so always check.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            return zf.testzip()
    except zipfile.BadZipFile:
        return "(archive unreadable)"


async def build_caption(members: list[str], size: int,
                        now: "datetime | None" = None) -> str:
    """A short summary so the contents can be sanity-checked without opening it."""
    now = now or now_tz()
    users = await storage.get_all_users()
    appts = await storage.get_appointments()
    anns = await storage.get_announcements()
    return (
        f"🗄 Nightly backup — {now.strftime('%Y-%m-%d %H:%M %Z')}\n"
        f"{len(members)} file(s), {size:,} bytes\n"
        f"{len(users)} user(s), {len(appts)} appointment(s), "
        f"{len(anns)} announcement(s)"
    )


async def send_backup(bot, now: "datetime | None" = None, force: bool = False) -> int:
    """Build, verify and DM the backup. Returns the number of admins reached.

    Skips sending when data/ is byte-for-byte identical to the last backup,
    unless *force* is set (manual /backup) or FORCE_AFTER_DAYS has elapsed.

    Never raises: a failing backup must not take the bot down with it. Failures
    are logged and recorded in the activity log so they show up in /stats.
    """
    now = now or now_tz()

    if not force:
        fingerprint, present = data_fingerprint()
        if present:
            needed, why = backup_needed(fingerprint, now)
            if not needed:
                logger.info("Backup skipped — %s.", why)
                activity.log_command("backup", None, None, None,
                                     details=f"skipped ({why})")
                return 0

    try:
        payload, filename, members = build_backup(now)
    except OSError as exc:
        logger.error("Backup could not be built: %s", exc)
        activity.log_error(f"Backup failed while reading {DATA_DIR}: {exc}")
        return 0

    if not members:
        logger.warning("Backup skipped: no files found in %s", DATA_DIR)
        activity.log_error(f"Backup skipped — {DATA_DIR} is empty")
        return 0

    corrupt = verify_backup(payload)
    if corrupt is not None:
        logger.error("Backup archive failed verification (%s)", corrupt)
        activity.log_error(f"Backup archive failed verification: {corrupt}")
        return 0

    if len(payload) > MAX_UPLOAD_BYTES:
        logger.error("Backup is %d bytes, above the upload limit", len(payload))
        activity.log_error(f"Backup too large to send: {len(payload)} bytes")
        return 0

    recipients = await ops_chat_ids()
    if not recipients:
        logger.warning("Backup built but no ops admin is reachable to send it to.")
        activity.log_error("Backup built but no ops admin was reachable")
        return 0

    caption = await build_caption(members, len(payload), now)
    sent = 0
    for chat_id in recipients:
        try:
            await bot.send_document(
                chat_id,
                document=InputFile(io.BytesIO(payload), filename=filename),
                caption=caption,
            )
            sent += 1
        except TelegramError as exc:
            logger.warning("Could not send backup to %s: %s", chat_id, exc)

    if sent:
        save_backup_state(data_fingerprint()[0], now)
        activity.log_command("backup", None, None, None,
                             details=f"{filename} ({len(payload)} bytes) to {sent} admin(s)")
        logger.info("Backup %s sent to %d admin(s).", filename, sent)
    else:
        activity.log_error(f"Backup {filename} built but could not be delivered")
    return sent


async def nightly_backup_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Scheduled daily at BACKUP_TIME."""
    await send_backup(context.bot)


@admin_only
async def cmd_backup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a backup now — for testing the pipeline, or before a risky change.

    Always sends, even when nothing has changed: an explicit request should not
    be silently skipped.
    """
    uid, uname, dname = user_info(update)
    activity.log_command("backup", uid, uname, dname, details="manual")
    sent = await send_backup(context.bot, force=True)
    if sent:
        await update.message.reply_text(f"✅ Backup sent to {sent} ops admin(s).")
    else:
        await update.message.reply_text(
            "⚠️ Backup could not be sent. Check logs/bot.log — the most likely "
            "cause is that no ops admin has started a chat with the bot yet."
        )
