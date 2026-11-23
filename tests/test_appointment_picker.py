"""The /appointment day and time picker, and the rules behind it.

Agreed with the user 2026-11-20: office hours Monday–Thursday 09:00–17:00 and
Friday 09:00–13:00 in the church timezone, 30-minute slots, weekday feast days
closed, shown to each member in their own timezone. The Sabbath reference is a
fixed 6:00 PM, so there is no sunset logic to test — and there must not be.

Two halves: appointment_slots' pure rules, then the handler that draws them
as buttons and re-checks every tap. Every handler test pins "now" to a fixed
Monday, so nothing here drifts with the calendar.
"""

from __future__ import annotations

import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytz

sys.path.insert(0, str(Path(__file__).parent.parent))

import appointment_slots as slots
import handlers.appointments as ha

NY = pytz.timezone("America/New_York")
TOKYO = pytz.timezone("Asia/Tokyo")


def at(y, mo, d, h=0, mi=0, tz=NY) -> datetime:
    return tz.localize(datetime(y, mo, d, h, mi))


# A Monday. Week of 2027-02-01: Mon 1 … Fri 5, Sat 6, Sun 7.
NOW = at(2027, 2, 1, 8, 0)
LATEST = at(2027, 8, 1, 8, 0)


def rules(**over) -> dict:
    base = {"church_tz": NY, "hours": slots.DEFAULT_HOURS, "closed": set(),
            "busy": [], "now": NOW, "latest": LATEST}
    base.update(over)
    return base


def hhmm(slot_list, tz=NY) -> list[str]:
    return [s.astimezone(tz).strftime("%H:%M") for s in slot_list]


# ---------------------------------------------------------------------------
# Office hours
# ---------------------------------------------------------------------------

class TestOfficeHours:
    def test_monday_runs_nine_to_five_in_half_hours(self):
        got = hhmm(slots.day_slots(date(2027, 2, 1), slots.DEFAULT_HOURS, NY))
        assert got[0] == "09:00" and got[-1] == "16:30"
        assert len(got) == 16

    @pytest.mark.parametrize("day", [1, 2, 3, 4])  # Mon–Thu
    def test_monday_to_thursday_are_the_same(self, day):
        got = hhmm(slots.day_slots(date(2027, 2, day), slots.DEFAULT_HOURS, NY))
        assert (got[0], got[-1], len(got)) == ("09:00", "16:30", 16)

    def test_friday_ends_at_one(self):
        got = hhmm(slots.day_slots(date(2027, 2, 5), slots.DEFAULT_HOURS, NY))
        assert got == ["09:00", "09:30", "10:00", "10:30",
                       "11:00", "11:30", "12:00", "12:30"]

    @pytest.mark.parametrize("day", [6, 7])
    def test_the_weekend_is_closed(self, day):
        assert slots.day_slots(date(2027, 2, day), slots.DEFAULT_HOURS, NY) == []

    def test_a_slot_must_finish_by_closing_time(self):
        """17:00 would end at 17:30 — after closing — so it is not offered."""
        got = hhmm(slots.day_slots(date(2027, 2, 1), {0: (9 * 60, 17 * 60 + 15)}, NY))
        assert got[-1] == "16:30"

    def test_slots_keep_wall_clock_time_across_a_dst_change(self):
        """2027-03-15 is after the spring change: still 09:00, now at UTC-4."""
        first = slots.day_slots(date(2027, 3, 15), slots.DEFAULT_HOURS, NY)[0]
        assert first.strftime("%H:%M") == "09:00"
        assert first.utcoffset() == timedelta(hours=-4)

    def test_no_sunset_logic_anywhere(self):
        """The church uses a fixed 6:00 PM; sunset must never creep in."""
        source = Path(slots.__file__).read_text().lower()
        code = "\n".join(line for line in source.splitlines()
                         if not line.strip().startswith("#"))
        assert "astral" not in code and "suntime" not in code
        assert "def sunset" not in code


class TestConfiguredHours:
    def test_parse_reads_ranges(self):
        got = slots.parse_hours({"Monday": "10:00-12:00", "friday": "9:00 - 13:00"})
        assert got == {0: (600, 720), 4: (540, 780)}

    def test_a_day_left_out_is_closed(self):
        got = slots.parse_hours({"tuesday": "09:00-17:00"})
        assert set(got) == {1}

    def test_midnight_closing_is_allowed(self):
        assert slots.parse_hours({"monday": "20:00-24:00"}) == {0: (1200, 1440)}

    @pytest.mark.parametrize("raw", [
        {"funday": "09:00-17:00"},
        {"monday": "9 to 5"},
        {"monday": "09:00-09:15"},      # shorter than a slot
        {"monday": "17:00-09:00"},      # backwards
        {"monday": "25:00-26:00"},
        {"monday": "09:00-24:30"},
        [],
        {},
        "09:00-17:00",
    ])
    def test_parse_rejects_what_it_cannot_read(self, raw):
        with pytest.raises(ValueError):
            slots.parse_hours(raw)

    def test_absent_means_the_defaults(self):
        assert slots.configured_hours(None) == slots.DEFAULT_HOURS

    def test_a_bad_value_falls_back_and_says_so(self, caplog):
        with caplog.at_level(logging.ERROR, logger="appointment_slots"):
            got = slots.configured_hours({"monday": "nonsense"})
        assert got == slots.DEFAULT_HOURS
        assert "default office hours" in caplog.text

    def test_the_fallback_is_a_copy(self):
        got = slots.configured_hours(None)
        got[5] = (0, 60)
        assert 5 not in slots.DEFAULT_HOURS


