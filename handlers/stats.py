"""/stats — operational and usage reporting for admins.

Two views, selected by argument:

    /stats            system health, last 7 days   (default)
    /stats 30         system health, last 30 days
    /stats usage      ministry activity, last 7 days
    /stats usage 30   ministry activity, last 30 days

Historical figures come from parsing logs/bot_activity.log, so they cannot
predate that file (entries are pruned after LOG_RETENTION days). Live counts
— users, appointments, announcements — are read from the YAML stores and are
always current.

Admin-only and English-only, consistent with the other admin commands.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

import storage
from common import md, now_tz
from handlers.announcements import active_announcements
from permissions import admin_only, user_info
from settings import LOGS_DIR, STARTED_AT, TZ, activity
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

ACTIVITY_LOG = LOGS_DIR / "bot_activity.log"
ERRORS_LOG = LOGS_DIR / "errors.log"

DEFAULT_DAYS = 7
MAX_DAYS = 30

# [2026-06-16 17:34:32 EDT] [COMMAND] Name @user (id:1) | /start — detail
_LINE_RE = re.compile(
    r"^\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})[^\]]*\]\s+"
    r"\[(?P<kind>[A-Z_]+)\]\s+(?P<who>.*?)\s+\|\s+(?P<detail>.*)$"
)


def parse_args(args: "list[str] | None") -> "tuple[str, int, bool]":
    """Interpret /stats arguments.

    Returns (view, days, clamped) where view is "system" or "usage".
    A bare integer sets the period; the keyword "usage" switches view.
    Unparseable values fall back to the defaults rather than erroring.
    """
    view, days, clamped = "system", DEFAULT_DAYS, False
    for raw in args or []:
        token = raw.strip().lower().lstrip("/")
        if token in ("usage", "ministry", "activity"):
            view = "usage"
        elif token in ("system", "sys", "health"):
            view = "system"
        elif token.isdigit():
            value = int(token)
            if value > MAX_DAYS:
                value, clamped = MAX_DAYS, True
            days = max(1, value)
    return view, days, clamped


def read_activity(days: int, now: "datetime | None" = None) -> list[dict]:
    """Parse activity-log entries newer than *days* ago.

    Malformed or undated lines are skipped: a stats command must never fail
    because of one odd line in a long-lived log.
    """
    now = now or now_tz()
    cutoff = now - timedelta(days=days)
    entries: list[dict] = []
    try:
        with open(ACTIVITY_LOG, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = _LINE_RE.match(line.rstrip("\n"))
                if not m:
                    continue
                try:
                    stamp = TZ.localize(datetime.strptime(m.group("ts"), "%Y-%m-%d %H:%M:%S"))
                except (ValueError, TypeError):
                    continue
                if stamp < cutoff:
                    continue
                entries.append({"at": stamp, "kind": m.group("kind"),
                                "who": m.group("who"), "detail": m.group("detail")})
    except FileNotFoundError:
        logger.warning("Activity log not found at %s", ACTIVITY_LOG)
    return entries


def _fmt_duration(delta: timedelta) -> str:
    total = int(delta.total_seconds())
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _command_name(detail: str) -> str:
    """'/appointment — foo' -> '/appointment'."""
    return detail.split()[0] if detail.split() else "?"


def _notification_recipients(detail: str) -> int:
    """'Sent notification for 'X' to 3 user(s)' -> 3."""
    m = re.search(r"\bto (\d+) ", detail)
    return int(m.group(1)) if m else 0


def build_system_report(days: int, clamped: bool, now: "datetime | None" = None) -> str:
    """Health view: uptime, error volume, and what the most recent errors were."""
    now = now or now_tz()
    entries = read_activity(days, now)
    errors = [e for e in entries if e["kind"] == "ERROR"]
    last_24h = [e for e in errors if now - e["at"] <= timedelta(hours=24)]

    lines = [f"🩺 *System stats* — last {days} day(s)\n"]
    lines.append(f"*Uptime:* {_fmt_duration(now - STARTED_AT)} "
                 f"(since {STARTED_AT.strftime('%Y-%m-%d %H:%M %Z')})")
    lines.append(f"*Activity entries in period:* {len(entries)}")
    lines.append("")
    lines.append(f"*Errors — last 24h:* {len(last_24h)}")
    lines.append(f"*Errors — last {days}d:* {len(errors)}")

    if errors:
        # Group by the type/id prefix so recurring faults stand out.
        kinds: dict[str, int] = {}
        for e in errors:
            m = re.match(r"(ERR-[0-9A-F]+)\s+([A-Za-z_.]+):", e["detail"])
            label = m.group(2) if m else e["detail"].split(":")[0][:40]
            kinds[label] = kinds.get(label, 0) + 1
        lines.append("")
        lines.append("*Most frequent:*")
        for label, count in sorted(kinds.items(), key=lambda kv: kv[1], reverse=True)[:5]:
            lines.append(f"  • {md(label)} — {count}")
        latest = max(errors, key=lambda e: e["at"])
        lines.append("")
        lines.append(f"*Most recent:* {latest['at'].strftime('%Y-%m-%d %H:%M %Z')}")
        lines.append(f"  {md(latest['detail'][:160])}")

    lines.append("")
    lines.append(f"_Full traces: logs/errors.log_ "
                 f"({'present' if ERRORS_LOG.exists() else 'none yet'})")
    if clamped:
        lines.append(f"\n_Period capped at {MAX_DAYS} days._")
    return "\n".join(lines)


async def build_usage_report(days: int, clamped: bool,
                             now: "datetime | None" = None) -> str:
    """Ministry view: people, appointments, announcements, reminders, commands."""
    now = now or now_tz()
    entries = read_activity(days, now)
    users = await storage.get_all_users()
    appts = await storage.get_appointments()
    anns = await storage.get_announcements()

    joined = len([e for e in entries if e["kind"] == "USER_JOINED"])
    left = len([e for e in entries if e["kind"] == "USER_LEFT"])
    commands = [e for e in entries if e["kind"] == "COMMAND"]
    notifs = [e for e in entries if e["kind"] == "NOTIFICATION"]

    # Opt-in reminder categories and languages, from live user records.
    cats: dict[str, int] = {}
    langs: dict[str, int] = {}
    for u in users:
        for c in u.get("notif_prefs") or []:
            cats[c] = cats.get(c, 0) + 1
        langs[u.get("language") or "en"] = langs.get(u.get("language") or "en", 0) + 1

    # Only appointments still ahead of us are counted: past ones say nothing
    # about current load, and they age out to the archive anyway.
    statuses: dict[str, int] = {}
    upcoming_total = 0
    next_30_days = 0
    undated = 0
    for a in appts:
        raw = a.get("confirmed_datetime") or a.get("requested_datetime") or ""
        try:
            when = datetime.fromisoformat(raw)
        except (ValueError, TypeError):
            undated += 1
            continue
        if when.tzinfo is None:
            when = TZ.localize(when)
        if when < now:
            continue                      # already happened
        upcoming_total += 1
        statuses[a.get("status", "?")] = statuses.get(a.get("status", "?"), 0) + 1
        if when <= now + timedelta(days=30):
            next_30_days += 1

    lines = [f"📊 *Usage stats* — last {days} day(s)\n"]
    lines.append(f"*Registered users:* {len(users)}  (+{joined} new, -{left} left)")
    if langs:
        lines.append("*Languages:* " + ", ".join(f"{k} {v}" for k, v in sorted(langs.items())))
    if cats:
        lines.append("*Personal reminder opt-ins:* "
                     + ", ".join(f"{k} {v}" for k, v in sorted(cats.items())))
    else:
        lines.append("*Personal reminder opt-ins:* none")

    lines.append("")
    lines.append(f"*Upcoming appointments:* {upcoming_total}")
    if statuses:
        lines.append("  " + ", ".join(f"{k}: {v}" for k, v in sorted(statuses.items())))
    lines.append(f"  within next 30 days: {next_30_days}")
    if undated:
        lines.append(f"  ⚠️ {undated} record(s) with an unreadable date")

    lines.append("")
    lines.append(f"*Active announcements:* {len(active_announcements(anns, now))}")

    lines.append("")
    lines.append(f"*Event reminders sent:* {len(notifs)} broadcast(s), "
                 f"{sum(_notification_recipients(e['detail']) for e in notifs)} recipient(s)")

    lines.append("")
    lines.append(f"*Commands used:* {len(commands)}")
    if commands:
        counts: dict[str, int] = {}
        for e in commands:
            name = _command_name(e["detail"])
            counts[name] = counts.get(name, 0) + 1
        for name, count in sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:5]:
            lines.append(f"  • {md(name)} — {count}")

    if clamped:
        lines.append(f"\n_Period capped at {MAX_DAYS} days._")
    lines.append("\n_Historical figures come from the activity log and cannot "
                 "predate it._")
    return "\n".join(lines)


@admin_only
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid, uname, dname = user_info(update)
    view, days, clamped = parse_args(getattr(context, "args", None))
    activity.log_command("stats", uid, uname, dname, details=f"{view} {days}d")
    if view == "usage":
        text = await build_usage_report(days, clamped)
    else:
        text = build_system_report(days, clamped)
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
