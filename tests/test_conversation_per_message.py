"""The signal behind the warning we silence in pyproject.toml.

python-telegram-bot warns that with per_message=False a CallbackQueryHandler
is not tracked for every message. Every conversation in bot.py is entered by a
CommandHandler, so per_message=False is CORRECT for all of them: per_message=True
requires every entry point, state handler and fallback to be a
CallbackQueryHandler, and setting it would break the command entry (and emit
three warnings instead of one — verified 2026-10-20).

So the advisory is unfixable and was repeated 90 times a run: six handlers
times the ~15 tests that build the application. Real warnings would be lost in
it, which is how warnings stop being read.

pyproject.toml filters that exact message. This test is the other half of the
bargain: it fails if a conversation ever appears for which per_message=True
WOULD be right — a purely callback-driven one — so the information the filter
hides is recovered at the only moment it matters.

Silence an advisory only with something that tells you when it stops being
true.
"""

from __future__ import annotations

import sys
import warnings
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from telegram.ext import CallbackQueryHandler, ConversationHandler

sys.path.insert(0, str(Path(__file__).parent.parent))


def _conversations() -> list[ConversationHandler]:
    import bot

    class FakeApp:
        def __init__(self):
            self.handlers = []
            self.job_queue = MagicMock()
            self.bot = MagicMock()
            self.bot.set_my_commands = AsyncMock()

        def add_handler(self, h, group=0):
            self.handlers.append(h)

        def add_error_handler(self, h):
            pass

        def run_polling(self, **kw):
            pass

    fake = FakeApp()

    class FakeBuilder:
        def token(self, t):
            return self

        def post_init(self, f):
            return self

        def build(self):
            return fake

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with patch.object(bot.Application, "builder",
                          staticmethod(lambda: FakeBuilder())):
            bot.main()
    return [h for h in fake.handlers if isinstance(h, ConversationHandler)]


def _all_callback(handlers) -> bool:
    return bool(handlers) and all(isinstance(h, CallbackQueryHandler) for h in handlers)


class TestNoConversationWantsPerMessage:
    def test_the_sweep_sees_the_conversations(self):
        """A sweep over nothing passes vacuously."""
        assert len(_conversations()) >= 10

    def test_no_conversation_is_purely_callback_driven(self):
        """The one case where per_message=True would be correct.

        If this fails, a new conversation is entered and driven only by
        callback queries. Set per_message=True on THAT handler and add it to
        the exemption below — and do not widen the filter in pyproject.toml.
        """
        offenders = []
        for conv in _conversations():
            everything = list(conv.entry_points)
            for state_handlers in (conv.states or {}).values():
                everything += list(state_handlers)
            everything += list(conv.fallbacks or [])
            if _all_callback(everything) and not conv.per_message:
                offenders.append(conv.name or "<unnamed>")
        assert not offenders, (
            "these are driven entirely by callback queries, so per_message=True "
            f"is correct for them and the filtered warning was real: {offenders}"
        )

    def test_every_conversation_is_entered_by_a_command(self):
        """Why per_message=False is right here: a command entry cannot be
        tracked per message, so per_message=True would break it."""
        for conv in _conversations():
            assert not _all_callback(conv.entry_points), (
                f"{conv.name!r} is entered only by callback queries"
            )


class TestTheFilterStaysNarrow:
    @staticmethod
    def _filters():
        import tomllib
        cfg = tomllib.loads((Path(__file__).resolve().parents[1]
                             / "pyproject.toml").read_text())
        return cfg["tool"]["pytest"]["ini_options"].get("filterwarnings", [])

    def test_only_the_known_advisory_is_ignored(self):
        """One blanket 'ignore' would hide everything this suite might tell us."""
        for entry in self._filters():
            if entry.startswith("ignore"):
                assert "per_message=False" in entry, (
                    f"filter {entry!r} is broader than the one advisory this "
                    "was agreed for"
                )

    def test_the_filter_is_still_there(self):
        """If it is removed, the 90 warnings return and this test file is the
        only thing explaining why they were ever acceptable."""
        assert any("per_message=False" in f for f in self._filters())
