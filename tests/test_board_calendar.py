"""/boardcalendar and the ICS it produces.

Three properties matter, and all three have bitten this project or its
neighbours before:

**All-day entries.** The user asked for these explicitly. An obligation has no
time of day, and inventing 9am is a fiction the officer then reads around.
In ICS terms that means DTSTART;VALUE=DATE, not a datetime.

**A stable UID, and a SEQUENCE that moves.** Exporting twice used to leave the
congregation with every service duplicated in their own calendars — that is
what _event_uid was written to fix. The board case adds the second half: an
item's dates CHANGE (edited, or rolled forward a cycle), and a client that
already holds the item ignores an incoming copy whose SEQUENCE has not
increased. Stable UID without a moving SEQUENCE means the officer re-imports
and keeps seeing last year's date.

**An alarm ladder, not a mirror.** Mirroring the bot exactly would be ~28
VALARMs per item, because a VALARM carries one trigger and ICS has no
"weekly then daily" rule.
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from icalendar import Calendar

sys.path.insert(0, str(Path(__file__).parent.parent))

import board_items as bi
import handlers.board as hb
import permissions
from ics_generator import board_items_to_ics

DUE = date(2027, 3, 14)


def _item(**over):
    base = {"id": "AAA11111", "action": "Renew logo registration",
            "due": DUE.isoformat(), "notify_days": 60, "status": "pending",
            "description": "", "url": "", "history": []}
    base.update(over)
    return base


def _parse(raw: bytes) -> list:
    return list(Calendar.from_ical(raw).walk("VEVENT"))


def _one(items=None):
    return _parse(board_items_to_ics(items or [_item()]))[0]


def _alarms(vevent) -> list:
    return list(vevent.walk("VALARM"))


def _triggers(vevent) -> list[timedelta]:
    return [a.get("trigger").dt for a in _alarms(vevent)]


# ---------------------------------------------------------------------------
# All-day semantics
# ---------------------------------------------------------------------------


class TestAllDay:
    def test_dtstart_is_a_date_not_a_datetime(self):
        """The user asked for all-day entries with no time of day."""
        dtstart = _one().get("dtstart").dt
        assert isinstance(dtstart, date)
        assert not hasattr(dtstart, "hour"), f"got a datetime: {dtstart!r}"
        assert dtstart == DUE

    def test_the_serialised_form_declares_VALUE_DATE(self):
        """The thing a calendar client actually reads."""
        raw = board_items_to_ics([_item()]).decode()
        assert "DTSTART;VALUE=DATE:20270314" in raw, raw

    def test_dtend_is_the_following_day(self):
        """RFC 5545: an all-day event's end is EXCLUSIVE. Setting it equal to
        DTSTART gives a zero-length event some clients refuse to show."""
        vevent = _one()
        assert vevent.get("dtend").dt == DUE + timedelta(days=1)

    def test_no_timezone_is_attached(self):
        """A floating date is the point — the deadline is the 14th wherever the
        officer happens to be."""
        raw = board_items_to_ics([_item()]).decode()
        assert "DTSTART;TZID" not in raw


# ---------------------------------------------------------------------------
# UID and SEQUENCE
# ---------------------------------------------------------------------------


class TestIdentity:
    def test_the_uid_is_derived_from_the_item_id(self):
        assert str(_one().get("uid")) == "board-AAA11111@christofgodbot"

    def test_exporting_twice_gives_the_same_uid(self):
        """Otherwise the officer collects one dead entry per export."""
        first = str(_one().get("uid"))
        second = str(_one().get("uid"))
        assert first == second

    def test_two_items_get_different_uids(self):
        events = _parse(board_items_to_ics([_item(id="A1"), _item(id="B2")]))
        assert len({str(e.get("uid")) for e in events}) == 2

    def test_the_uid_survives_a_renewal_cycle(self):
        """One obligation, one calendar entry, the date moving — which is the
        whole reason completing an item rolls it forward instead of retiring
        it and starting a new one."""
        item = _item()
        before = str(_one([item]).get("uid"))
        bi.complete(item, date(2027, 2, 1), "Officer")
        bi.reopen(item, date(2030, 3, 14))
        assert str(_one([item]).get("uid")) == before

    def test_sequence_starts_at_zero(self):
        assert int(_one().get("sequence")) == 0

    def test_a_renewal_cycle_raises_the_sequence(self):
        """A client that already holds this item ignores an incoming copy whose
        SEQUENCE has not increased, so without this the officer re-imports and
        keeps seeing the previous cycle's date."""
        item = _item()
        bi.complete(item, date(2027, 2, 1), "Officer")
        bi.reopen(item, date(2030, 3, 14))
        assert int(_one([item]).get("sequence")) > 0

    def test_sequence_survives_a_hand_edited_file(self):
        for bad in ("three", None, -2, ""):
            assert bi.sequence_of({"sequence": bad}) == 0, bad

    def test_sequence_increments_rather_than_resetting(self):
        item = _item()
        for _ in range(3):
            bi.bump_sequence(item)
        assert bi.sequence_of(item) == 3


