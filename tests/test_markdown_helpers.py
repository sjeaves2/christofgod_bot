"""The three fallback helpers in common.

Between a message that loses its bold and a message that is never delivered,
the second is far worse — Telegram rejects a whole message over one unpaired
marker, and nobody sees an error. These helpers make the first outcome happen
instead of the second.

They are a seatbelt, not the fix. Escaping with md() is the fix, which is why
every fallback logs: a silent one would hide the escaping bug it compensates
for. The log assertion below is deliberate.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from telegram.constants import ParseMode
from telegram.error import BadRequest

sys.path.insert(0, str(Path(__file__).parent.parent))

from common import edit_markdown, reply_markdown, send_markdown  # noqa: E402


def _target(fail_markdown, exc=None):
    """A send that rejects Markdown but accepts plain text."""
    calls = []

    async def send(*args, **kwargs):
        calls.append((args, kwargs))
        if kwargs.get("parse_mode") is not None and fail_markdown:
            raise exc or BadRequest("can't parse entities")
        return "sent"

    return AsyncMock(side_effect=send), calls


class TestReplyMarkdown:
    async def test_sends_as_markdown_when_accepted(self):
        msg = MagicMock()
        msg.reply_text, calls = _target(fail_markdown=False)
        await reply_markdown(msg, "hello")
        assert len(calls) == 1
        assert calls[0][1]["parse_mode"] == ParseMode.MARKDOWN

    async def test_falls_back_to_plain_text(self):
        msg = MagicMock()
        msg.reply_text, calls = _target(fail_markdown=True)
        await reply_markdown(msg, "a_b")
        assert len(calls) == 2
        assert calls[1][1].get("parse_mode") is None
        assert calls[1][0][0] == "a_b", "the text itself must be unchanged"

    async def test_keyword_arguments_survive_the_retry(self):
        """A dropped reply_markup would silently remove the buttons."""
        msg = MagicMock()
        msg.reply_text, calls = _target(fail_markdown=True)
        await reply_markdown(msg, "x", reply_markup="KB")
        assert calls[0][1]["reply_markup"] == "KB"
        assert calls[1][1]["reply_markup"] == "KB"

    async def test_the_fallback_is_logged(self, caplog):
        """A silent fallback would hide the escaping bug that caused it."""
        msg = MagicMock()
        msg.reply_text, _ = _target(fail_markdown=True)
        with caplog.at_level(logging.WARNING, logger="common"):
            await reply_markdown(msg, "a_b")
        assert "unescaped" in caplog.text.lower()

    async def test_other_errors_are_not_swallowed(self):
        """Only a parse failure is retried; a real fault must surface."""
        msg = MagicMock()
        msg.reply_text = AsyncMock(side_effect=RuntimeError("network down"))
        try:
            await reply_markdown(msg, "x")
        except RuntimeError:
            return
        raise AssertionError("a non-Telegram error must propagate")


class TestSendMarkdown:
    async def test_falls_back_and_keeps_the_chat_id(self):
        bot = MagicMock()
        bot.send_message, calls = _target(fail_markdown=True)
        await send_markdown(bot, 4242, "a_b")
        assert len(calls) == 2
        assert calls[0][0][0] == 4242 and calls[1][0][0] == 4242
        assert calls[1][1].get("parse_mode") is None

    async def test_sends_once_when_markdown_is_fine(self):
        bot = MagicMock()
        bot.send_message, calls = _target(fail_markdown=False)
        await send_markdown(bot, 1, "hi")
        assert len(calls) == 1


class TestEditMarkdown:
    async def test_falls_back_to_plain_text(self):
        q = MagicMock()
        q.edit_message_text, calls = _target(fail_markdown=True)
        await edit_markdown(q, "a_b")
        assert len(calls) == 2

    async def test_message_is_not_modified_is_not_an_escaping_fault(self):
        """Telegram rejects an edit that changes nothing. There is nothing to
        retry and nothing to warn about — it must not be reported as one."""
        q = MagicMock()
        q.edit_message_text, calls = _target(
            fail_markdown=True, exc=BadRequest("Message is not modified"))
        result = await edit_markdown(q, "same")
        assert len(calls) == 1, "must not retry an unchanged edit"
        assert result is None

    async def test_a_failing_plain_retry_does_not_raise(self):
        """An edit can fail for reasons the retry cannot fix (too old, deleted).
        Losing an edit must not take down the handler around it."""
        q = MagicMock()
        q.edit_message_text = AsyncMock(side_effect=BadRequest("can't parse entities"))
        assert await edit_markdown(q, "x") is None
