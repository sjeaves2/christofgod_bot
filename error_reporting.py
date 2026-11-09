"""Unhandled-exception capture: durable traces on disk, alerts to ops admins.

Every exception the bot raises gets a short error id (ERR-XXXXXX), a full
traceback appended to logs/errors.log, and — subject to the noise controls
below — a direct message to each admin with `ops: true` in admins.yaml.

Two noise controls keep an unattended bot from spamming its admins:

  * Fingerprint de-duplication. Errors are grouped by exception type plus the
    file:line where they were raised. The first occurrence alerts immediately;
    repeats inside ALERT_COOLDOWN are counted silently and rolled up into the
    next alert ("+14 more since 21:35").

  * Transient-network hold, measured in DURATION, not occurrences. A
    NetworkError/TimedOut/BadGateway alerts only once the condition has
    PERSISTED for SUSTAINED_FOR. See below for why counting was wrong.

Alert messages are sent as PLAIN TEXT (no parse_mode): a traceback is full of
underscores, asterisks and backticks, and Markdown parsing would reject the
whole message — the exact failure mode that once silently dropped appointment
requests.

Why duration and not a count
----------------------------
The rule used to be "5 occurrences inside 5 minutes". Two months of production
data showed what that actually selects for. Telegram runs something scheduled
at 01:11–01:12 UTC: 60 of 61 recorded Bad Gateway events landed in that
75-second window between 2026-09-05 and 2026-11-02, and the local clock
following the DST change on 2026-11-01 while the UTC time did not move
confirmed it is their schedule, not anything here.

Those bursts are dense and short — six errors in fourteen seconds on
2026-11-02 — so a count threshold is really a measure of how FAST errors
arrive, and a burst clears it while being completely harmless. Four such
nights crossed it. The longest outage any of them caused was 1m 59s, PTB's
polling loop recovered by itself every time, and the congregation never saw
anything.

What is worth waking an admin for is a condition that PERSISTS. So an episode
is tracked instead: occurrences no more than EPISODE_GAP apart are one
episode, and an episode alerts once it has lasted SUSTAINED_FOR. A fourteen-
second burst can never qualify however many errors it contains, and a genuine
outage qualifies even if it produces few errors.

A count floor is not needed: one occurrence is an episode of zero length, so a
lone error never alerts.

This is deliberately NOT read from network_watchdog, which measures outage
duration precisely and would be the obvious source. network_watchdog imports
this module, so reaching back for it would close a cycle;
tests/test_resilience.py pins the one-way dependency. The occurrence
timestamps needed are already here.

Undeliverable alerts are held, not dropped
------------------------------------------
An alert about a network failure travels over the network that is failing, so
the alerts that matter most are the ones least able to get out. On 2026-10-27
the fifth Telegram 502 in nine seconds passed the transient hold, the DM was
attempted, and the send failed with `Bad Gateway` — the admin was never told,
because this module logged a warning and moved on.

So an alert that reaches nobody is now queued to PENDING_ALERTS and retried by
`flush_pending_alerts`, which runs on every successful watchdog probe and once
at startup.

The startup leg is not redundant. `network_watchdog` deliberately exits the
process after a sustained outage so systemd restarts it, which means an
in-memory queue would be destroyed by exactly the outages worth alerting
about. The queue is on disk for that reason alone.

It lives under logs/ rather than data/ because handlers/backup.py globs all of
data/ into the nightly backup DM, and this is transient ops state, not a store.
"""

from __future__ import annotations

import json
import logging
import traceback
import uuid
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
# Alerts that could not be delivered, waiting for connectivity to come back.
PENDING_ALERTS = LOGS_DIR / "pending_alerts.json"

# Cap on held alerts. A bot that has generated more than this while offline has
# one fault repeating, and the oldest entries describe it just as well as the
# newest; keeping the OLDEST is what matters, because the first occurrence is
# the one with the useful context.
MAX_PENDING = 10
# Held alerts older than this are dropped unsent. An admin reading "the bot
# failed" two days after the fact cannot act on it, and errors.log still has
# the trace, so the message would be noise rather than news.
PENDING_MAX_AGE = timedelta(hours=24)

