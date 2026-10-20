"""The library we test against must be the library we pin.

python-telegram-bot is the one dependency whose major version changes the
bot's API surface. v22.0 removed everything deprecated across v20.x —
`filters.CHAT`, `Defaults.disable_web_page_preview`, `proxy_url`, the
`*_timeout` arguments to run_polling, the `quote=` argument on reply_*. This
bot used none of them, which is why the upgrade was uneventful, but that is a
fact about today's code and not a guarantee about tomorrow's.

The failure this guards against is quiet: a developer venv left on an older
major while CI and the server run a newer one. The suite would pass locally
against an API the server does not have.
"""

from __future__ import annotations

import re
from pathlib import Path

import telegram

REPO = Path(__file__).resolve().parents[1]
REQUIREMENTS = (REPO / "requirements.txt").read_text()

_PIN = re.compile(r"python-telegram-bot(?:\[[^\]]*\])?\s*>=\s*(\d+)\.[\d.]*\s*,\s*<\s*(\d+)\.")


def _pinned_range() -> tuple[int, int]:
    m = _PIN.search(REQUIREMENTS)
    assert m, "python-telegram-bot is not pinned to a major-version range"
    return int(m.group(1)), int(m.group(2))


class TestTelegramPin:
    def test_it_is_pinned_to_a_major_range(self):
        low, high = _pinned_range()
        assert high == low + 1, (
            f"the pin spans majors {low}..{high}; a major bump removes APIs, "
            "so it should be taken deliberately rather than by resolution"
        )

    def test_the_installed_version_satisfies_the_pin(self):
        low, high = _pinned_range()
        installed = int(telegram.__version__.split(".")[0])
        assert low <= installed < high, (
            f"this venv has python-telegram-bot {telegram.__version__}, but "
            f"requirements.txt pins >={low},<{high}. The suite would be "
            "testing a different API from the one CI and the server use — "
            "reinstall with: pip install -r requirements-dev.txt"
        )

    def test_the_job_queue_extra_is_requested(self):
        """APScheduler arrives only through this extra, and it is what sends
        every reminder. deploy/prune_venv.py would remove it otherwise."""
        assert "python-telegram-bot[job-queue]" in REQUIREMENTS

    def test_the_job_queue_is_actually_available(self):
        """The extra being named is not the same as it being installed."""
        import apscheduler  # noqa: F401
        from telegram.ext import JobQueue
        assert JobQueue is not None


class TestRemovedApisAreNotUsed:
    """v22.0 removals. Cheap to check, and the check keeps its own reason."""

    SOURCES = sorted((REPO / "handlers").glob("*.py")) + [REPO / "bot.py",
                                                          REPO / "common.py"]

    def test_none_of_the_v22_removals_appear(self):
        removed = {
            "filters.CHAT": "use filters.ChatType",
            "StatusUpdate.USER_SHARED": "use USERS_SHARED",
            "disable_web_page_preview": "use link_preview_options",
            "proxy_url": "use proxy",
            "EncryptedPassportElement.credentials": "removed with no replacement",
        }
        found = []
        for path in self.SOURCES:
            text = path.read_text()
            for api, replacement in removed.items():
                if api in text:
                    found.append(f"{path.name}: {api} ({replacement})")
        assert not found, "removed in python-telegram-bot 22.0:\n  " + "\n  ".join(found)

    def test_run_polling_passes_no_removed_timeouts(self):
        """The *_timeout arguments moved to ApplicationBuilder in v22.0."""
        text = (REPO / "bot.py").read_text()
        call = text[text.index("run_polling("):][:200]
        for arg in ("read_timeout", "write_timeout", "connect_timeout", "pool_timeout"):
            assert arg not in call, f"run_polling({arg}=...) was removed in v22.0"