# ---------------------------------------------------------------------------
# Which slots are free
# ---------------------------------------------------------------------------

class TestFreeSlots:
    def test_a_whole_day_is_free_by_default(self):
        got = slots.free_slots_for_viewer_day(date(2027, 2, 2), NY, **rules())
        assert len(got) == 16

    def test_past_slots_are_not_offered(self):
        now = at(2027, 2, 1, 10, 15)
        got = hhmm(slots.free_slots_for_viewer_day(date(2027, 2, 1), NY, **rules(now=now)))
        assert got[0] == "10:30"

    def test_a_slot_starting_now_is_not_offered(self):
        now = at(2027, 2, 1, 10, 0)
        got = hhmm(slots.free_slots_for_viewer_day(date(2027, 2, 1), NY, **rules(now=now)))
        assert "10:00" not in got and got[0] == "10:30"

    def test_nothing_beyond_the_horizon(self):
        latest = at(2027, 2, 3, 11, 0)
        got = hhmm(slots.free_slots_for_viewer_day(date(2027, 2, 3), NY,
                                                   **rules(latest=latest)))
        assert got[-1] == "11:00"
        assert slots.free_slots_for_viewer_day(date(2027, 2, 4), NY,
                                               **rules(latest=latest)) == []

    def test_a_feast_day_is_closed(self):
        got = slots.free_slots_for_viewer_day(
            date(2027, 2, 3), NY, **rules(closed={date(2027, 2, 3)}))
        assert got == []

    def test_a_feast_day_closes_only_itself(self):
        got = slots.free_slots_for_viewer_day(
            date(2027, 2, 4), NY, **rules(closed={date(2027, 2, 3)}))
        assert len(got) == 16

    def test_a_busy_slot_is_hidden(self):
        busy = [(at(2027, 2, 2, 10, 0), at(2027, 2, 2, 10, 30))]
        got = hhmm(slots.free_slots_for_viewer_day(date(2027, 2, 2), NY, **rules(busy=busy)))
        assert "10:00" not in got
        assert "09:30" in got and "10:30" in got  # back-to-back is fine

    def test_a_long_busy_interval_hides_every_slot_it_touches(self):
        busy = [(at(2027, 2, 2, 10, 15), at(2027, 2, 2, 11, 15))]
        got = hhmm(slots.free_slots_for_viewer_day(date(2027, 2, 2), NY, **rules(busy=busy)))
        for gone in ("10:00", "10:30", "11:00"):
            assert gone not in got
        assert "09:30" in got and "11:30" in got

    def test_bookable_days_of_a_month(self):
        got = slots.bookable_viewer_days(2027, 2, NY, **rules(closed={date(2027, 2, 10)}))
        assert date(2027, 2, 1) in got          # today, before opening
        assert date(2027, 2, 5) in got          # a Friday
        assert date(2027, 2, 6) not in got      # Saturday
        assert date(2027, 2, 7) not in got      # Sunday
        assert date(2027, 2, 10) not in got     # the feast
        assert all(d.month == 2 for d in got)

    def test_a_past_day_is_not_bookable(self):
        got = slots.bookable_viewer_days(2027, 2, NY, **rules(now=at(2027, 2, 3, 18, 0)))
        assert date(2027, 2, 2) not in got and date(2027, 2, 3) not in got
        assert date(2027, 2, 4) in got


class TestTheViewersTimezone:
    """Church hours, the viewer's dates. Tokyo is 14 hours ahead of New York in
    winter, so church Monday 09:00 and 09:30 are Monday 23:00 and 23:30 there,
    and church Monday 10:00 is already Tuesday."""

    def test_a_church_day_straddles_two_viewer_dates(self):
        monday = slots.free_slots_for_viewer_day(date(2027, 2, 1), TOKYO, **rules())
        assert hhmm(monday, TOKYO) == ["23:00", "23:30"]

    def test_the_rest_lands_on_the_next_viewer_date(self):
        tuesday = slots.free_slots_for_viewer_day(date(2027, 2, 2), TOKYO, **rules())
        tokyo = hhmm(tuesday, TOKYO)
        # Church Monday 10:00–16:30 (14 slots), then church Tuesday 09:00, 09:30.
        assert tokyo[:2] == ["00:00", "00:30"]
        assert tokyo[-2:] == ["23:00", "23:30"]
        assert len(tuesday) == 14 + 2

    def test_slots_are_still_church_hours(self):
        tuesday = slots.free_slots_for_viewer_day(date(2027, 2, 2), TOKYO, **rules())
        assert all(9 <= s.astimezone(NY).hour < 17 for s in tuesday)

    def test_the_church_friday_cut_holds_for_the_viewer(self):
        """Church Friday ends 12:30 = Saturday 02:30 Tokyo; nothing after."""
        saturday = slots.free_slots_for_viewer_day(date(2027, 2, 6), TOKYO, **rules())
        assert hhmm(saturday, TOKYO)[-1] == "02:30"
        assert all(s.astimezone(NY).weekday() == 4 for s in saturday)