# ---------------------------------------------------------------------------
# Alarms
# ---------------------------------------------------------------------------


class TestAlarms:
    def test_the_default_ladder_is_present(self):
        assert sorted(-t.days for t in _triggers(_one())) == [1, 7, 14, 30, 60]

    def test_triggers_are_negative_offsets_from_the_due_date(self):
        """A positive trigger fires AFTER the deadline, which is useless."""
        assert all(t.days < 0 for t in _triggers(_one()))

    def test_the_serialised_trigger_is_a_day_duration(self):
        raw = board_items_to_ics([_item()]).decode()
        assert "TRIGGER:-P60D" in raw, raw

    def test_no_alarm_precedes_the_items_own_lead_time(self):
        """A 60-day alarm on an item the board asked to hear about at 30 would
        contradict the setting they chose."""
        offsets = [-t.days for t in _triggers(_one([_item(notify_days=30)]))]
        assert max(offsets) == 30

    def test_a_custom_lead_time_gets_its_own_alarm(self):
        offsets = [-t.days for t in _triggers(_one([_item(notify_days=45)]))]
        assert 45 in offsets

    def test_the_ladder_stays_small(self):
        """The alternative was mirroring the bot exactly: ~28 VALARMs per item,
        because a VALARM has one trigger and ICS has no "weekly then daily"
        rule. If this grows, it was a decision."""
        assert len(_alarms(_one())) <= 6

    def test_every_alarm_names_the_obligation(self):
        """An alarm reading only "Reminder" tells the officer nothing at 7am."""
        for alarm in _alarms(_one()):
            assert "Renew logo registration" in str(alarm.get("description"))

    def test_the_alarms_are_displayable(self):
        for alarm in _alarms(_one()):
            assert str(alarm.get("action")) == "DISPLAY"


# ---------------------------------------------------------------------------
# Content
# ---------------------------------------------------------------------------


class TestContent:
    def test_the_summary_is_the_action(self):
        assert str(_one().get("summary")) == "Renew logo registration"

    def test_an_item_with_no_action_still_gets_a_summary(self):
        assert str(_one([_item(action="")]).get("summary"))

    def test_the_description_and_url_are_carried(self):
        vevent = _one([_item(description="Serial 123",
                             url="https://example.test/renew")])
        assert "Serial 123" in str(vevent.get("description"))
        assert str(vevent.get("url")) == "https://example.test/renew"

    def test_an_absent_description_adds_no_empty_property(self):
        assert _one().get("description") is None

    def test_markdown_is_not_escaped_into_the_calendar(self):
        """md() is for Telegram. A calendar client shows the raw text, so an
        escaped underscore would appear as a literal backslash."""
        vevent = _one([_item(action="File Form_990")])
        assert str(vevent.get("summary")) == "File Form_990"

    def test_an_undated_item_is_skipped_not_defaulted(self):
        """Defaulting to today would plant a false deadline in the calendar."""
        events = _parse(board_items_to_ics([_item(due="whenever"),
                                            _item(id="OK")]))
        assert [str(e.get("uid")) for e in events] == ["board-OK@christofgodbot"]

    def test_an_empty_list_is_still_a_valid_calendar(self):
        cal = Calendar.from_ical(board_items_to_ics([]))
        assert list(cal.walk("VEVENT")) == []

    def test_the_calendar_is_named(self):
        raw = board_items_to_ics([_item()], calendar_name="Board Deadlines").decode()
        assert "Board Deadlines" in raw

    def test_the_output_parses_as_a_calendar_at_all(self):
        """Guards every test above: all of them walk a parsed calendar, and a
        malformed one would raise rather than assert."""
        cal = Calendar.from_ical(board_items_to_ics([_item()]))
        assert cal.get("version") == "2.0"
        assert cal.get("prodid")


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------


class FakeStore:
    def __init__(self, items=None, officer=True):
        self.items = items if items is not None else [_item()]
        user = {"chat_id": 111, "display_name": "Board Officer"}
        if officer:
            user["board_officer"] = True
        self.users = [user]

    def install(self, monkeypatch):
        async def get_board_items():
            return self.items

        async def get_all_users():
            return self.users

        for module in (hb, permissions):
            monkeypatch.setattr(module.storage, "get_board_items", get_board_items,
                                raising=False)
            monkeypatch.setattr(module.storage, "get_all_users", get_all_users)
        return self


