"""Boot-time configuration: config.yaml load, paths, logging, activity log.

Importing this module has side effects (reads config, creates directories,
configures logging) — exactly as the same code did at the top of bot.py before
it was extracted. Import it before anything that logs.
"""

from __future__ import annotations

import logging
import re
from logging.handlers import TimedRotatingFileHandler
from datetime import datetime
from pathlib import Path

import pytz
import yaml

from activity_logger import ActivityLogger

BASE_DIR = Path(__file__).parent
CONFIG_DIR = BASE_DIR / "config"

with open(CONFIG_DIR / "config.yaml", encoding="utf-8") as _f:
    _CFG = yaml.safe_load(_f)

BOT_TOKEN: str = _CFG["bot"]["token"]
TZ = pytz.timezone(_CFG["bot"]["timezone"])
BOT_DISPLAY_NAME: str = _CFG["bot"].get("display_name", "Kingdom Events Bot")
DATA_DIR = BASE_DIR / _CFG["paths"]["data_dir"]
LOGS_DIR = BASE_DIR / _CFG["paths"]["logs_dir"]
GEN_DIR = BASE_DIR / _CFG["paths"]["generated_dir"]
LOG_RETENTION = _CFG["log"]["retention_days"]
# logs/bot.log is pruned on its own, shorter schedule: it is far more verbose
# than the activity log and only useful for recent debugging.
DEFAULT_RUNTIME_LOG_RETENTION = 45
RUNTIME_LOG_RETENTION = int(
    _CFG["log"].get("runtime_retention_days", DEFAULT_RUNTIME_LOG_RETENTION))
# Console/file log verbosity. INFO shows command execution + notification
# broadcasts; DEBUG additionally shows the underlying Telegram API calls.
LOG_LEVEL = getattr(logging, str(_CFG["log"].get("level", "INFO")).upper(), logging.INFO)
DEFAULT_NOTIF_MIN: int = _CFG["notifications"]["default_minutes_before"]

# Anything written to a log or sent to an admin passes through redact_secrets():
# python-telegram-bot talks to api.telegram.org/bot<TOKEN>/method, so the bot
# token appears in httpx request logs and in the text of network exceptions.
TOKEN_PLACEHOLDER = "BOT-TOKEN-HERE"
# Matches a Telegram bot token in a URL even if it is not *our* token.
_TOKEN_RE = re.compile(r"bot\d{6,}:[A-Za-z0-9_-]{20,}")


def redact_secrets(text: str | None) -> str:
    """Replace the bot token (and any token-shaped string) with a placeholder.

    The URL form is handled first so "…/bot<TOKEN>/getUpdates" and a bare token
    both come out as the same placeholder rather than "botBOT-TOKEN-HERE".
    """
    if not text:
        return "" if text is None else text
    text = _TOKEN_RE.sub(TOKEN_PLACEHOLDER, text)
    if BOT_TOKEN and BOT_TOKEN in text:
        text = text.replace(BOT_TOKEN, TOKEN_PLACEHOLDER)
    return text
# Optional donation link surfaced by /donate (e.g. a PayPal or giving-page URL).
DONATION_URL: str = ((_CFG.get("donations") or {}).get("url") or "").strip()

# Where /privacy sends people. Configurable so the policy can move (a website,
# GitHub Pages) without a code change; defaults to the copy in this repository.
DEFAULT_PRIVACY_URL = "https://github.com/sjeaves2/christofgod_bot/blob/main/PRIVACY.md"
PRIVACY_URL: str = (
    ((_CFG.get("privacy") or {}).get("url") or "").strip() or DEFAULT_PRIVACY_URL)

for _d in (DATA_DIR, LOGS_DIR, GEN_DIR):
    _d.mkdir(parents=True, exist_ok=True)

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_log_file = LOGS_DIR / "bot.log"

# Console handler — always on
_console_handler = logging.StreamHandler()
_console_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
_console_handler.setLevel(LOG_LEVEL)

# One file per day, deleted whole once it falls outside the retention window.
#
# This replaced a custom pruner that read the ENTIRE log into memory at every
# import of this module and rewrote it in place. Measured on the live server
# 2026-10-29: 9.1 seconds to delete 8 lines from a 45 MB file, which was most
# of the bot's ~8s startup and ran TWICE per deploy (update.sh does an import
# check). It also allocated the whole file — a 45 MB spike on a 412 MB box
# whose one outage was caused by memory exhaustion.
#
# Rotation makes the cost O(1): expired files are unlinked, never read. It also
# removes the constraint that forced the old design — the in-place rewrite
# existed to keep this handler's file descriptor valid, and a rotating handler
# manages its own.
#
# backupCount counts FILES, not days. With daily rotation that is normally the
# same thing; if the bot is down for a stretch, no file is written for those
# days, so the window covers 45 days of ACTIVITY rather than 45 calendar days.
# That is the more useful of the two.
#
# Rotated files are named bot.log.YYYY-MM-DD, so searching history means
# `grep logs/bot.log*` rather than `grep logs/bot.log`.
_file_handler = TimedRotatingFileHandler(
    _log_file, when="midnight", backupCount=RUNTIME_LOG_RETENTION,
    encoding="utf-8")