# Repeats of the same fingerprint stay silent for this long after an alert.
ALERT_COOLDOWN = timedelta(minutes=30)
# How long a transient condition must PERSIST before it is worth an alert.
# Chosen against the production record: the worst nightly Telegram blip ran
# 1m 59s and needed nobody, while the 2026-09-04 outage ran 87 minutes and
# needed somebody. Five minutes is clear of the first and well inside the
# second, and it is half network_watchdog's FAILURE_GRACE, so a sustained
# outage is reported before the watchdog restarts the process over it.
SUSTAINED_FOR = timedelta(minutes=5)
# Quiet gap after which the next occurrence starts a NEW episode rather than
# extending the old one. Longer than the longest measured blip (1m 59s), so
# two separate blips an hour apart are never welded into one long "episode"
# that would alert about a problem that is not happening.
EPISODE_GAP = timedelta(minutes=3)
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
# fingerprint -> {"started": datetime, "last_seen": datetime, "count": int}
# The current episode of a transient condition. See "Why duration and not a
# count" in the module docstring.
_episodes: dict[str, dict[str, Any]] = {}


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


def episode_duration(exc: BaseException, now: datetime) -> timedelta:
    """Record this occurrence and return how long its episode has now run.

    An episode is a run of occurrences of the same fingerprint no more than
    EPISODE_GAP apart. A longer silence means the condition cleared, so the
    next occurrence starts a fresh episode at zero rather than inheriting the
    age of an unrelated earlier one.

    The first occurrence of an episode returns zero, which is what keeps a
    lone error silent without needing a separate count threshold.
    """
    fp = fingerprint(exc)
    episode = _episodes.get(fp)
    if episode is None or now - episode["last_seen"] > EPISODE_GAP:
        episode = {"started": now, "count": 0}
    episode["last_seen"] = now
    episode["count"] += 1
    _episodes[fp] = episode
    return now - episode["started"]


