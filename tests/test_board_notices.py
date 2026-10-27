"""The 6am board notice job.

The schedule is tested in test_board_items.py. What is tested here is DELIVERY,
and the one decision that matters most:

**An item is marked notified only if at least one officer actually received
it.** If every send fails — Telegram unreachable, which happened here for 87
minutes on 2026-09-04 — the item must keep its old `last_notified` and be tried
again tomorrow. Marking it would record a notice that never arrived, and the
item would then go quiet for a week (or forever, inside the daily window, if
the failure repeats on the day it is finally due).

The other half is isolation: one officer who has blocked the bot must not cost
the other three their notice.
"""

from __future__ import annotations

import logging
import sys
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from telegram.error import Forbidden, TelegramError

sys.path.insert(0, str(Path(__file__).parent.parent))

import board_items as bi
import handlers.board as hb
import permissions


def _item(**over):
    base = {"id": "AAA11111", "action": "Renew logo registration",
            "due": (date.today() + timedelta(days=10)).isoformat(),
            "notify_days": 60, "status": "pending", "description": "",
            "url": "", "history": []}
    base.update(over)
    return base


class FakeStore:
    def __init__(self, items=None, officers=(111, 222)):
        self.items = items if items is not None else [_item()]
        self.users = [{"chat_id": c, "display_name": f"Officer {c}",
                       "board_officer": True} for c in officers]
        self.saves = 0

    def install(self, monkeypatch):
        async def get_board_items():
            return self.items

        async def save_board_items(items):
            self.items = items
            self.saves += 1

        async def get_all_users():
            return self.users

        for module in (hb, permissions):
            monkeypatch.setattr(module.storage, "get_board_items", get_board_items,
                                raising=False)
            monkeypatch.setattr(module.storage, "save_board_items", save_board_items,
                                raising=False)
            monkeypatch.setattr(module.storage, "get_all_users", get_all_users)
        return self


class FakeBot:
    """Records sends; optionally fails for specific chats."""

    def __init__(self, fail_for=(), fail_all=False, fail_with=None):
        self.sent: list[tuple[int, str, object]] = []
        self.fail_for = set(fail_for)
        self.fail_all = fail_all
        self.fail_with = fail_with or Forbidden("bot was blocked by the user")

    async def send_message(self, chat_id, text, **kwargs):
        if self.fail_all or chat_id in self.fail_for:
            raise self.fail_with
        self.sent.append((chat_id, text, kwargs.get("reply_markup")))
        return MagicMock()


def _ctx(bot):
    ctx = MagicMock()
    ctx.bot = bot
    return ctx


def recipients(bot) -> list[int]:
    return [chat_id for chat_id, _t, _m in bot.sent]


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


