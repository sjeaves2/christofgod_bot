"""Unhandled-exception capture: durable traces on disk, alerts to ops admins.

Every exception the bot raises gets a short error id (ERR-XXXXXX), a full
traceback appended to logs/errors.log, and — subject to the noise controls
below — a direct message to each admin with `ops: true` in admins.yaml.

Two noise controls keep an unattended bot from spamming its admins:

  * Fingerprint de-duplication. Errors are grouped by exception type plus the
    file:line where they were raised. The first occurrence alerts immediately;
    repeats inside ALERT_COOLDOWN are counted silently and rolled up into the
    next alert ("+14 more since 21:35").

  * Transient-network hold. A single NetworkError/TimedOut/BadGateway is a
    normal blip that resolves itself, so those alert only once they reach
    TRANSIENT_THRESHOLD occurrences inside TRANSIENT_WINDOW.

Alert messages are sent as PLAIN TEXT (no parse_mode): a traceback is full of
underscores, asterisks and backticks, and Markdown parsing would reject the
whole message — the exact failure mode that once silently dropped appointment
requests.
"""

from __future__ import annotations

import logging
import traceback
import uuid
from collections import deque
from datetime import datetime, timedelta
from typing import Any

import permissions
import storage
from settings import LOGS_DIR, TZ, activity, redact_secrets
from telegram.error import (
    BadRequest,
    ChatMigrated,
    Conflict,
    Forbidden,
    InvalidToken,
    NetworkError,
)

logger = logging.getLogger(__name__)

ERRORS_LOG = LOGS_DIR / "errors.log"

# Repeats of the same fingerprint stay silent for this long after an alert.
ALERT_COOLDOWN = timedelta(minutes=30)
# Transient network errors must occur this many times within this window
# before they are considered worth waking anyone for.
TRANSIENT_WINDOW = timedelta(minutes=5)
TRANSIENT_THRESHOLD = 5
# Frames of traceback included in the DM (the deepest ones carry the line
# numbers that actually locate the bug).
DM_FRAMES = 4
# Telegram hard-caps messages at 4096 characters; stay clear of it.
MAX_DM_CHARS = 3500

# Transient connectivity noise, matched three ways because the same upstream
# blip reaches us differently depending on the layer that caught it:
#   * python-telegram-bot wraps most of them (NetworkError, TimedOut, RetryAfter)
#   * raw httpx/OS errors surface by class name
#   * gateway failures arrive as a NetworkError whose *message* is "Bad Gateway"
TRANSIENT_TYPES = {"NetworkError", "TimedOut", "RetryAfter",
                   "ServerDisconnectedError", "ConnectionError", "ConnectError",
                   "ReadError", "ConnectTimeout", "ReadTimeout", "PoolTimeout",
                   "RemoteProtocolError"}
TRANSIENT_MESSAGES = ("bad gateway", "service unavailable", "gateway time-out",
                      "gateway timeout", "connection reset", "temporarily unavailable")

# PTB models BadRequest/TimedOut as NetworkError subclasses, so real faults have
# to be named explicitly to escape the transient hold.
ALWAYS_ALERT_TYPES = (BadRequest, Conflict, Forbidden, InvalidToken, ChatMigrated)
_NetworkError = NetworkError

# fingerprint -> {"last_alert": datetime, "suppressed": int}
_alert_state: dict[str, dict[str, Any]] = {}
# fingerprint -> deque[datetime] of recent occurrences (transient throttling)
_recent: dict[str, deque] = {}


def _now() -> datetime:
    return datetime.now(TZ)


def new_error_id() -> str:
    """Short, quotable identifier an admin can read back to you."""
    return f"ERR-{uuid.uuid4().hex[:6].upper()}"


def _exc_origin(exc: BaseException) -> str:
    """'file.py:123' for the deepest frame of the traceback, or '?' if absent."""
    tb = getattr(exc, "__traceback__", None)
    if tb is None:
        return "?"
    frames = traceback.extract_tb(tb)
    if not frames:
        return "?"
    last = frames[-1]
    name = str(last.filename).split("/")[-1]
    return f"{name}:{last.lineno}"


def fingerprint(exc: BaseException) -> str:
    """Group key for de-duplication: exception type plus where it was raised."""
    return f"{type(exc).__name__}@{_exc_origin(exc)}"


def is_transient(exc: BaseException) -> bool:
    """True for connectivity blips that normally resolve on their own.

    Some real faults must NOT be held back, even though python-telegram-bot
    models them as NetworkError subclasses:
      * BadRequest  — e.g. "can't parse entities", a bug in a message we built
      * Forbidden   — a user blocked the bot, or it was removed from a group
      * Conflict    — two bot instances polling the same token
      * InvalidToken/ChatMigrated — configuration problems
    Those alert on the first occurrence.
    """
    if isinstance(exc, ALWAYS_ALERT_TYPES):
        return False
    if type(exc).__name__ in TRANSIENT_TYPES:
        return True
    if isinstance(exc, _NetworkError):   # wrapped httpx/connection failures
        return True
    message = str(exc).lower()
    return any(marker in message for marker in TRANSIENT_MESSAGES)


def format_trace(exc: BaseException) -> str:
    """Full traceback text for the durable log."""
    return "".join(traceback.format_exception(type(exc), exc,
                                              getattr(exc, "__traceback__", None)))