class TestFeastDates:
    def test_eve_services_do_not_close_their_day(self):
        import hebrew_calendar
        events = hebrew_calendar.upcoming_convocation_events(NY)
        feasts = slots.feast_dates(NY)
        days = {e["service_time"].date() for e in events if e["label"] != "Eve"}
        eves = {e["service_time"].date() for e in events if e["label"] == "Eve"}
        assert feasts == days
        assert eves - days, "expected at least one eve on an ordinary day"
        assert not (eves - days) & feasts

    def test_a_calendar_failure_closes_nothing(self, caplog):
        with patch("hebrew_calendar.upcoming_convocation_events",
                   side_effect=RuntimeError("boom")):
            assert slots.feast_dates(NY) == set()
        assert "feast days" in caplog.text


# ---------------------------------------------------------------------------
# Who is busy
# ---------------------------------------------------------------------------

def appt(start, *, user=111, official="off1", status="confirmed", minutes=30, aid="A"):
    iso = start.isoformat()
    return {"id": aid, "user_chat_id": user, "official_id": official,
            "official_name": "Pastor Test", "requested_datetime": iso,
            "confirmed_datetime": iso if status == "confirmed" else None,
            "status": status, "duration_minutes": minutes}


class TestBusyIntervals:
    def test_the_officials_confirmed_appointment_blocks(self):
        a = appt(at(2027, 2, 2, 10), user=222)
        assert ha._busy_intervals([a], "off1", 111) == [
            (at(2027, 2, 2, 10), at(2027, 2, 2, 10, 30))]

    def test_someone_elses_pending_request_does_not(self):
        a = appt(at(2027, 2, 2, 10), user=222, status="pending")
        assert ha._busy_intervals([a], "off1", 111) == []

    def test_the_requesters_own_pending_request_does(self):
        a = appt(at(2027, 2, 2, 10), official="off2", status="pending")
        assert len(ha._busy_intervals([a], "off1", 111)) == 1

    def test_another_officials_appointment_with_someone_else_does_not(self):
        a = appt(at(2027, 2, 2, 10), user=222, official="off2")
        assert ha._busy_intervals([a], "off1", 111) == []

    @pytest.mark.parametrize("status", ["cancelled", "declined"])
    def test_finished_records_do_not(self, status):
        a = appt(at(2027, 2, 2, 10), status=status)
        assert ha._busy_intervals([a], "off1", 111) == []

    def test_duration_is_respected(self):
        a = appt(at(2027, 2, 2, 10), user=222, minutes=90)
        assert ha._busy_intervals([a], "off1", 111)[0][1] == at(2027, 2, 2, 11, 30)


# ---------------------------------------------------------------------------
# The keyboards
# ---------------------------------------------------------------------------

def _all_keys(markup):
    return [k for row in markup.inline_keyboard for k in row]


class TestCalendarKeyboard:
    def kb(self, lang="en", bookable=None, month=(2027, 2),
           first=(2027, 2), last=(2027, 7)):
        if bookable is None:
            bookable = {date(2027, 2, 2), date(2027, 2, 5)}
        return ha._calendar_keyboard(*month, bookable, lang, first, last)

    def test_title_and_weekday_header(self):
        rows = self.kb().inline_keyboard
        assert rows[0][0].text == "February 2027"
        assert [k.text for k in rows[1]] == ["Su", "Mo", "Tu", "We", "Th", "Fr", "Sa"]

    def test_english_weeks_start_on_sunday_as_in_the_us(self):
        """CLDR's plain "en" starts on Monday; the code asks for en_US."""
        assert self.kb("en").inline_keyboard[1][0].text == "Su"

    def test_spanish_weeks_start_on_monday(self):
        rows = self.kb("es").inline_keyboard
        assert rows[0][0].text.startswith("F")  # "Febrero 2027", capitalised
        assert rows[1][0].text.upper().startswith("LU")

    def test_only_bookable_days_are_buttons(self):
        days = {k.text: k.callback_data for k in _all_keys(self.kb())
                if k.text.isdigit()}
        assert days == {"2": "apcal:d:2027-02-02", "5": "apcal:d:2027-02-05"}

    def test_every_other_key_is_inert(self):
        for k in _all_keys(self.kb()):
            if not k.text.isdigit() and k.text not in ("▶️", "◀️") \
                    and "Cancel" not in k.text:
                assert k.callback_data == "apcal:n"

    def test_seven_columns_in_every_week(self):
        rows = self.kb().inline_keyboard
        weeks = rows[2:-2]  # title, header … nav, cancel
        assert weeks and all(len(r) == 7 for r in weeks)

    def test_navigation_stops_at_the_bounds(self):
        keys = {k.text: k.callback_data for k in _all_keys(self.kb())}
        assert keys.get("▶️") == "apcal:m:2027-03" and "◀️" not in keys
        keys = {k.text: k.callback_data for k in _all_keys(self.kb(month=(2027, 7)))}
        assert keys.get("◀️") == "apcal:m:2027-06" and "▶️" not in keys

    def test_navigation_wraps_the_year(self):
        keys = {k.text: k.callback_data for k in _all_keys(
            self.kb(month=(2026, 12), first=(2026, 11), last=(2027, 5)))}
        assert keys["▶️"] == "apcal:m:2027-01"
        assert keys["◀️"] == "apcal:m:2026-11"

    @pytest.mark.parametrize("lang", ["en", "es", "fr", "zu"])
    def test_callback_data_fits_telegrams_limit(self, lang):
        for k in _all_keys(self.kb(lang)):
            assert len(k.callback_data.encode()) <= 64
            assert k.text  # Telegram rejects an empty label