class TestDelivery:
    async def test_a_due_item_reaches_every_officer(self, monkeypatch):
        FakeStore().install(monkeypatch)
        bot = FakeBot()
        await hb.board_notice_job(_ctx(bot))
        assert sorted(recipients(bot)) == [111, 222]

    async def test_the_notice_names_the_action_and_the_date(self, monkeypatch):
        due = (date.today() + timedelta(days=10)).isoformat()
        FakeStore(items=[_item(due=due)]).install(monkeypatch)
        bot = FakeBot()
        await hb.board_notice_job(_ctx(bot))
        text = bot.sent[0][1]
        assert "Renew logo registration" in text
        assert due in text
        assert "10 days" in text

    async def test_a_due_item_is_marked_notified(self, monkeypatch):
        store = FakeStore().install(monkeypatch)
        await hb.board_notice_job(_ctx(FakeBot()))
        assert store.items[0]["last_notified"] == date.today().isoformat()
        assert store.saves == 1

    async def test_an_item_not_yet_due_a_notice_is_left_alone(self, monkeypatch):
        far_off = _item(due=(date.today() + timedelta(days=200)).isoformat())
        store = FakeStore(items=[far_off]).install(monkeypatch)
        bot = FakeBot()
        await hb.board_notice_job(_ctx(bot))
        assert bot.sent == []
        assert store.saves == 0
        assert "last_notified" not in store.items[0]

    async def test_running_twice_in_a_day_sends_once(self, monkeypatch):
        """A restart at 06:05 must not re-send the 06:00 notice."""
        FakeStore().install(monkeypatch)
        bot = FakeBot()
        await hb.board_notice_job(_ctx(bot))
        await hb.board_notice_job(_ctx(bot))
        assert len(bot.sent) == 2, "one item, two officers, one day"

    async def test_completed_and_deleted_items_are_never_sent(self, monkeypatch):
        store = FakeStore(items=[
            _item(id="C", status="completed"),
            _item(id="D", status="deleted"),
        ]).install(monkeypatch)
        bot = FakeBot()
        await hb.board_notice_job(_ctx(bot))
        assert bot.sent == []
        assert store.saves == 0

    async def test_several_due_items_are_all_sent_and_saved_once(self, monkeypatch):
        store = FakeStore(items=[
            _item(id="A", action="First", due=(date.today() + timedelta(days=3)).isoformat()),
            _item(id="B", action="Second", due=(date.today() + timedelta(days=9)).isoformat()),
        ]).install(monkeypatch)
        bot = FakeBot()
        await hb.board_notice_job(_ctx(bot))
        assert len(bot.sent) == 4, "two items × two officers"
        assert store.saves == 1, "the file is one document; save it once"

    async def test_notices_arrive_soonest_deadline_first(self, monkeypatch):
        """Four messages at 6am are a priority list, so their order is part of
        the message. bi.pending() sorts; notice_due() alone would leave them in
        file order, which is the order they happened to be added in."""
        FakeStore(items=[
            _item(id="L", action="Later one",
                  due=(date.today() + timedelta(days=18)).isoformat()),
            _item(id="S", action="Sooner one",
                  due=(date.today() + timedelta(days=2)).isoformat()),
        ], officers=(111,)).install(monkeypatch)
        bot = FakeBot()
        await hb.board_notice_job(_ctx(bot))
        order = [t for _c, t, _m in bot.sent]
        assert len(order) == 2
        assert "Sooner one" in order[0] and "Later one" in order[1]

    async def test_an_empty_store_does_nothing(self, monkeypatch):
        store = FakeStore(items=[]).install(monkeypatch)
        bot = FakeBot()
        await hb.board_notice_job(_ctx(bot))
        assert bot.sent == [] and store.saves == 0

    async def test_an_overdue_item_keeps_being_sent(self, monkeypatch):
        """The one failure mode that would make this feature harmful."""
        overdue = _item(due=(date.today() - timedelta(days=4)).isoformat())
        FakeStore(items=[overdue]).install(monkeypatch)
        bot = FakeBot()
        await hb.board_notice_job(_ctx(bot))
        assert len(bot.sent) == 2
        assert "OVERDUE" in bot.sent[0][1]


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------


class TestFailureHandling:
    async def test_one_blocked_officer_does_not_cost_the_others(self, monkeypatch):
        FakeStore(officers=(111, 222, 333)).install(monkeypatch)
        bot = FakeBot(fail_for=(222,))
        await hb.board_notice_job(_ctx(bot))
        assert sorted(recipients(bot)) == [111, 333]

    async def test_a_partial_delivery_still_marks_the_item(self, monkeypatch):
        """Otherwise the three who received it get it again tomorrow, and the
        day after, for as long as the fourth stays blocked."""
        store = FakeStore(officers=(111, 222)).install(monkeypatch)
        await hb.board_notice_job(_ctx(FakeBot(fail_for=(222,))))
        assert store.items[0]["last_notified"] == date.today().isoformat()

    async def test_a_total_failure_does_not_mark_the_item(self, monkeypatch):
        """Telegram unreachable. The notice must not be recorded as delivered —
        the item would then go quiet for a week."""
        store = FakeStore().install(monkeypatch)
        await hb.board_notice_job(_ctx(FakeBot(fail_all=True)))
        assert "last_notified" not in store.items[0]
        assert store.saves == 0

    async def test_after_a_total_failure_tomorrow_still_sends(self, monkeypatch):
        FakeStore().install(monkeypatch)
        await hb.board_notice_job(_ctx(FakeBot(fail_all=True)))
        bot = FakeBot()
        await hb.board_notice_job(_ctx(bot))
        assert len(bot.sent) == 2

    async def test_a_failure_on_one_item_does_not_stop_the_next(self, monkeypatch):
        store = FakeStore(items=[
            _item(id="A", action="First", due=(date.today() + timedelta(days=3)).isoformat()),
            _item(id="B", action="Second", due=(date.today() + timedelta(days=9)).isoformat()),
        ], officers=(111,)).install(monkeypatch)

        bot = FakeBot()
        original = bot.send_message
        calls = {"n": 0}

        async def flaky(chat_id, text, **kwargs):
            calls["n"] += 1
            if "First" in text:
                raise TelegramError("transient")
            return await original(chat_id, text, **kwargs)

        bot.send_message = flaky
        await hb.board_notice_job(_ctx(bot))
        assert calls["n"] == 2, "the second item was never attempted"
        assert len(bot.sent) == 1
        assert "Second" in bot.sent[0][1]
        by_id = {i["id"]: i for i in store.items}
        assert "last_notified" not in by_id["A"]
        assert by_id["B"]["last_notified"] == date.today().isoformat()

    async def test_no_officers_sends_nothing_and_marks_nothing(self, monkeypatch):
        """Obligations tracked and nobody told. Nothing is marked, so the
        notices start arriving the morning after the flag is set."""
        store = FakeStore(officers=()).install(monkeypatch)
        bot = FakeBot()
        await hb.board_notice_job(_ctx(bot))
        assert bot.sent == []
        assert "last_notified" not in store.items[0]
        assert store.saves == 0

    async def test_no_officers_is_logged_as_a_fault(self, monkeypatch, caplog):
        """It is a configuration error, not a quiet day — it must be visible."""
        FakeStore(officers=()).install(monkeypatch)
        with caplog.at_level(logging.WARNING, logger=hb.logger.name):
            await hb.board_notice_job(_ctx(FakeBot()))
        assert any("board_officer" in r.message or "board_officer" in r.getMessage()
                   for r in caplog.records), caplog.text

    async def test_an_item_reaching_nobody_is_logged(self, monkeypatch):
        FakeStore().install(monkeypatch)
        warnings: list[str] = []
        monkeypatch.setattr(hb.logger, "warning",
                            lambda msg, *a: warnings.append(msg % a if a else msg))
        await hb.board_notice_job(_ctx(FakeBot(fail_all=True)))
        assert any("reached nobody" in w for w in warnings), warnings


