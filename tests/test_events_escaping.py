"""/events must survive any text an admin or a join link can contain.

On 2026-09-17 `/events` failed outright with

    BadRequest: Can't parse entities: can't find end of the entity
                starting at byte offset 2551

because `data/events.yaml` had been seeded from the template and every join link
read `...?pwd=REPLACE_ME`. The underscore in the placeholder is a Markdown
italic marker, and the URL was interpolated into a Markdown message unescaped,
so Telegram rejected the WHOLE listing — no events for anyone.

The placeholder merely exposed it. Event names and announcements are
admin-typed, and a real Zoom password may contain an underscore, so any of them
could have done the same at any time.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytz
from telegram.error import BadRequest

sys.path.insert(0, str(Path(__file__).parent.parent))

import handlers.user_basics as UB  # noqa: E402

TZ = pytz.timezone("America/New_York")

# Fixtures use example.test, never a zoom.us URL. A Zoom link embeds its
# passcode, so a realistic-looking one IS a credential — the first draft of this
# file used real meeting ids copied from the old events.yaml and was caught by
# tests/test_no_secrets_committed.py on commit. Nothing here needs a real host:
# what these tests exercise is the underscore, not the domain.

# The exact string that broke production.
PLACEHOLDER_URL = "https://example.test/j/1234567890?pwd=REPLACE_ME"


def _event(name="God's Holy Convocation—Sabbath Eve", url="", announcements=()):
    return {
        "name": name,
        "service_time": TZ.localize(datetime.now().replace(microsecond=0)) + timedelta(days=1),
        "notification_time": TZ.localize(datetime.now().replace(microsecond=0)),
        "url": url,
        "announcements": list(announcements),
        "type": "convocation",
    }


def _update():
    upd = MagicMock()
    upd.effective_user.id = 7
    upd.effective_user.username = "member"
    upd.effective_user.full_name = "A Member"
    upd.message.reply_text = AsyncMock()
    return upd


async def _events(evs, markdown_ok=True):
    """Run /events; when markdown_ok is False, Telegram rejects Markdown sends."""
    upd = _update()

    async def reply(text, **kwargs):
        if kwargs.get("parse_mode") is not None and not markdown_ok:
            raise BadRequest("can't parse entities: can't find end of the entity")

    upd.message.reply_text = AsyncMock(side_effect=reply)
    with patch.object(UB, "all_upcoming", AsyncMock(return_value=evs)), \
         patch.object(UB, "get_user_prefs", AsyncMock(return_value=(TZ, "en"))), \
         patch.object(UB.activity, "log_command", lambda *a, **k: None):
        await UB.cmd_events(upd, MagicMock())
    return upd


class TestEventsEscaping:
    async def test_underscore_in_a_join_link_is_escaped(self):
        """The exact production failure."""
        upd = await _events([_event(url=PLACEHOLDER_URL)])
        sent = upd.message.reply_text.call_args[0][0]
        assert "REPLACE\\_ME" in sent
        assert "pwd=REPLACE_ME" not in sent

    async def test_a_real_password_with_an_underscore_is_escaped(self):
        """Not just the placeholder — Zoom passwords may contain underscores."""
        url = "https://example.test/j/1234567890?pwd=aB_cD3fG_hJ"
        sent = (await _events([_event(url=url)])).message.reply_text.call_args[0][0]
        assert "aB\\_cD3fG\\_hJ" in sent

    async def test_markdown_in_an_admin_typed_event_name_is_escaped(self):
        sent = (await _events([_event(name="Men's *Special* Service")])) \
            .message.reply_text.call_args[0][0]
        assert "\\*Special\\*" in sent

    async def test_markdown_in_an_announcement_is_escaped(self):
        sent = (await _events([_event(announcements=["Bring _your_ Bible"])])) \
            .message.reply_text.call_args[0][0]
        assert "\\_your\\_" in sent

    async def test_ordinary_links_are_still_readable(self):
        """Escaping must not mangle a link with nothing special in it."""
        url = "https://example.test/j/1234567890"
        sent = (await _events([_event(url=url)])).message.reply_text.call_args[0][0]
        assert url in sent

    async def test_the_event_name_is_still_bold(self):
        """Escaping the CONTENT must not strip our own formatting."""
        sent = (await _events([_event(name="Sabbath Eve")])).message.reply_text.call_args[0][0]
        assert "*Sabbath Eve*" in sent


class TestEventsNeverGoesSilent:
    async def test_falls_back_to_plain_text_when_markdown_fails(self):
        """Belt and braces: one unparseable event must not hide every event."""
        upd = await _events([_event(url=PLACEHOLDER_URL)], markdown_ok=False)
        calls = upd.message.reply_text.call_args_list
        assert len(calls) == 2, "should retry without parse_mode"
        assert calls[1].kwargs.get("parse_mode") is None
        assert "Sabbath" in calls[1].args[0]

    async def test_no_fallback_needed_when_markdown_is_fine(self):
        upd = await _events([_event(url=PLACEHOLDER_URL)], markdown_ok=True)
        assert len(upd.message.reply_text.call_args_list) == 1


class TestTemplatePlaceholderIsSafe:
    """The seeded config must not be able to break /events."""

    @staticmethod
    def _template():
        return (Path(__file__).resolve().parents[1] / "data" / "events.yaml.example").read_text()

    @pytest.mark.parametrize("char", ["_", "*", "`", "[", "]"])
    def test_placeholder_has_no_markdown_characters(self, char):
        import re
        for pwd in set(re.findall(r"pwd=([A-Za-z0-9_.*`\[\]-]+)", self._template())):
            assert char not in pwd, (
                f"template placeholder {pwd!r} contains {char!r}, which Telegram "
                "reads as a formatting marker — a freshly seeded config would "
                "break /events, as it did on 2026-09-17"
            )

    def test_placeholder_says_what_to_do(self):
        """An admin seeing it in a link should know how to replace it."""
        assert "SETSERVICELINK" in self._template().upper()


class TestReminderEscaping:
    """The same defect existed in the reminder path — and that one is on a timer.

    /events is only broken for whoever types it. A reminder that Telegram
    rejects is never delivered at all, and nobody knows to look: the
    congregation simply does not hear about the service.
    """

    @staticmethod
    def _render(url):
        import handlers.notifications as N
        ev = _event(url=url)
        ev["description"] = ""
        return N._render_notification(ev, TZ, "en")

    def test_underscore_in_a_join_link_is_escaped(self):
        assert "REPLACE\\_ME" in self._render(PLACEHOLDER_URL)

    def test_a_real_password_with_an_underscore_is_escaped(self):
        assert "aB\\_cD3fG\\_hJ" in self._render(
            "https://example.test/j/1234567890?pwd=aB_cD3fG_hJ")

    def test_ordinary_links_are_still_readable(self):
        url = "https://example.test/j/1234567890"
        assert url in self._render(url)


class TestReminderNeverGoesSilent:
    @staticmethod
    def _bot(markdown_ok):
        async def send_message(chat_id, text, **kwargs):
            if kwargs.get("parse_mode") is not None and not markdown_ok:
                raise BadRequest("can't parse entities")
        bot = MagicMock()
        bot.send_message = AsyncMock(side_effect=send_message)
        return bot

    async def _deliver(self, markdown_ok):
        import handlers.notifications as N
        bot = self._bot(markdown_ok)
        await N._send_notification_payload(
            bot, 99, {}, "Reminder", {"image": {}, "document": {}})
        return bot

    async def test_falls_back_to_plain_text_when_markdown_fails(self):
        bot = await self._deliver(markdown_ok=False)
        calls = bot.send_message.call_args_list
        assert len(calls) == 2, "should retry without parse_mode"
        assert calls[1].kwargs.get("parse_mode") is None
        assert "Reminder" in calls[1].args[1]

    async def test_no_fallback_needed_when_markdown_is_fine(self):
        bot = await self._deliver(markdown_ok=True)
        assert len(bot.send_message.call_args_list) == 1


class TestServiceLinkCheck:
    """A re-seeded config must be noticed at startup, not by a congregant.

    Nothing reads data/events.yaml until something asks it to, so on 2026-09-17
    the bot ran for a day with ten placeholder links and no way to know.
    """

    @staticmethod
    async def _check(evs):
        import events as E
        with patch.object(E, "all_upcoming", AsyncMock(return_value=evs)):
            return await E.check_service_links()

    async def test_placeholder_link_is_reported(self):
        ev = _event(url="https://example.test/j/1234567890?pwd=SETMEWITHSETSERVICELINK")
        problems = await self._check([ev])
        assert len(problems) == 1
        assert "placeholder" in problems[0][1]

    async def test_missing_link_on_a_convocation_is_reported(self):
        problems = await self._check([_event(url="")])
        assert len(problems) == 1
        assert "no join link" in problems[0][1]

    async def test_a_special_event_without_a_link_is_not_reported(self):
        """A special event may legitimately be in person."""
        ev = _event(url="")
        ev["type"] = "special"
        assert await self._check([ev]) == []

    async def test_a_placeholder_is_reported_even_on_a_special_event(self):
        ev = _event(url="https://example.test/j/9?pwd=SETMEWITHSETSERVICELINK")
        ev["type"] = "special"
        assert len(await self._check([ev])) == 1

    async def test_good_links_produce_no_report(self):
        assert await self._check([_event(url="https://example.test/j/1234567890")]) == []

    async def test_the_label_names_the_date(self):
        """'Sabbath Eve' recurs weekly — an alert must say which one."""
        label = (await self._check([_event(url="")]))[0][0]
        assert any(d in label for d in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"))


class TestStartupAlert:
    @staticmethod
    async def _startup(problems, ops=(555,), raises=None):
        import bot as B
        app = MagicMock()
        app.bot.send_message = AsyncMock()
        check = AsyncMock(side_effect=raises) if raises else AsyncMock(return_value=problems)
        with patch.object(B, "check_service_links", check), \
             patch.object(B.error_reporting, "ops_chat_ids", AsyncMock(return_value=set(ops))):
            await B.alert_on_bad_service_links(app)
        return app

    async def test_ops_are_alerted_and_told_how_to_fix_it(self):
        app = await self._startup([("Sabbath Eve (Fri 18 Sep)", "has no join link")])
        text = app.bot.send_message.call_args.args[1]
        assert "Sabbath Eve" in text and "/setservicelink" in text

    async def test_silent_when_every_link_is_good(self):
        app = await self._startup([])
        app.bot.send_message.assert_not_called()

    async def test_long_lists_are_summarised(self):
        import bot as B
        many = [(f"Service {i}", "has no join link") for i in range(11)]
        text = (await self._startup(many)).bot.send_message.call_args.args[1]
        assert "*11 upcoming service(s)*" in text
        assert f"…and {11 - B.LINK_ALERT_LIMIT} more" in text

    async def test_event_names_are_escaped(self):
        """The alert is Markdown; an admin-typed name must not break it."""
        app = await self._startup([("Men's *Big* Service", "x")])
        text = app.bot.send_message.call_args.args[1]
        assert "\\*Big\\*" in text

    async def test_a_failing_check_never_blocks_startup(self):
        app = await self._startup([], raises=RuntimeError("events.yaml unreadable"))
        app.bot.send_message.assert_not_called()

    async def test_a_failing_send_never_blocks_startup(self):
        """The guard must cover the send, not just the check. Caught by the
        existing wiring tests, which drive post_init with a plain MagicMock."""
        import bot as B
        app = MagicMock()
        app.bot.send_message = MagicMock()  # not awaitable
        with patch.object(B, "check_service_links",
                          AsyncMock(return_value=[("Sabbath Eve", "has no join link")])), \
             patch.object(B.error_reporting, "ops_chat_ids", AsyncMock(return_value={5})):
            await B.alert_on_bad_service_links(app)  # must not raise

    async def test_post_init_actually_runs_the_check(self):
        """Registered is not the same as reached: the check is worthless if
        startup never calls it."""
        import bot as B
        called = AsyncMock()
        app = MagicMock()
        app.bot.set_my_commands = AsyncMock()
        app.bot.set_my_description = AsyncMock()
        app.bot.set_my_short_description = AsyncMock()
        with patch.object(B, "alert_on_bad_service_links", called), \
             patch.object(B, "schedule_all_upcoming", AsyncMock()), \
             patch.object(B.error_reporting, "reset_error_log", lambda *a, **k: None):
            try:
                await B.post_init(app)
            except Exception:
                pass  # later startup steps are out of scope
        called.assert_awaited_once()