def write_error_log(error_id: str, exc: BaseException, note: str = "") -> None:
    """Append the full trace to logs/errors.log so it can be found by id later."""
    stamp = _now().strftime("%Y-%m-%d %H:%M:%S %Z")
    block = [f"===== {error_id}  {stamp} =====",
             f"type   : {type(exc).__name__}",
             f"message: {exc}",
             f"origin : {_exc_origin(exc)}"]
    if note:
        block.append(f"context: {note}")
    block.append(format_trace(exc).rstrip())
    block.append("")
    try:
        ERRORS_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(ERRORS_LOG, "a", encoding="utf-8") as fh:
            # Network exceptions embed the API URL, which carries the token.
            fh.write(redact_secrets("\n".join(block)) + "\n")
    except OSError as write_exc:      # never let logging failure mask the error
        logger.error("Could not write %s: %s", ERRORS_LOG, write_exc)


def should_alert(exc: BaseException, now: "datetime | None" = None) -> "tuple[bool, int]":
    """Decide whether to alert for *exc*; returns (send_alert, suppressed_count).

    Records the occurrence either way. *suppressed_count* is how many repeats
    were swallowed since the last alert, for roll-up in the message.
    """
    now = now or _now()
    fp = fingerprint(exc)

    # Transient connectivity errors: only speak up once they persist.
    if is_transient(exc):
        seen = _recent.setdefault(fp, deque())
        seen.append(now)
        while seen and now - seen[0] > TRANSIENT_WINDOW:
            seen.popleft()
        if len(seen) < TRANSIENT_THRESHOLD:
            return False, 0

    state = _alert_state.get(fp)
    if state is not None and now - state["last_alert"] < ALERT_COOLDOWN:
        state["suppressed"] += 1
        return False, state["suppressed"]

    suppressed = state["suppressed"] if state else 0
    _alert_state[fp] = {"last_alert": now, "suppressed": 0}
    return True, suppressed


def build_alert(error_id: str, exc: BaseException, note: str = "",
                suppressed: int = 0) -> str:
    """Compact PLAIN-TEXT alert: identity, message, and the deepest frames.

    The deepest frames are kept because those carry the file and line numbers
    that locate the fault; earlier frames are usually framework plumbing.
    """
    lines = [f"⚠️ Bot error {error_id}",
             f"{type(exc).__name__}: {exc}"]
    if note:
        lines.append(f"Context: {note}")
    if is_transient(exc):
        lines.append(f"(transient network errors — {TRANSIENT_THRESHOLD}+ "
                     f"in {int(TRANSIENT_WINDOW.total_seconds() // 60)} minutes)")
    if suppressed:
        lines.append(f"(+{suppressed} more of this error since the last alert)")

    tb = getattr(exc, "__traceback__", None)
    if tb is not None:
        frames = traceback.extract_tb(tb)[-DM_FRAMES:]
        if frames:
            lines.append("")
            lines.append("Traceback (most recent call last):")
            for f in frames:
                name = str(f.filename).split("/")[-1]
                lines.append(f"  {name}:{f.lineno} in {f.name}")
                if f.line:
                    lines.append(f"    {f.line.strip()}")
    lines.append("")
    lines.append(f"Full trace: logs/errors.log (search {error_id})")

    text = redact_secrets("\n".join(lines))
    if len(text) > MAX_DM_CHARS:
        text = text[:MAX_DM_CHARS] + "\n… (truncated — see logs/errors.log)"
    return text


async def ops_chat_ids() -> set:
    """Chat ids of ops admins the bot can actually reach.

    Ops admins are known either because they shared a contact (phone match) or
    because a registered user's current username is listed with `ops: true`.
    """
    ids = set(permissions._ops_chat_ids)
    for u in await storage.get_all_users():
        uname = (u.get("username") or "").lstrip("@").lower()
        if uname and uname in permissions.OPS_USERNAMES and u.get("chat_id"):
            ids.add(u["chat_id"])
    return ids


async def report_exception(bot, exc: BaseException, note: str = "") -> "str | None":
    """Record an exception and alert ops admins when the noise rules allow.

    Returns the error id (always assigned, so the activity log and any alert
    can be correlated), or None if *exc* is missing.
    """
    if exc is None:
        return None
    error_id = new_error_id()
    write_error_log(error_id, exc, note)
    activity.log_error(redact_secrets(f"{error_id} {type(exc).__name__}: {exc}"))

    send, suppressed = should_alert(exc)
    if not send:
        return error_id

    recipients = await ops_chat_ids()
    if not recipients:
        logger.warning("No ops admin is reachable to alert about %s — they must "
                       "run /start (or share a contact) first.", error_id)
        return error_id

    text = build_alert(error_id, exc, note, suppressed)
    for chat_id in recipients:
        try:
            # Plain text on purpose: tracebacks are not valid Markdown.
            await bot.send_message(chat_id, text)
        except Exception as send_exc:     # noqa: BLE001 - alerting must not raise
            logger.warning("Could not alert ops admin %s about %s: %s",
                           chat_id, error_id, send_exc)
    return error_id


def reset_error_log() -> int:
    """Clear logs/errors.log at startup and forget in-memory alert state.

    A restart normally means new code, so traces from the previous run refer to
    bugs that are either fixed or about to recur with fresh line numbers —
    keeping them only makes the file harder to read. Returns the bytes discarded
    (logged, so the size is not lost even though the content is).
    """
    _alert_state.clear()
    _recent.clear()
    try:
        if not ERRORS_LOG.exists():
            return 0
        size = ERRORS_LOG.stat().st_size
        ERRORS_LOG.write_text("", encoding="utf-8")
        if size:
            logger.info("Cleared %s (%d bytes) from the previous run.", ERRORS_LOG.name, size)
        return size
    except OSError as exc:
        logger.warning("Could not clear %s: %s", ERRORS_LOG, exc)
        return 0