_file_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
_file_handler.setLevel(LOG_LEVEL)

# Root at DEBUG so demoted-to-DEBUG records can reach handlers; the handlers'
# own levels (LOG_LEVEL) decide what is actually emitted.
logging.basicConfig(level=logging.DEBUG, handlers=[_console_handler, _file_handler])

# apscheduler narrates every job tick at INFO ("Running job X", "Job X executed
# successfully", "Added job", "Removed job"). Measured 2026-10-29 on the live
# server: of 259,251 lines in a 45 MB bot.log, **253,256 (97.7%) were these**.
# Everything the bot itself logged came to about 650 lines. The cost was not
# just disk: the pruner this file used to run at import read the WHOLE log
# every time, so the volume was also most of the bot's startup and a 45 MB
# allocation on a 412 MB box. Rotation fixed the reading; this fixes the
# writing, so the files stay small too.
#
# Set on the PARENT logger, which is enough: apscheduler's children
# (apscheduler.executors.default, apscheduler.scheduler) have no explicit level,
# so their effective level resolves through this one. Contrast the httpx
# handling below, which needs a FILTER because it rewrites records rather than
# dropping them.
#
# Still recoverable: at LOG_LEVEL=DEBUG the scheduler goes back to being
# verbose, the same bargain the httpx filter strikes.
logging.getLogger("apscheduler").setLevel(
    logging.DEBUG if LOG_LEVEL <= logging.DEBUG else logging.WARNING)

logger = logging.getLogger(__name__)
logger.info("---- Bot process started ----")


class _HttpxApiLogFilter(logging.Filter):
    """Keep the Telegram API call logs out of the way at INFO level.

    - The very first getUpdates poll is replaced with a friendly INFO notice.
    - Any HTTP 4xx/5xx response is left at its original level so API errors
      always surface.
    - Every other successful API request line (getUpdates polls, sendMessage,
      etc.) is demoted to DEBUG, so it only appears when LOG_LEVEL=DEBUG.
      This also keeps the bot token (embedded in request URLs) out of the
      INFO-level logs.
    """

    def __init__(self) -> None:
        super().__init__()
        self._seen_first = False

    def filter(self, record: logging.LogRecord) -> bool:
        # Scoped to httpx's own records. This filter lives on the handlers (so
        # that child loggers cannot bypass it), which means every record in the
        # process passes through — without this guard a real error from, say,
        # telegram.ext would be demoted merely for mentioning a getUpdates URL.
        if not record.name.startswith("httpx"):
            return True
        msg = record.getMessage()
        if "HTTP Request" not in msg and "getUpdates" not in msg:
            return True  # unrelated record — pass through unchanged

        status_match = re.search(r'"HTTP/[\d.]+ (\d{3})', msg)
        if status_match and int(status_match.group(1)) >= 400:
            return True  # always surface API errors at their original level

        if "getUpdates" in msg and not self._seen_first:
            self._seen_first = True
            record.msg = (
                "Long polling started — using getUpdates to check for incoming messages"
            )
            record.args = ()
            return True  # friendly one-time INFO notice

        # Successful API calls → DEBUG (hidden unless LOG_LEVEL=DEBUG).
        # The record is also *rejected* here when the configured verbosity is
        # above DEBUG: a handler's level is checked before its filters run, so
        # re-labelling alone would no longer suppress anything now that this
        # filter lives on the handlers.
        record.levelno = logging.DEBUG
        record.levelname = "DEBUG"
        return LOG_LEVEL <= logging.DEBUG


class _SecretRedactingFilter(logging.Filter):
    """Strip the bot token out of every log record before it is emitted."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:      # noqa: BLE001 - never break logging
            return True
        redacted = redact_secrets(message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


# Both filters are attached to the HANDLERS, not to the "httpx" logger.
# A filter on a logger is NOT applied to records propagated from its children,
# and httpx logs under "httpx._client" — which is how the bot token was
# reaching bot.log at INFO despite the httpx filter existing.
_httpx_api_filter = _HttpxApiLogFilter()
_redacting_filter = _SecretRedactingFilter()
for _handler in (_console_handler, _file_handler):
    _handler.addFilter(_httpx_api_filter)
    _handler.addFilter(_redacting_filter)

activity = ActivityLogger(LOGS_DIR, retention_days=LOG_RETENTION, tz=TZ)

# When this process started — reported by /stats as uptime.
STARTED_AT = datetime.now(TZ)