class TestTimeKeyboard:
    def test_labels_are_in_the_viewers_time_and_data_is_absolute(self):
        free = slots.free_slots_for_viewer_day(date(2027, 2, 2), NY, **rules())[:4]
        kb = ha._time_keyboard(free, TOKYO, "en")
        first = kb.inline_keyboard[0][0]
        assert first.text == "11:00 PM"          # 09:00 New York
        assert first.callback_data == f"apcal:t:{int(free[0].timestamp())}"

    def test_three_to_a_row_then_back_and_cancel(self):
        free = slots.free_slots_for_viewer_day(date(2027, 2, 5), NY, **rules())  # 8
        rows = ha._time_keyboard(free, NY, "en").inline_keyboard
        assert [len(r) for r in rows] == [3, 3, 2, 2]
        assert rows[-1][0].callback_data == "apcal:m:2027-02"
        assert rows[-1][1].callback_data == "apcal:x"

    def test_twenty_four_hour_time_for_french(self):
        free = slots.free_slots_for_viewer_day(date(2027, 2, 2), NY, **rules())
        label = ha._time_keyboard(free, NY, "fr").inline_keyboard[0][0].text
        assert label == "09:00"


# ---------------------------------------------------------------------------
# The handler
# ---------------------------------------------------------------------------

OFF = {"id": "off1", "name": "Pastor Test", "chat_id": 999}


def _query(data: str, uid: int = 111):
    q = MagicMock()
    q.data = data
    q.from_user.id = uid
    q.answer = AsyncMock()
    q.edit_message_text = AsyncMock()
    q.message.reply_text = AsyncMock()
    upd = MagicMock()
    upd.callback_query = q
    upd.effective_user.id = uid
    return upd, q


def _ctx(official=OFF):
    ctx = MagicMock()
    ctx.user_data = {"ap_official": official} if official else {}
    return ctx


async def _pick(data, *, appts=(), viewer=NY, lang="en", closed=frozenset(),
                official=OFF, ctx=None):
    upd, q = _query(data)
    ctx = ctx or _ctx(official)

    async def fake_get():
        return list(appts)

    with patch("handlers.appointments.now_tz", return_value=NOW), \
         patch("handlers.appointments.get_user_prefs",
               AsyncMock(return_value=(viewer, lang))), \
         patch("handlers.appointments.slots.closed_dates", return_value=set(closed)), \
         patch("storage.get_appointments", side_effect=fake_get):
        result = await ha.ap_pick(upd, ctx)
    return result, q, ctx


def _last_text(q) -> str:
    return q.edit_message_text.call_args[0][0]


def _last_markup(q):
    return q.edit_message_text.call_args.kwargs.get("reply_markup")


def _ts(dt: datetime) -> str:
    return f"apcal:t:{int(dt.timestamp())}"


class TestPickingADay:
    async def test_a_day_shows_its_times(self):
        result, q, _ = await _pick("apcal:d:2027-02-05")
        assert result == ha.AP_TIME
        labels = [k.text for k in _all_keys(_last_markup(q)) if ":" in k.text]
        assert labels[0] == "9:00 AM" and labels[-1] == "12:30 PM"
        assert "Friday, February 5, 2027" in _last_text(q)

    async def test_a_day_with_nothing_free_returns_to_the_month(self):
        result, q, _ = await _pick("apcal:d:2027-02-06")  # Saturday
        assert result == ha.AP_DATE
        assert "no longer available" in _last_text(q)

    async def test_a_feast_day_is_not_offered_even_if_tapped(self):
        result, _, _ = await _pick("apcal:d:2027-02-03", closed={date(2027, 2, 3)})
        assert result == ha.AP_DATE

    async def test_month_navigation(self):
        result, q, _ = await _pick("apcal:m:2027-03")
        assert result == ha.AP_DATE
        assert _last_markup(q).inline_keyboard[0][0].text == "March 2027"

    async def test_navigation_is_clamped_to_the_horizon(self):
        _, q, _ = await _pick("apcal:m:2030-01")
        assert _last_markup(q).inline_keyboard[0][0].text == "August 2027"
        _, q, _ = await _pick("apcal:m:2020-01")
        assert _last_markup(q).inline_keyboard[0][0].text == "February 2027"

    async def test_an_inert_key_changes_nothing(self):
        result, q, _ = await _pick("apcal:n")
        assert result is None
        q.edit_message_text.assert_not_called()

    async def test_cancel_ends_the_request(self):
        result, q, _ = await _pick("apcal:x")
        assert result == ha.ConversationHandler.END
        assert "cancelled" in _last_text(q).lower()

    async def test_a_garbled_tap_redraws_the_calendar(self):
        result, q, _ = await _pick("apcal:zz:what")
        assert result == ha.AP_DATE
        assert _last_markup(q) is not None


