"""Boot-time configuration: config.yaml load, paths, logging, activity log.

Importing this module has side effects (reads config, creates directories,
configures logging) — exactly as the same code did at the top of bot.py before
it was extracted. Import it before anything that logs.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path

import pytz
import yaml

from activity_logger import ActivityLogger, prune_log_file

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


def redact_secrets(text: "str | None") -> str:
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

for _d in (DATA_DIR, LOGS_DIR, GEN_DIR):
    _d.mkdir(parents=True, exist_ok=True)

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_log_file = LOGS_DIR / "bot.log"

# Console handler — always on
_console_handler = logging.StreamHandler()
_console_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
_console_handler.setLevel(LOG_LEVEL)

# Trim records older than the retention window before attaching the handler:
# at this point nothing holds the file open, so the rewrite is safe.
_pruned = prune_log_file(_log_file, RUNTIME_LOG_RETENTION, TZ)

# File handler — appends across restarts
_file_handler = logging.FileHandler(_log_file, mode="a", encoding="utf-8")
_file_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
_file_handler.setLevel(LOG_LEVEL)

# Root at DEBUG so demoted-to-DEBUG records can reach handlers; the handlers'
# own levels (LOG_LEVEL) decide what is actually emitted.
logging.basicConfig(level=logging.DEBUG, handlers=[_console_handler, _file_handler])
logger = logging.getLogger(__name__)
logger.info("---- Bot process started ----")
if _pruned:
    logger.info("Pruned %d log line(s) older than %d days from %s",
                _pruned, RUNTIME_LOG_RETENTION, _log_file.name)


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