def _ctx() -> MagicMock:
    ctx = MagicMock()
    ctx.bot = MagicMock()
    ctx.user_data = {}
    return ctx


def _update(chat_id: int = 111) -> MagicMock:
    upd = MagicMock()
    upd.effective_chat.id = chat_id
    upd.effective_user.id = chat_id
    upd.effective_user.username = "officer"
    upd.effective_user.full_name = "Board Officer"
    upd.message.reply_text = AsyncMock()
    upd.message.reply_document = AsyncMock()
    return upd


class TestBoardCalendarCommand:
    async def test_an_officer_receives_a_file(self, monkeypatch):
        FakeStore().install(monkeypatch)
        upd = _update()
        await hb.cmd_boardcalendar(upd, _ctx())
        upd.message.reply_document.assert_awaited_once()

    async def test_the_file_is_named_ics(self, monkeypatch):
        FakeStore().install(monkeypatch)
        upd = _update()
        await hb.cmd_boardcalendar(upd, _ctx())
        kwargs = upd.message.reply_document.await_args.kwargs
        assert kwargs["document"].filename.endswith(".ics")

    async def test_the_file_contains_the_pending_items(self, monkeypatch):
        FakeStore().install(monkeypatch)
        upd = _update()
        await hb.cmd_boardcalendar(upd, _ctx())
        payload = upd.message.reply_document.await_args.kwargs["document"].input_file_content
        raw = payload.decode() if isinstance(payload, bytes) else str(payload)
        assert "Renew logo registration" in raw
        assert "BEGIN:VEVENT" in raw

    async def test_a_non_officer_is_refused(self, monkeypatch):
        FakeStore(officer=False).install(monkeypatch)
        upd = _update()
        await hb.cmd_boardcalendar(upd, _ctx())
        upd.message.reply_document.assert_not_awaited()
        assert "Unknown command" in upd.message.reply_text.await_args.args[0]

    async def test_completed_items_are_not_exported(self, monkeypatch):
        """A completed item has no deadline left to warn about."""
        FakeStore(items=[_item(id="DONE", status="completed")]).install(monkeypatch)
        upd = _update()
        await hb.cmd_boardcalendar(upd, _ctx())
        upd.message.reply_document.assert_not_awaited()
        assert "Nothing pending" in upd.message.reply_text.await_args.args[0]

    async def test_nothing_pending_says_so_rather_than_sending_an_empty_file(
            self, monkeypatch):
        FakeStore(items=[]).install(monkeypatch)
        upd = _update()
        await hb.cmd_boardcalendar(upd, _ctx())
        upd.message.reply_document.assert_not_awaited()
        assert "/addboarditem" in upd.message.reply_text.await_args.args[0]

    async def test_an_undated_item_alone_counts_as_nothing_to_export(self,
                                                                     monkeypatch):
        """It is pending, but there is no date to put on a calendar — so an
        empty .ics would be sent without this check."""
        FakeStore(items=[_item(due="whenever")]).install(monkeypatch)
        upd = _update()
        await hb.cmd_boardcalendar(upd, _ctx())
        upd.message.reply_document.assert_not_awaited()

    async def test_the_caption_counts_the_deadlines(self, monkeypatch):
        FakeStore(items=[_item(id="A"), _item(id="B")]).install(monkeypatch)
        upd = _update()
        await hb.cmd_boardcalendar(upd, _ctx())
        caption = upd.message.reply_document.await_args.kwargs["caption"]
        assert "2 pending board deadlines" in caption

    async def test_the_caption_is_singular_for_one(self, monkeypatch):
        FakeStore().install(monkeypatch)
        upd = _update()
        await hb.cmd_boardcalendar(upd, _ctx())
        caption = upd.message.reply_document.await_args.kwargs["caption"]
        assert "1 pending board deadline," in caption


class TestHelpAndWiring:
    def test_the_officer_help_names_the_command(self):
        import handlers.user_basics as ub
        assert "/boardcalendar" in ub.BOARD_COMMANDS_TEXT

    def test_the_help_block_is_still_officer_only(self):
        import handlers.user_basics as ub
        text = ub._commands_text("en", is_adm=True, is_prayer=True, is_board=False)
        assert "/boardcalendar" not in text

    def test_the_command_is_registered(self):
        from tests.test_board_wiring import _handlers, _index_of_command
        _index_of_command(_handlers(), "boardcalendar")

    def test_it_is_registered_before_the_catch_alls(self):
        from tests.test_board_wiring import (
            _catch_all_indices,
            _handlers,
            _index_of_command,
        )
        handlers = _handlers()
        assert _index_of_command(handlers, "boardcalendar") < min(
            _catch_all_indices(handlers))