class TestPickingATime:
    async def test_a_free_slot_moves_on_to_the_purpose(self):
        result, q, ctx = await _pick(_ts(at(2027, 2, 2, 14, 30)))
        assert result == ha.AP_DESC
        assert ctx.user_data["ap_date"] == "2027-02-02"
        assert ctx.user_data["ap_time"] == "14:30"
        assert "Tuesday, February 2, 2027" in _last_text(q)
        assert "description" in _last_text(q).lower()

    async def test_the_stored_time_is_the_churchs_not_the_viewers(self):
        """The fix the picker brings: a Tokyo member's tap stores New York time."""
        _, _, ctx = await _pick(_ts(at(2027, 2, 2, 9, 0)), viewer=TOKYO)
        assert (ctx.user_data["ap_date"], ctx.user_data["ap_time"]) == ("2027-02-02", "09:00")

    async def test_the_desc_step_rebuilds_the_same_instant(self):
        """ap_desc and ap_confirm localize ap_date/ap_time in the church tz."""
        _, _, ctx = await _pick(_ts(at(2027, 2, 2, 9, 0)), viewer=TOKYO)
        d = [int(x) for x in ctx.user_data["ap_date"].split("-")]
        tm = [int(x) for x in ctx.user_data["ap_time"].split(":")]
        rebuilt = ha.TZ.localize(datetime(*d, *tm))
        assert rebuilt == at(2027, 2, 2, 9, 0)

    async def test_a_past_slot_is_refused(self):
        result, q, ctx = await _pick(_ts(at(2027, 2, 1, 7, 30)))
        assert result != ha.AP_DESC
        assert "ap_time" not in ctx.user_data

    async def test_a_slot_outside_office_hours_is_refused(self):
        result, _, ctx = await _pick(_ts(at(2027, 2, 5, 15, 0)))  # Friday 3 PM
        assert result != ha.AP_DESC and "ap_time" not in ctx.user_data

    async def test_a_slot_beyond_the_horizon_is_refused(self):
        result, _, ctx = await _pick(_ts(at(2027, 9, 6, 10, 0)))
        assert result != ha.AP_DESC and "ap_time" not in ctx.user_data

    async def test_a_slot_overlapping_the_requesters_own_appointment_is_refused(self):
        mine = appt(at(2027, 2, 2, 14, 15), official="off2", status="pending")
        result, q, ctx = await _pick(_ts(at(2027, 2, 2, 14, 30)), appts=[mine])
        assert result == ha.AP_TIME
        assert "ap_time" not in ctx.user_data
        q.message.reply_text.assert_awaited()
        assert "no longer available" in q.message.reply_text.call_args[0][0]

    async def test_a_slot_the_official_has_since_confirmed_is_refused(self):
        theirs = appt(at(2027, 2, 2, 14, 30), user=222)
        result, _, ctx = await _pick(_ts(at(2027, 2, 2, 14, 30)), appts=[theirs])
        assert result != ha.AP_DESC and "ap_time" not in ctx.user_data

    async def test_the_last_slot_of_a_day_gone_returns_to_the_month(self):
        """Friday with only 12:30 left, then 12:30 taken: back to the calendar."""
        busy = appt(at(2027, 2, 5, 9, 0), user=222, minutes=210, aid="B")
        theirs = appt(at(2027, 2, 5, 12, 30), user=333, aid="C")
        result, q, _ = await _pick(_ts(at(2027, 2, 5, 12, 30)), appts=[busy, theirs])
        assert result == ha.AP_DATE
        assert "no longer available" in _last_text(q)

    async def test_a_lost_conversation_ends_cleanly(self):
        result, q, _ = await _pick(_ts(at(2027, 2, 2, 14, 30)), official=None)
        assert result == ha.ConversationHandler.END


class TestStartingTheRequest:
    async def _official(self, *, hours=None, appts=()):
        q = MagicMock()
        q.data = "apsel:0"
        q.from_user.id = 111
        q.answer = AsyncMock()
        q.edit_message_text = AsyncMock()
        upd = MagicMock()
        upd.callback_query = q
        ctx = MagicMock()
        ctx.user_data = {}

        async def fake_get():
            return list(appts)

        with patch("handlers.appointments.now_tz", return_value=NOW), \
             patch("handlers.appointments.get_user_prefs",
                   AsyncMock(return_value=(NY, "en"))), \
             patch("handlers.appointments.slots.closed_dates", return_value=set()), \
             patch("handlers.appointments.OFFICE_HOURS",
                   slots.DEFAULT_HOURS if hours is None else hours), \
             patch("permissions.OFFICIALS", [OFF]), \
             patch("storage.get_appointments", side_effect=fake_get):
            result = await ha.ap_official(upd, ctx)
        return result, q, ctx

    async def test_choosing_an_official_opens_the_calendar(self):
        result, q, ctx = await self._official()
        assert result == ha.AP_DATE
        assert ctx.user_data["ap_official"] == OFF
        text = q.edit_message_text.call_args[0][0]
        assert "Pastor Test" in text and "EST" in text
        markup = q.edit_message_text.call_args.kwargs["reply_markup"]
        assert markup.inline_keyboard[0][0].text == "February 2027"

    async def test_no_hours_at_all_says_so_instead_of_an_empty_calendar(self):
        result, q, _ = await self._official(hours={})
        assert result == ha.ConversationHandler.END
        assert "no free times" in q.edit_message_text.call_args[0][0]

    async def test_a_full_month_skips_to_the_next(self):
        with patch("handlers.appointments.slots.bookable_viewer_days",
                   side_effect=lambda y, m, *a, **k: set() if m == 2 else {date(y, m, 1)}):
            result, q, _ = await self._official()
        assert result == ha.AP_DATE
        markup = q.edit_message_text.call_args.kwargs["reply_markup"]
        assert markup.inline_keyboard[0][0].text == "March 2027"