# ---------------------------------------------------------------------------
# The link button
# ---------------------------------------------------------------------------


class TestNoticeButton:
    async def test_a_good_link_rides_on_a_button(self, monkeypatch):
        FakeStore(items=[_item(url="https://example.test/renew")]).install(monkeypatch)
        bot = FakeBot()
        await hb.board_notice_job(_ctx(bot))
        markup = bot.sent[0][2]
        assert markup is not None
        assert markup.inline_keyboard[0][0].url == "https://example.test/renew"

    async def test_no_link_means_no_button(self, monkeypatch):
        FakeStore(items=[_item(url="")]).install(monkeypatch)
        bot = FakeBot()
        await hb.board_notice_job(_ctx(bot))
        assert bot.sent[0][2] is None

    def test_an_unacceptable_link_never_becomes_a_button(self):
        """Telegram rejects the ENTIRE message over a bad button url, so the
        notice would simply never arrive."""
        for bad in ("zoom.us/j/1", "ask Pastor", "https://ex ample.test", None, ""):
            assert hb.notice_markup(_item(url=bad)) is None, bad

    def test_an_unacceptable_link_is_shown_as_text_instead(self):
        """An officer pasted it for a reason; dropping it silently is worse."""
        text = hb.render_notice(_item(url="ask Pastor for the_link"), date.today())
        assert r"the\_link" in text

    async def test_a_rejected_button_falls_back_to_a_plain_notice(self, monkeypatch):
        """Belt and braces: if Telegram refuses the button anyway, the deadline
        matters more than the button."""
        FakeStore(items=[_item(url="https://example.test/renew")],
                  officers=(111,)).install(monkeypatch)
        bot = FakeBot()
        plain = []

        async def picky(chat_id, text, **kwargs):
            if kwargs.get("reply_markup") is not None:
                raise TelegramError("BUTTON_URL_INVALID")
            plain.append(text)
            return MagicMock()

        bot.send_message = picky
        await hb.board_notice_job(_ctx(bot))
        assert len(plain) == 1
        assert "Renew logo registration" in plain[0]

    async def test_a_rejected_button_still_counts_as_delivered(self, monkeypatch):
        store = FakeStore(items=[_item(url="https://example.test/renew")],
                          officers=(111,)).install(monkeypatch)
        bot = FakeBot()

        async def picky(chat_id, text, **kwargs):
            if kwargs.get("reply_markup") is not None:
                raise TelegramError("BUTTON_URL_INVALID")
            return MagicMock()

        bot.send_message = picky
        await hb.board_notice_job(_ctx(bot))
        assert store.items[0]["last_notified"] == date.today().isoformat()


# ---------------------------------------------------------------------------
# Markdown safety of the notice itself
# ---------------------------------------------------------------------------