def should_alert(exc: BaseException, now: datetime | None = None) -> tuple[bool, int]:
    """Decide whether to alert for *exc*; returns (send_alert, suppressed_count).

    Records the occurrence either way. *suppressed_count* is how many repeats
    were swallowed since the last alert, for roll-up in the message.
    """
    now = now or _now()
    fp = fingerprint(exc)

    # Transient connectivity errors: only speak up once they have PERSISTED.
    if is_transient(exc) and episode_duration(exc, now) < SUSTAINED_FOR:
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
        minutes = int(SUSTAINED_FOR.total_seconds() // 60)
        episode = _episodes.get(fingerprint(exc)) or {}
        lines.append(
            f"(network trouble lasting over {minutes} minutes — "
            f"{episode.get('count', 0)} occurrence(s) so far. Brief blips are "
            "not reported.)")
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

    Three routes, because the first two are both perishable:

      * `permissions._ops_chat_ids` — somebody shared a contact during THIS
        process. Rebuilt from empty on every start, so it says nothing about
        anyone after a restart.
      * a registered user whose CURRENT username is listed with `ops: true`.
        Stops matching the day that person changes their Telegram handle.
      * a registered user whose STORED phone is listed with `ops: true`.

    The stored-phone route is the one this function was missing. A `phone:` in
    admins.yaml is meant to be the backstop for a changed username, but the
    number was only ever compared against a live contact-share
    (permissions._register_admin_by_phone), never against the phone already
    sitting in data/users.yaml — so a phone-matched ops admin went unreachable
    at the next restart, and nothing reported it. An alerting path that loses
    recipients silently is worse than one that never had them: the alerts keep
    "succeeding" with nobody on the other end.

    Both sides of the phone comparison go through
    permissions.normalize_phone, which is also what OPS_PHONES is built with,
    so a NANP number matches whether or not it carries its country code.
    """
    ids = set(permissions._ops_chat_ids)
    for u in await storage.get_all_users():
        chat_id = u.get("chat_id")
        if not chat_id:
            continue
        uname = (u.get("username") or "").lstrip("@").lower()
        if uname and uname in permissions.OPS_USERNAMES:
            ids.add(chat_id)
            # Skips a redundant regex, nothing more: both branches can only
            # add this same chat_id and `ids` is a set. Deleting this line is
            # an EQUIVALENT mutation — no test can kill it, and one written to
            # try would be asserting on an implementation detail.
            continue
        phone = permissions.normalize_phone(u.get("phone"))
        if phone and phone in permissions.OPS_PHONES:
            ids.add(chat_id)
    return ids


def _load_pending() -> list[dict[str, Any]]:
    """Held alerts, oldest first. Returns [] rather than raising, always.

    A corrupt or half-written file must not break error reporting — this code
    path only ever runs when something has already gone wrong.
    """
    try:
        if not PENDING_ALERTS.exists():
            return []
        data = json.loads(PENDING_ALERTS.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("Could not read %s (%s); discarding held alerts.",
                       PENDING_ALERTS.name, exc)
        return []
    if not isinstance(data, list):
        return []
    return [e for e in data
            if isinstance(e, dict) and isinstance(e.get("text"), str)]


def _store_pending(entries: list[dict[str, Any]]) -> None:
    """Replace the held-alert file, or delete it when nothing is left."""
    try:
        PENDING_ALERTS.parent.mkdir(parents=True, exist_ok=True)
        if not entries:
            PENDING_ALERTS.unlink(missing_ok=True)
            return
        PENDING_ALERTS.write_text(json.dumps(entries, indent=1),
                                  encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not write %s: %s", PENDING_ALERTS, exc)


def _fresh(entries: list[dict[str, Any]],
           now: datetime) -> list[dict[str, Any]]:
    """Drop entries past PENDING_MAX_AGE, and any with an unreadable stamp."""
    kept = []
    for entry in entries:
        try:
            queued_at = datetime.fromisoformat(entry["queued_at"])
        except (KeyError, TypeError, ValueError):
            continue
        if now - queued_at <= PENDING_MAX_AGE:
            kept.append(entry)
    return kept


def hold_alert(error_id: str, text: str, now: datetime | None = None) -> None:
    """Queue an alert that reached nobody, for retry once the network returns."""
    now = now or _now()
    entries = _fresh(_load_pending(), now)
    entries.append({"error_id": error_id,
                    "queued_at": now.isoformat(),
                    "text": text})
    # Keep the OLDEST on overflow: the first occurrence carries the context,
    # and the later ones are usually the same fault repeating.
    _store_pending(entries[:MAX_PENDING])
    logger.warning("Alert %s could not be delivered; held for retry (%d "
                   "pending).", error_id, min(len(entries), MAX_PENDING))


def _delayed_text(entry: dict[str, Any]) -> str:
    """The held alert, labelled with when it actually happened.

    Without this an admin reads a stale traceback as a live incident and goes
    looking for a fault that ended hours ago.
    """
    try:
        when = datetime.fromisoformat(entry["queued_at"]).strftime("%a %H:%M")
    except (KeyError, TypeError, ValueError):
        when = "earlier"
    header = (f"⏳ Delayed alert from {when} — the bot could not reach "
              "Telegram at the time.")
    text = f"{header}\n\n{entry['text']}"
    if len(text) > MAX_DM_CHARS:
        text = text[:MAX_DM_CHARS] + "\n… (truncated — see logs/errors.log)"
    return text


async def flush_pending_alerts(bot) -> int:
    """Deliver held alerts; returns how many went out. Never raises.

    Stops at the first failure and keeps that entry and everything after it:
    if one send fails the network is still bad, and emptying the queue against
    a dead connection is how the original bug lost its alerts.
    """
    entries = _load_pending()
    if not entries:
        return 0

    now = _now()
    entries = _fresh(entries, now)
    if not entries:
        _store_pending([])
        logger.info("Held alerts expired unsent (older than %s).",
                    PENDING_MAX_AGE)
        return 0

    recipients = await ops_chat_ids()
    if not recipients:
        _store_pending(entries)     # prune, but keep waiting for a recipient
        return 0

    sent = 0
    for index, entry in enumerate(entries):
        text = _delayed_text(entry)
        delivered = False
        for chat_id in recipients:
            try:
                await bot.send_message(chat_id, text)
                delivered = True
            except Exception as send_exc:   # noqa: BLE001 - must not raise
                logger.warning("Retry of held alert %s to %s failed: %s",
                               entry.get("error_id"), chat_id, send_exc)
        if not delivered:
            _store_pending(entries[index:])
            return sent
        sent += 1

    _store_pending([])
    logger.info("Delivered %d held alert(s) after connectivity returned.", sent)
    return sent


async def report_exception(bot, exc: BaseException, note: str = "") -> str | None:
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
    delivered = False
    for chat_id in recipients:
        try:
            # Plain text on purpose: tracebacks are not valid Markdown.
            await bot.send_message(chat_id, text)
            delivered = True
        except Exception as send_exc:     # noqa: BLE001 - alerting must not raise
            logger.warning("Could not alert ops admin %s about %s: %s",
                           chat_id, error_id, send_exc)
    if not delivered:
        # Reached nobody. Hold it rather than drop it — see the module
        # docstring; this is the path that lost the 2026-10-27 alert.
        hold_alert(error_id, text)
    return error_id


def _retain_traces(text: str, error_ids: set) -> str:
    """The blocks of *text* belonging to *error_ids*; "" when none apply.

    write_error_log delimits each trace with a `===== ERR-XXXXXX  ... =====`
    header line, which is what this splits on.
    """
    if not error_ids or not text:
        return ""
    kept: list[str] = []
    keeping = False
    for line in text.splitlines(keepends=True):
        if line.startswith("===== ERR-"):
            keeping = any(eid and eid in line for eid in error_ids)
        if keeping:
            kept.append(line)
    return "".join(kept)


def pending_error_ids() -> set:
    """Error ids of the alerts currently held on disk.

    Read BEFORE flushing, so the traces behind alerts the flush delivers can
    still be retained — see reset_error_log and bot.post_init. After a
    successful flush those entries are gone from the queue and their ids
    cannot be recovered.

    Falsy ids are dropped rather than passed along. `_retain_traces` tests
    each id with `eid in line`, and None or "" would match every header line,
    silently turning "clear the log" into "keep everything".
    """
    return {e.get("error_id") for e in _load_pending() if e.get("error_id")}


def reset_error_log(also_retain: set | None = None) -> int:
    """Clear logs/errors.log at startup and forget in-memory alert state.

    A restart normally means new code, so traces from the previous run refer to
    bugs that are either fixed or about to recur with fresh line numbers —
    keeping them only makes the file harder to read. Returns the bytes discarded
    (logged, so the size is not lost even though the content is).

    PENDING_ALERTS is deliberately NOT cleared here. A restart is the most
    likely reason an alert is still sitting there — the watchdog exits the
    process after a sustained outage — so clearing it at startup would discard
    precisely the alerts this is meant to preserve. post_init flushes it.

    Traces are kept for two sets of alerts, because every alert ends with
    "Full trace: logs/errors.log (search ERR-XXXXXX)" and clearing the file
    wholesale sends the reader to an empty log:

      * alerts STILL held, whose send failed again — found via _load_pending
      * alerts in *also_retain*, which post_init captures before flushing

    The second set is the bug this parameter exists for. post_init flushes the
    queue and THEN calls this, so a DELIVERED alert is no longer pending by the
    time this runs: before v0.28.12 its trace was deleted seconds after the
    admin was told to go and read it. The alert that arrived was the one whose
    trace did not survive — the exact inverse of what was intended.

    A dangling pointer is worse than no pointer. The reader concludes the
    logging is broken, or that they misread the id, and stops trusting alerts
    that did arrive.
    """
    _alert_state.clear()
    _episodes.clear()
    try:
        if not ERRORS_LOG.exists():
            return 0
        size = ERRORS_LOG.stat().st_size
        keep_ids = pending_error_ids() | {e for e in (also_retain or ()) if e}
        retained = _retain_traces(ERRORS_LOG.read_text(encoding="utf-8"),
                                  keep_ids)
        ERRORS_LOG.write_text(retained, encoding="utf-8")
        size -= len(retained.encode("utf-8"))
        if retained:
            logger.info("Kept the trace(s) behind %d alert(s) still being "
                        "referenced.", len(keep_ids))
        if size:
            logger.info("Cleared %s (%d bytes) from the previous run.", ERRORS_LOG.name, size)
        return size
    except OSError as exc:
        logger.warning("Could not clear %s: %s", ERRORS_LOG, exc)
        return 0