class TestTypingAndStaleTaps:
    async def test_typing_while_the_picker_waits_points_at_the_buttons(self):
        upd = MagicMock()
        upd.effective_user.id = 111
        upd.message.reply_text = AsyncMock()
        with patch("handlers.appointments.get_user_prefs",
                   AsyncMock(return_value=(NY, "en"))):
            assert await ha.ap_typed_instead(upd, MagicMock()) is None
        assert "buttons" in upd.message.reply_text.call_args[0][0]

    async def test_a_stale_picker_tap_says_how_to_start_again(self):
        upd, q = _query("apcal:d:2027-02-02")
        with patch("handlers.appointments.get_user_prefs",
                   AsyncMock(return_value=(NY, "en"))):
            result = await ha.ap_stale_button(upd, MagicMock())
        assert result == ha.ConversationHandler.END
        assert "/appointment" in _last_text(q)

    def test_the_stale_handler_is_registered_after_the_conversation(self):
        from tests.test_board_wiring import _handlers
        names = [getattr(h, "callback", None) for _, h in _handlers()]
        convs = [i for i, (_, h) in enumerate(_handlers())
                 if getattr(h, "entry_points", None)
                 and any(getattr(e, "callback", None) is ha.cmd_appointment
                         for e in h.entry_points)]
        stale = names.index(ha.ap_stale_button)
        assert convs and stale > convs[0]


class TestLocaleKeys:
    KEYS = ("appt_pick_day", "appt_pick_time", "appt_no_days", "appt_slot_gone",
            "appt_tap_hint", "appt_chosen", "appt_btn_calendar", "appt_btn_cancel",
            "appt_stale_buttons", "appt_picker_closed")

    @pytest.mark.parametrize("lang", ["en", "es", "fr", "zu"])
    def test_every_language_has_the_picker_strings(self, lang):
        from localization import CATALOG
        for key in self.KEYS:
            assert key in CATALOG[lang], f"{lang} lacks {key}"

    @pytest.mark.parametrize("lang", ["en", "es", "fr", "zu"])
    def test_no_picker_string_asks_for_a_typed_format(self, lang):
        from localization import CATALOG
        for key in self.KEYS:
            assert "YYYY" not in CATALOG[lang][key] and "HH:MM" not in CATALOG[lang][key]

    def test_the_typed_prompts_are_gone(self):
        from localization import CATALOG
        for gone in ("appt_ask_date", "appt_bad_date", "appt_ask_time",
                     "appt_bad_time", "appt_bad_datetime"):
            assert gone not in CATALOG["en"]


# ---------------------------------------------------------------------------
# US federal holidays (added 2026-11-23, Juneteenth included)
# ---------------------------------------------------------------------------

class TestUsFederalHolidays:
    """Checked against the OPM federal holiday calendars for 2026 and 2027."""

    def test_2026(self):
        assert sorted(slots.us_federal_holidays(2026)) == [
            date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16),
            date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3),
            date(2026, 9, 7), date(2026, 10, 12), date(2026, 11, 11),
            date(2026, 11, 26), date(2026, 12, 25)]

    def test_2027_and_its_weekend_observances(self):
        got = slots.us_federal_holidays(2027)
        assert sorted(got) == [
            date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15),
            date(2027, 5, 31), date(2027, 6, 18), date(2027, 7, 5),
            date(2027, 9, 6), date(2027, 10, 11), date(2027, 11, 11),
            date(2027, 11, 25), date(2027, 12, 24), date(2027, 12, 31)]
        assert got[date(2027, 6, 18)] == "Juneteenth"          # Sat → Fri
        assert got[date(2027, 7, 5)] == "Independence Day"     # Sun → Mon
        assert got[date(2027, 12, 31)] == "New Year's Day"     # 2028's, Sat → Fri

    def test_a_years_list_holds_only_its_own_dates(self):
        assert date(2027, 12, 31) not in slots.us_federal_holidays(2028)
        assert all(d.year == 2028 for d in slots.us_federal_holidays(2028))

    def test_every_observed_date_is_a_weekday(self):
        for year in range(2026, 2040):
            assert all(d.weekday() < 5 for d in slots.us_federal_holidays(year)), year

    def test_eleven_a_year_except_when_new_year_moves_back(self):
        for year in range(2026, 2040):
            n = len(slots.us_federal_holidays(year))
            jan1_next_is_saturday = date(year + 1, 1, 1).weekday() == 5
            jan1_is_saturday = date(year, 1, 1).weekday() == 5
            assert n == 11 + jan1_next_is_saturday - jan1_is_saturday, year

    def test_closed_dates_joins_holidays_and_feasts(self):
        with patch.object(slots, "feast_dates", return_value={date(2027, 3, 3)}):
            got = slots.closed_dates(NY, date(2026, 11, 23))
        assert date(2027, 3, 3) in got                 # the feast
        assert date(2026, 11, 26) in got               # Thanksgiving
        assert date(2027, 6, 18) in got                # next year's Juneteenth

    async def test_thanksgiving_cannot_be_booked(self):
        now = at(2026, 11, 23, 8)
        with patch("handlers.appointments.now_tz", return_value=now), \
             patch.object(slots, "feast_dates", return_value=set()):
            rules = ha._slot_rules([], "off1", 111)
        assert slots.free_slots_for_viewer_day(date(2026, 11, 26), NY, **rules) == []
        assert slots.free_slots_for_viewer_day(date(2026, 11, 27), NY, **rules)


# ---------------------------------------------------------------------------
# The official cannot be double-booked (added 2026-11-23)
# ---------------------------------------------------------------------------

