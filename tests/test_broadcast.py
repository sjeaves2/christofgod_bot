"""Tests for /broadcast — admin message to groups and/or all subscribers."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _ctx():
    ctx = MagicMock()
    ctx.user_data = {}
    ctx.application = MagicMock()
    ctx.application.bot = MagicMock()
    ctx.application.bot.send_message = AsyncMock()
    return ctx


def _update(chat_id=1, text=None):
    upd = MagicMock()
    upd.effective_chat.id = chat_id
    upd.effective_user.id = chat_id
    upd.effective_user.username = "admin"
    upd.effective_user.full_name = "Admin User"
    if text is not None:
        upd.message.text = text
        upd.message.reply_text = AsyncMock()
    return upd


# ---------------------------------------------------------------------------
# Target option assembly
# ---------------------------------------------------------------------------

class TestTargetOptions:
    def _run_opts(self, known_groups, registry):
        import bot

        async def _kg():
            return known_groups

        async def _ev():
            return {"notification_targets": registry}

        with patch("bot._load_known_groups", side_effect=_kg), \
             patch("bot.get_all_events_data", side_effect=_ev):
            return _run(bot._broadcast_target_options())

    def test_all_subscribers_always_first(self):
        opts = self._run_opts({}, {})
        assert opts[0]["kind"] == "all"
        assert opts[0]["key"] == "all"

    def test_includes_tracked_groups(self):
        opts = self._run_opts(
            {"-100": {"chat_id": -100, "title": "COGM Members", "status": "member"}}, {})
        labels = [o["label"] for o in opts]
        assert "COGM Members" in labels

    def test_includes_registry_groups(self):
        opts = self._run_opts({}, {"cogm_members": -1001178984510})
        ids = [o["chat_id"] for o in opts if o["kind"] == "group"]
        assert -1001178984510 in ids

    def test_union_dedupes_by_chat_id(self):
        opts = self._run_opts(
            {"-100": {"chat_id": -100, "title": "Tracked", "status": "member"}},
            {"same": -100},
        )
        groups = [o for o in opts if o["kind"] == "group" and o["chat_id"] == -100]
        assert len(groups) == 1  # tracked entry wins, registry duplicate skipped


# ---------------------------------------------------------------------------
# Recipient expansion
# ---------------------------------------------------------------------------

class TestExpandRecipients:
    def _expand(self, options, selected, users):
        import bot

        async def _users():
            return users

        with patch("bot.get_all_users", side_effect=_users):
            return _run(bot._bc_expand_recipients(options, set(selected)))

    def test_all_expands_to_subscribers(self):
        opts = [{"key": "all", "kind": "all", "chat_id": None, "label": "All subscribers"}]
        recips = self._expand(opts, ["all"], [{"chat_id": 1, "display_name": "A"},
                                              {"chat_id": 2, "display_name": "B"}])
        assert {r["chat_id"] for r in recips} == {1, 2}
        assert all(r["kind"] == "user" for r in recips)

    def test_group_selection_adds_group(self):
        opts = [{"key": "-100", "kind": "group", "chat_id": -100, "label": "G"}]
        recips = self._expand(opts, ["-100"], [])
        assert recips == [{"kind": "group", "chat_id": -100, "label": "G"}]

    def test_mixed_selection(self):
        opts = [
            {"key": "all", "kind": "all", "chat_id": None, "label": "All subscribers"},
            {"key": "-100", "kind": "group", "chat_id": -100, "label": "G"},
        ]
        recips = self._expand(opts, ["all", "-100"], [{"chat_id": 5, "display_name": "E"}])
        kinds = {(r["kind"], r["chat_id"]) for r in recips}
        assert ("user", 5) in kinds
        assert ("group", -100) in kinds


# ---------------------------------------------------------------------------
# Sending + retry loop
# ---------------------------------------------------------------------------

class TestSendAndRetry:
    def _prime(self, ctx, recipients, message="Hello *world*"):
        ctx.user_data["bc_message"] = message
        ctx.user_data["bc_recipients"] = recipients
        ctx.user_data["bc_done"] = set()
        ctx.user_data["bc_retries"] = 0

    def test_send_pending_skips_done(self):
        import bot
        ctx = _ctx()
        self._prime(ctx, [{"kind": "user", "chat_id": 1, "label": "A"},
                          {"kind": "user", "chat_id": 2, "label": "B"}])
        ctx.user_data["bc_done"] = {1}
        failures = _run(bot._bc_send_pending(ctx.application.bot, ctx))
        assert failures == []
        # Only chat 2 was (re)sent.
        assert ctx.application.bot.send_message.await_count == 1
        assert ctx.application.bot.send_message.await_args[0][0] == 2

    def test_failure_recorded_and_not_marked_done(self):
        import bot
        from telegram.error import NetworkError
        ctx = _ctx()
        ctx.application.bot.send_message = AsyncMock(side_effect=NetworkError("x"))
        self._prime(ctx, [{"kind": "group", "chat_id": -100, "label": "G"}])
        failures = _run(bot._bc_send_pending(ctx.application.bot, ctx))
        assert len(failures) == 1
        assert ctx.user_data["bc_done"] == set()

    def test_all_success_ends_conversation(self):
        import bot
        ctx = _ctx()
        self._prime(ctx, [{"kind": "user", "chat_id": 1, "label": "A"}])
        upd = _update(chat_id=99)
        result = _run(bot._bc_attempt_and_prompt(upd, ctx))
        assert result == bot.ConversationHandler.END
        # Confirmation message sent to admin chat.
        assert any("delivered to all" in str(c.args[1]).lower()
                   for c in ctx.application.bot.send_message.await_args_list)

    def test_partial_failure_prompts_retry(self):
        import bot
        from telegram.error import NetworkError
        ctx = _ctx()

        async def half(chat_id, text, **kw):
            if chat_id == 2:
                raise NetworkError("x")

        ctx.application.bot.send_message = AsyncMock(side_effect=half)
        self._prime(ctx, [{"kind": "user", "chat_id": 1, "label": "A"},
                          {"kind": "user", "chat_id": 2, "label": "B"}])
        upd = _update(chat_id=99)
        result = _run(bot._bc_attempt_and_prompt(upd, ctx))
        assert result == bot.BC_RETRY
        assert ctx.user_data["bc_done"] == {1}

    def test_retry_limit_stops_without_prompt(self):
        import bot
        from telegram.error import NetworkError
        ctx = _ctx()

        async def fail_recipient(chat_id, text, **kw):
            if chat_id == 1:  # recipient fails; admin chat (99) succeeds
                raise NetworkError("x")

        ctx.application.bot.send_message = AsyncMock(side_effect=fail_recipient)
        self._prime(ctx, [{"kind": "user", "chat_id": 1, "label": "A"}])
        ctx.user_data["bc_retries"] = bot.BC_MAX_RETRIES  # already at limit
        upd = _update(chat_id=99)
        result = _run(bot._bc_attempt_and_prompt(upd, ctx))
        assert result == bot.ConversationHandler.END
        assert any("retry limit" in str(c.args[1]).lower()
                   for c in ctx.application.bot.send_message.await_args_list)

    def test_retry_yes_increments_and_resends(self):
        import bot
        ctx = _ctx()
        self._prime(ctx, [{"kind": "user", "chat_id": 1, "label": "A"}])
        query = MagicMock()
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        query.data = "bc:retry:yes"
        upd = MagicMock()
        upd.callback_query = query
        upd.effective_chat.id = 99
        upd.effective_user.id = 99
        upd.effective_user.username = "a"
        upd.effective_user.full_name = "Admin"
        result = _run(bot.bc_retry(upd, ctx))
        assert ctx.user_data["bc_retries"] == 1
        # Succeeds on retry → conversation ends.
        assert result == bot.ConversationHandler.END

    def test_retry_no_ends(self):
        import bot
        ctx = _ctx()
        self._prime(ctx, [{"kind": "user", "chat_id": 1, "label": "A"},
                          {"kind": "user", "chat_id": 2, "label": "B"}])
        ctx.user_data["bc_done"] = {1}
        query = MagicMock()
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        query.data = "bc:retry:no"
        upd = MagicMock()
        upd.callback_query = query
        result = _run(bot.bc_retry(upd, ctx))
        assert result == bot.ConversationHandler.END
        assert "1/2" in query.edit_message_text.await_args[0][0]


# ---------------------------------------------------------------------------
# Message preview / markdown validation
# ---------------------------------------------------------------------------

class TestMessagePreview:
    def test_bad_markdown_stays_in_message_state(self):
        import bot
        from telegram.error import BadRequest
        ctx = _ctx()
        upd = _update(text="bad *markdown")

        async def reply(text, **kwargs):
            # The markdown preview fails to parse; the plain error reply succeeds.
            if kwargs.get("parse_mode") is not None:
                raise BadRequest("can't parse entities")

        upd.message.reply_text = AsyncMock(side_effect=reply)
        result = _run(bot.bc_message(upd, ctx))
        assert result == bot.BC_MESSAGE
        assert "bc_message" not in ctx.user_data

    def test_good_markdown_advances_to_select(self):
        import bot

        async def _opts():
            return [{"key": "all", "kind": "all", "chat_id": None, "label": "All subscribers"}]

        ctx = _ctx()
        upd = _update(text="Good *message*")
        with patch("bot._broadcast_target_options", side_effect=_opts):
            result = _run(bot.bc_message(upd, ctx))
        assert result == bot.BC_SELECT
        assert ctx.user_data["bc_message"] == "Good *message*"


# ---------------------------------------------------------------------------
# Selection toggling
# ---------------------------------------------------------------------------

class TestSelectionToggle:
    def _ctx_with_options(self):
        ctx = _ctx()
        ctx.user_data["bc_options"] = [
            {"key": "all", "kind": "all", "chat_id": None, "label": "All subscribers"},
            {"key": "-100", "kind": "group", "chat_id": -100, "label": "G"},
        ]
        ctx.user_data["bc_selected"] = set()
        return ctx

    def _query_update(self, data):
        query = MagicMock()
        query.answer = AsyncMock()
        query.edit_message_reply_markup = AsyncMock()
        query.edit_message_text = AsyncMock()
        query.data = data
        upd = MagicMock()
        upd.callback_query = query
        upd.effective_chat.id = 99
        return upd, query

    def test_toggle_adds_selection(self):
        import bot
        ctx = self._ctx_with_options()
        upd, query = self._query_update("bc:toggle:all")
        result = _run(bot.bc_select(upd, ctx))
        assert "all" in ctx.user_data["bc_selected"]
        assert result == bot.BC_SELECT

    def test_toggle_twice_removes(self):
        import bot
        ctx = self._ctx_with_options()
        upd, query = self._query_update("bc:toggle:-100")
        _run(bot.bc_select(upd, ctx))
        _run(bot.bc_select(upd, ctx))
        assert "-100" not in ctx.user_data["bc_selected"]

    def test_send_with_no_selection_alerts(self):
        import bot
        ctx = self._ctx_with_options()
        upd, query = self._query_update("bc:send")
        result = _run(bot.bc_select(upd, ctx))
        assert result == bot.BC_SELECT
        query.answer.assert_awaited()

    def test_cancel_ends(self):
        import bot
        ctx = self._ctx_with_options()
        upd, query = self._query_update("bc:cancel")
        result = _run(bot.bc_select(upd, ctx))
        assert result == bot.ConversationHandler.END
