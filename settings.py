"""Boot-time configuration: config.yaml load, paths, logging, activity log.

Importing this module has side effects (reads config, creates directories,
configures logging) — exactly as the same code did at the top of bot.py before
it was extracted. Import it before anything that logs.
"""

from __future__ import annotations

import logging
import re
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
# Console/file log verbosity. INFO shows command execution + notification
# broadcasts; DEBUG additionally shows the underlying Telegram API calls.
LOG_LEVEL = getattr(logging, str(_CFG["log"].get("level", "INFO")).upper(), logging.INFO)
DEFAULT_NOTIF_MIN: int = _CFG["notifications"]["default_minutes_before"]
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

# File handler — appends across restarts
_file_handler = logging.FileHandler(_log_file, mode="a", encoding="utf-8")
_file_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
_file_handler.setLevel(LOG_LEVEL)

# Root at DEBUG so demoted-to-DEBUG records can reach handlers; the handlers'
# own levels (LOG_LEVEL) decide what is actually emitted.
logging.basicConfig(level=logging.DEBUG, handlers=[_console_handler, _file_handler])
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

        # Successful API calls → DEBUG (hidden unless LOG_LEVEL=DEBUG)
        record.levelno = logging.DEBUG
        record.levelname = "DEBUG"
        return True


_httpx_api_filter = _HttpxApiLogFilter()
logging.getLogger("httpx").addFilter(_httpx_api_filter)

activity = ActivityLogger(LOGS_DIR, retention_days=LOG_RETENTION, tz=TZ)