class TestOfficialOverlap:
    def target(self, **over):
        a = appt(at(2027, 2, 2, 10), status="pending", aid="T")
        a.update(over)
        return a

    def test_another_members_confirmed_booking_is_found(self):
        other = appt(at(2027, 2, 2, 10, 15), user=222, aid="O")
        hit = ha._official_overlap([self.target(), other], self.target(),
                                   at(2027, 2, 2, 10).isoformat())
        assert hit["id"] == "O"

    def test_a_pending_request_is_not(self):
        other = appt(at(2027, 2, 2, 10), user=222, status="pending", aid="O")
        assert ha._official_overlap([other], self.target(),
                                    at(2027, 2, 2, 10).isoformat()) is None

    def test_the_appointment_itself_is_not(self):
        me = self.target(status="confirmed",
                         confirmed_datetime=at(2027, 2, 2, 10).isoformat())
        assert ha._official_overlap([me], me, at(2027, 2, 2, 10).isoformat()) is None

    def test_another_official_is_not(self):
        other = appt(at(2027, 2, 2, 10), user=222, official="off2", aid="O")
        assert ha._official_overlap([other], self.target(),
                                    at(2027, 2, 2, 10).isoformat()) is None

    def test_back_to_back_is_fine(self):
        other = appt(at(2027, 2, 2, 10, 30), user=222, aid="O")
        assert ha._official_overlap([other], self.target(),
                                    at(2027, 2, 2, 10).isoformat()) is None

    def test_the_proposed_length_is_the_appointments_own(self):
        other = appt(at(2027, 2, 2, 10, 45), user=222, aid="O")
        long = self.target(duration_minutes=60)
        assert ha._official_overlap([other], long,
                                    at(2027, 2, 2, 10).isoformat())["id"] == "O"

    async def test_accepting_a_reschedule_into_a_booked_slot_is_refused(self):
        when = at(2027, 2, 2, 10)
        me = appt(at(2027, 2, 3, 10), aid="T")
        me["reschedule_proposed_datetime"] = when.isoformat()
        me["reschedule_proposed_by"] = "official"
        other = appt(when, user=222, aid="O")
        q = MagicMock()
        q.edit_message_text = AsyncMock()
        q.from_user.id = 111
        q.from_user.username = "req"
        finalize = AsyncMock()
        with patch("handlers.appointments.now_tz", return_value=NOW), \
             patch.object(ha, "_finalize_appointment", finalize), \
             patch("storage.save_appointments", new=AsyncMock()):
            await ha._handle_reschedule_response(MagicMock(), q, me, [me, other],
                                                 "rs_accept")
        finalize.assert_not_called()
        assert me["confirmed_datetime"] == at(2027, 2, 3, 10).isoformat()
        assert "already has a confirmed appointment" in q.edit_message_text.call_args[0][0]
        assert "ID" not in q.edit_message_text.call_args[0][0]  # no other member's id


# ---------------------------------------------------------------------------
# The stateless picker for an existing appointment
# ---------------------------------------------------------------------------

def _existing(**over):
    a = appt(at(2027, 2, 3, 10), status="pending", aid="EX1")
    a.update({"user_username": "req", "user_display_name": "John Doe",
              "description": "chat"})
    a.update(over)
    return a


async def _tap_existing(data, *, actor=999, uname="pastor", appts=None, officials=None):
    upd, q = _query(data, uid=actor)
    q.from_user.username = uname
    ctx = MagicMock()
    ctx.bot.send_message = AsyncMock()
    store = list(appts if appts is not None else [_existing()])
    saved = []

    async def fake_get():
        return store

    async def fake_save(a):
        saved.append([dict(x) for x in a])

    with patch("handlers.appointments.now_tz", return_value=NOW), \
         patch("handlers.appointments.get_user_prefs",
               AsyncMock(return_value=(NY, "en"))), \
         patch("handlers.appointments.slots.closed_dates", return_value=set()), \
         patch("permissions.OFFICIALS", officials or [OFF]), \
         patch("storage.get_appointments", side_effect=fake_get), \
         patch("storage.save_appointments", side_effect=fake_save):
        await ha.appt_picker_callback(upd, ctx)
    return q, ctx, store, saved