class TestNoticeMarkdown:
    NASTY = "Renew 501(c)(3) *and* file Form_990"

    def test_the_action_is_escaped(self):
        text = hb.render_notice(_item(action=self.NASTY), date.today())
        assert r"Form\_990" in text
        assert text.count("*") % 2 == 0, text

    def test_the_description_is_escaped(self):
        text = hb.render_notice(_item(description="EIN 12_3456789 *draft*"),
                                date.today())
        assert r"12\_3456789" in text
        assert text.count("*") % 2 == 0, text

    def test_no_doubled_asterisks(self):
        today = date.today()
        for offset in (-5, 0, 3, 30):
            text = hb.render_notice(
                _item(due=(today + timedelta(days=offset)).isoformat()), today)
            assert "**" not in text, (offset, text)

    def test_it_says_how_to_act_on_it(self):
        """A notice with no next step is a notice that gets ignored."""
        assert "/boarditems" in hb.render_notice(_item(), date.today())


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------


class TestScheduling:
    def test_the_notice_runs_at_six_am_church_time(self):
        assert hb.BOARD_NOTICE_TIME.hour == 6
        assert hb.BOARD_NOTICE_TIME.minute == 0

    def test_it_carries_the_church_timezone(self):
        """A naive time would be interpreted in the process's zone, which on a
        UTC server is 6am UTC — 1am or 2am in Virginia."""
        import settings
        assert hb.BOARD_NOTICE_TIME.tzinfo is settings.TZ

    def test_it_does_not_collide_with_the_nightly_backup(self):
        """Both read and write files under data/."""
        from handlers.backup import BACKUP_TIME
        assert hb.BOARD_NOTICE_TIME.hour != BACKUP_TIME.hour

    async def test_the_job_is_registered_as_a_daily_job(self):
        """run_repeating anchored to process start would drift an hour earlier
        on every restart until the 6am notice arrived at 2am.

        NB the jobs are registered in post_init, not in main(). A fake builder
        that accepts post_init and discards it sees NO jobs at all and this
        test passes vacuously — which is what it did at first. The assertion
        that `daily` is non-empty is what caught that.
        """
        import warnings

        import bot as botmod

        daily, repeating = [], []
        captured: dict = {}

        class FakeApp:
            def __init__(self):
                self.job_queue = MagicMock()
                self.job_queue.run_daily = lambda cb, time=None, name=None, **k: \
                    daily.append((getattr(cb, "__name__", str(cb)), time, name))
                self.job_queue.run_repeating = lambda cb, **k: \
                    repeating.append(getattr(cb, "__name__", str(cb)))
                self.bot = MagicMock()
                self.bot.set_my_commands = AsyncMock()

            def add_handler(self, h, group=0):
                pass

            def add_error_handler(self, h):
                pass

            def run_polling(self, **kw):
                pass

        fake = FakeApp()

        class FakeBuilder:
            def token(self, t):
                return self

            def post_init(self, f):
                captured["post_init"] = f
                return self

            def build(self):
                return fake

        from unittest.mock import patch
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with patch.object(botmod.Application, "builder",
                              staticmethod(lambda: FakeBuilder())):
                botmod.main()

        assert "post_init" in captured, "main() no longer registers a post_init"
        await captured["post_init"](fake)

        assert daily, "no daily jobs registered at all — this test sees nothing"
        names = {n for n, _t, _name in daily}
        assert "board_notice_job" in names, sorted(names)
        assert "board_notice_job" not in repeating
        when = next(t for n, t, _ in daily if n == "board_notice_job")
        assert when == hb.BOARD_NOTICE_TIME


# ---------------------------------------------------------------------------
# Purge of soft-deleted items
# ---------------------------------------------------------------------------


class TestPurge:
    async def test_an_old_deletion_is_dropped(self, monkeypatch):
        old = (date.today() - timedelta(days=bi.DELETED_PURGE_AFTER_DAYS + 1)).isoformat()
        store = FakeStore(items=[_item(status="deleted", deleted_at=old)]).install(monkeypatch)
        assert await hb.purge_deleted_board_items() == 1
        assert store.items == []

    async def test_a_recent_deletion_is_kept(self, monkeypatch):
        recent = (date.today() - timedelta(days=1)).isoformat()
        store = FakeStore(items=[_item(status="deleted",
                                       deleted_at=recent)]).install(monkeypatch)
        assert await hb.purge_deleted_board_items() == 0
        assert len(store.items) == 1
        assert store.saves == 0, "nothing to purge should mean no write"

    async def test_pending_items_survive(self, monkeypatch):
        store = FakeStore(items=[_item()]).install(monkeypatch)
        await hb.purge_deleted_board_items()
        assert len(store.items) == 1

    async def test_the_daily_maintenance_job_calls_it(self):
        """A purge nothing calls is a purge that never runs."""
        import inspect

        import bot as botmod
        assert "purge_deleted_board_items" in inspect.getsource(
            botmod.daily_maintenance_job)
