"""/cancel — the shared conversation fallback.

It had never worked. Every conversation used
`lambda u, c: ConversationHandler.END`, a SYNCHRONOUS callback returning the
integer -1. PTB awaits every callback, so this raised
`TypeError: object int can't be used in 'await' expression`, and because the
exception meant no state was returned, the member stayed stuck in the very
conversation they were trying to leave. Several prompts tell people to send
/cancel, so the bot was promising something it could not do.

Found in production on 2026-09-16 (ERR-D616E9) while testing /prayer, after
roughly four months in which no test exercised a fallback.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram.ext import CommandHandler, ConversationHandler

sys.path.insert(0, str(Path(__file__).parent.parent))

import handlers.user_basics as UB  # noqa: E402
from localization import AVAILABLE_LANGUAGES, t  # noqa: E402

LANGS = list(AVAILABLE_LANGUAGES)


def _update():
    upd = MagicMock()
    upd.effective_user.id = 7
    upd.effective_user.username = "member"
    upd.effective_user.full_name = "A Member"
    upd.message.text = "/cancel"
    upd.message.reply_text = AsyncMock()
    return upd


def _ctx(**user_data):
    ctx = MagicMock()
    ctx.user_data = dict(user_data)
    return ctx


async def _cancel(lang="en", **user_data):
    upd, ctx = _update(), _ctx(**user_data)
    with patch.object(UB, "get_user_prefs", AsyncMock(return_value=(None, lang))), \
         patch.object(UB.activity, "log_command", lambda *a, **k: None):
        state = await UB.cancel_conversation(upd, ctx)
    return state, ctx, upd


class TestCancelBehaviour:
    async def test_ends_the_conversation(self):
        state, _, _ = await _cancel()
        assert state == ConversationHandler.END

    async def test_it_is_awaitable(self):
        """The whole bug: a sync callback returning an int cannot be awaited."""
        assert inspect.iscoroutinefunction(UB.cancel_conversation)

    async def test_tells_the_member_it_was_cancelled(self):
        """Silence after /cancel is indistinguishable from the old failure."""
        _, _, upd = await _cancel()
        assert upd.message.reply_text.called
        assert upd.message.reply_text.call_args[0][0].strip()

    async def test_clears_the_draft(self):
        """A half-written announcement must not leak into the next conversation."""
        _, ctx, _ = await _cancel(an_title="Half-written", an_body="draft text")
        assert ctx.user_data == {}

    async def test_logged_for_stats(self):
        upd, ctx = _update(), _ctx()
        with patch.object(UB, "get_user_prefs", AsyncMock(return_value=(None, "en"))), \
             patch.object(UB.activity, "log_command") as log:
            await UB.cancel_conversation(upd, ctx)
        assert log.call_args[0][0] == "cancel"

    @pytest.mark.parametrize("lang", LANGS)
    async def test_localised(self, lang):
        _, _, upd = await _cancel(lang)
        assert upd.message.reply_text.call_args[0][0] == t("action_cancelled", lang)

    @pytest.mark.parametrize("lang", LANGS)
    def test_every_language_has_the_string(self, lang):
        assert t("action_cancelled", lang).strip()


class TestEveryCallbackIsAwaitable:
    """The general form of the bug, not just the instance that was found.

    PTB awaits every handler callback. Any synchronous one fails at runtime with
    a TypeError that names the library rather than the handler, which is why
    this survived so long. Walk everything main() registers and check.
    """

    @staticmethod
    def _handlers():
        import bot
        app = MagicMock()
        seen: list = []
        app.add_handler.side_effect = lambda h, group=0: seen.append(h)
        builder = MagicMock()
        builder.token.return_value = builder
        builder.post_init.return_value = builder
        builder.build.return_value = app
        with patch.object(bot, "Application") as App:
            App.builder.return_value = builder
            bot.main()
        return seen

    @staticmethod
    def _callbacks(handler):
        """(label, callback) for a handler, descending into conversations."""
        out = []
        if isinstance(handler, ConversationHandler):
            for entry in handler.entry_points:
                out += [(f"entry_point {getattr(entry, 'commands', entry)}", entry.callback)]
            for state, hs in (handler.states or {}).items():
                for h in hs:
                    out.append((f"state {state}", h.callback))
            for fb in handler.fallbacks:
                out.append((f"fallback {getattr(fb, 'commands', fb)}", fb.callback))
        elif hasattr(handler, "callback"):
            out.append((type(handler).__name__, handler.callback))
        return out

    def test_no_synchronous_callbacks_anywhere(self):
        bad = []
        for h in self._handlers():
            for label, cb in self._callbacks(h):
                if not inspect.iscoroutinefunction(cb):
                    bad.append(f"{type(h).__name__}: {label} → {getattr(cb, '__name__', cb)}")
        assert not bad, (
            "these callbacks are not async; PTB awaits every callback, so each "
            "raises TypeError at runtime:\n  " + "\n  ".join(bad)
        )

    def test_the_walk_actually_finds_callbacks(self):
        """Guard against the check above passing because it inspected nothing."""
        total = sum(len(self._callbacks(h)) for h in self._handlers())
        assert total > 40, f"only {total} callbacks inspected — the walk is broken"

    def test_every_conversation_has_a_cancel_fallback(self):
        """Prompts tell members they can leave; every conversation must let them."""
        missing = []
        for h in self._handlers():
            if not isinstance(h, ConversationHandler):
                continue
            cancels = [fb for fb in h.fallbacks
                       if isinstance(fb, CommandHandler) and "cancel" in fb.commands]
            if not cancels:
                entry = h.entry_points[0] if h.entry_points else "?"
                missing.append(str(getattr(entry, "commands", entry)))
        assert not missing, f"conversations with no /cancel fallback: {missing}"

    def test_all_cancel_fallbacks_use_the_shared_handler(self):
        """One implementation, so a fix or a wording change lands everywhere."""
        wrong = []
        for h in self._handlers():
            if not isinstance(h, ConversationHandler):
                continue
            for fb in h.fallbacks:
                if isinstance(fb, CommandHandler) and "cancel" in fb.commands:
                    if fb.callback is not UB.cancel_conversation:
                        wrong.append(getattr(fb.callback, "__name__", str(fb.callback)))
        assert not wrong, f"/cancel fallbacks not using cancel_conversation: {wrong}"


class TestHandlerOrdering:
    """Registration order is load-bearing and invisible in review.

    PTB runs only the FIRST matching handler per group. bot.py registers a
    catch-all `filters.TEXT & ~filters.COMMAND` handler for counter-propose
    replies; any ConversationHandler registered AFTER it never receives the
    member's text, because the catch-all matches first and — finding no pending
    counter-proposal — returns silently.

    That is not a crash, so nothing appears in errors.log. /prayer shipped this
    way in v0.17.0: the member typed their request and the bot said nothing.
    """

    @staticmethod
    def _group0():
        import bot
        app = MagicMock()
        seen: list = []

        def add(handler, group=0):
            seen.append((group, handler))

        app.add_handler.side_effect = add
        builder = MagicMock()
        builder.token.return_value = builder
        builder.post_init.return_value = builder
        builder.build.return_value = app
        with patch.object(bot, "Application") as App:
            App.builder.return_value = builder
            bot.main()
        return [h for g, h in seen if g == 0]

    @staticmethod
    def _is_text_catch_all(h):
        """A MessageHandler that matches ordinary text with no further filter."""
        from telegram.ext import MessageHandler
        if not isinstance(h, MessageHandler) or isinstance(h, ConversationHandler):
            return False
        return "TEXT" in str(h.filters) and "COMMAND" in str(h.filters)

    def test_every_conversation_is_registered_before_the_text_catch_all(self):
        handlers = self._group0()
        first_catch_all = next(
            (i for i, h in enumerate(handlers) if self._is_text_catch_all(h)), None)
        assert first_catch_all is not None, (
            "no catch-all text handler found — this test is no longer checking "
            "anything; update it if the handler was removed"
        )
        late = []
        for i, h in enumerate(handlers[first_catch_all + 1:], first_catch_all + 1):
            if isinstance(h, ConversationHandler):
                entry = h.entry_points[0] if h.entry_points else "?"
                late.append(str(getattr(entry, "commands", entry)))
        assert not late, (
            "these conversations are registered AFTER the catch-all text "
            f"handler and will never receive their own replies: {late}"
        )

    def test_the_catch_all_is_actually_found(self):
        """Guard the guard: if the detector stops matching, the test above
        would pass while checking nothing."""
        handlers = self._group0()
        assert sum(1 for h in handlers if self._is_text_catch_all(h)) >= 1