class TestTheExistingAppointmentPicker:
    async def test_official_picks_a_time_and_the_requester_is_asked(self):
        q, ctx, store, saved = await _tap_existing(_ts(at(2027, 2, 4, 14)).replace(
            "apcal:", "appick:o:EX1:"))
        assert store[0]["status"] == "counter_proposed"
        assert store[0]["counter_datetime"] == at(2027, 2, 4, 14).isoformat()
        assert ctx.bot.send_message.await_args[0][0] == 111
        assert "Suggested time sent" in _last_text(q)

    async def test_requester_picks_a_time_and_the_negotiator_is_asked(self):
        ex = _existing(status="counter_proposed", negotiator_chat_id=999)
        q, ctx, store, _ = await _tap_existing(
            f"appick:u:EX1:t:{int(at(2027, 2, 4, 14).timestamp())}",
            actor=111, uname="req", appts=[ex])
        assert store[0]["user_counter_datetime"] == at(2027, 2, 4, 14).isoformat()
        assert ctx.bot.send_message.await_args[0][0] == 999
        assert "forwarded" in _last_text(q)

    async def test_someone_else_cannot_use_the_requesters_picker(self):
        q, ctx, store, saved = await _tap_existing(
            f"appick:u:EX1:t:{int(at(2027, 2, 4, 14).timestamp())}", actor=555)
        assert saved == [] and "not authorized" in _last_text(q)

    async def test_a_stranger_cannot_use_the_officials_picker(self):
        q, _, _, saved = await _tap_existing(
            f"appick:o:EX1:t:{int(at(2027, 2, 4, 14).timestamp())}", actor=555,
            uname="nobody")
        assert saved == [] and "not authorized" in _last_text(q)

    async def test_a_second_official_side_actor_is_turned_away(self):
        ex = _existing(negotiator_chat_id=888, negotiator_name="Jane Sec")
        q, _, _, saved = await _tap_existing(
            f"appick:o:EX1:t:{int(at(2027, 2, 4, 14).timestamp())}", appts=[ex])
        assert saved == [] and "already being handled" in _last_text(q)

    @pytest.mark.parametrize("status", ["confirmed", "declined", "cancelled"])
    async def test_a_settled_appointment_cannot_be_re_timed_here(self, status):
        q, _, _, saved = await _tap_existing(
            f"appick:o:EX1:t:{int(at(2027, 2, 4, 14).timestamp())}",
            appts=[_existing(status=status)])
        assert saved == [] and "already been" in _last_text(q)

    async def test_a_taken_slot_is_refused(self):
        other = appt(at(2027, 2, 4, 14), user=222, aid="O")
        q, ctx, store, saved = await _tap_existing(
            f"appick:o:EX1:t:{int(at(2027, 2, 4, 14).timestamp())}",
            appts=[_existing(), other])
        assert store[0]["status"] == "pending" and saved == []
        ctx.bot.send_message.assert_not_called()

    async def test_navigation_keeps_the_mode_and_appointment(self):
        q, _, _, saved = await _tap_existing("appick:o:EX1:m:2027-03")
        data = [k.callback_data for k in _all_keys(_last_markup(q))]
        assert any(d.startswith("appick:o:EX1:d:2027-03-") for d in data)
        assert "appick:o:EX1:x" in data and saved == []

    async def test_close_changes_nothing(self):
        q, _, store, saved = await _tap_existing("appick:o:EX1:x")
        assert saved == [] and store[0]["status"] == "pending"
        assert "nothing has changed" in _last_text(q)

    async def test_an_inert_key_does_nothing(self):
        q, _, _, saved = await _tap_existing("appick:o:EX1:n")
        q.edit_message_text.assert_not_called()

    async def test_a_missing_appointment(self):
        q, _, _, _ = await _tap_existing("appick:o:GONE:d:2027-02-04")
        assert "not found" in _last_text(q)

    async def test_malformed_data(self):
        q, _, _, _ = await _tap_existing("appick:o")
        assert "no longer works" in _last_text(q)

    async def test_an_unknown_mode_is_refused(self):
        q, _, _, saved = await _tap_existing(
            f"appick:z:EX1:t:{int(at(2027, 2, 4, 14).timestamp())}")
        assert saved == [] and "no longer works" in _last_text(q)

    def test_callback_data_fits_with_a_real_id(self):
        prefix = ha._pick_prefix(ha.PICK_RESCHEDULE, "ABCDEF1234")
        free = slots.free_slots_for_viewer_day(date(2027, 2, 2), NY, **rules())
        for k in _all_keys(ha._time_keyboard(free, NY, "en", prefix)):
            assert len(k.callback_data.encode()) <= 64
        for k in _all_keys(ha._calendar_keyboard(2027, 2, {date(2027, 2, 2)}, "zu",
                                                 (2027, 2), (2027, 7), prefix)):
            assert len(k.callback_data.encode()) <= 64

    def test_the_picker_is_registered(self):
        from tests.test_board_wiring import _handlers
        assert any(getattr(h, "callback", None) is ha.appt_picker_callback
                   for _, h in _handlers())

    async def test_an_hour_long_meeting_can_move_by_half_an_hour(self):
        """Its own current time must not count as busy, or every slot it
        overlaps — here 10:30 — would be refused."""
        ex = _existing(status="confirmed", duration_minutes=60,
                       confirmed_datetime=at(2027, 2, 3, 10).isoformat())
        q, ctx, store, saved = await _tap_existing(
            f"appick:r:EX1:t:{int(at(2027, 2, 3, 10, 30).timestamp())}",
            actor=111, uname="req", appts=[ex])
        assert store[0]["reschedule_proposed_datetime"] == at(2027, 2, 3, 10, 30).isoformat()
        assert store[0]["confirmed_datetime"] == at(2027, 2, 3, 10).isoformat()


class TestTheHelpTextMatchesThePicker:
    """/help described typing a date and time for a release after the picker
    shipped (fixed in v1.1.1). Pinned in English, where it can be read."""

    @pytest.mark.parametrize("key", ["help_appointment", "help_reschedule"])
    def test_it_says_tap_not_enter(self, key):
        from localization import CATALOG
        text = CATALOG["en"][key].lower()
        assert "tap" in text
        assert "enter" not in text and "date/time" not in text

    def test_myappointments_help_mentions_its_buttons(self):
        from localization import CATALOG
        text = CATALOG["en"]["help_myappointments"].lower()
        assert "reschedule" in text and "cancel" in text

    @pytest.mark.parametrize("lang", ["en", "es", "fr", "zu"])
    def test_every_language_points_reschedule_at_myappointments(self, lang):
        from localization import CATALOG
        assert "/myappointments" in CATALOG[lang]["help_reschedule"]
