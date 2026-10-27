"""The officer-facing board commands: /boarditems, /addboarditem, /setboardofficer.

Three things get the most attention here.

**Markdown safety.** An action, a description or a URL is text an officer
typed. "Renew 501(c)(3) *and* file Form_990" has three markers in it, and one
unpaired marker makes Telegram reject the WHOLE message — so the notice never
arrives, months later, with nobody watching. Every rendered string is checked
for balance, and there is a guard against `**bold**`, which legacy Markdown
does not support and which parses as an unpaired marker.

**The flag is written as a real boolean.** is_board_officer() checks `is True`,
so a `"true"` string written here would read back as not-an-officer — the
toggle would appear to work and silently grant nothing.

**Editing the due date or the lead time clears last_notified.** Otherwise the
first notice of the new schedule is held back by up to a week by a marker left
over from the old one.
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

import board_items as bi
import handlers.board as hb
import permissions
import telegram.ext as ext


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _ctx() -> MagicMock:
    ctx = MagicMock()
    ctx.bot = MagicMock()
    ctx.user_data = {}
    return ctx


def _update(text: str = "", chat_id: int = 111) -> MagicMock:
    upd = MagicMock()
    upd.effective_chat.id = chat_id
    upd.effective_user.id = chat_id
    upd.effective_user.username = "officer"
    upd.effective_user.full_name = "Board Officer"
    upd.effective_user.first_name = "Board"
    upd.message.text = text
    upd.message.reply_text = AsyncMock()
    upd.callback_query = None
    return upd


def _cb_update(data: str, chat_id: int = 111) -> MagicMock:
    upd = _update(chat_id=chat_id)
    query = MagicMock()
    query.data = data
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.edit_message_reply_markup = AsyncMock()
    upd.callback_query = query
    upd.message = None
    return upd


def _item(**over):
    base = {"id": "AAA11111", "action": "Renew logo registration",
            "due": (date.today() + timedelta(days=48)).isoformat(),
            "notify_days": 60, "status": "pending", "description": "",
            "url": "", "history": []}
    base.update(over)
    return base


class FakeStore:
    """Stands in for storage: board items and users, both in memory."""

    def __init__(self, items=None, users=None):
        self.items = items if items is not None else []
        self.users = users if users is not None else []
        self.item_saves = 0
        self.user_saves = 0

    def install(self, monkeypatch):
        async def get_board_items():
            return self.items

        async def save_board_items(items):
            self.items = items
            self.item_saves += 1

        async def get_all_users():
            return self.users

        async def save_users(users):
            self.users = users
            self.user_saves += 1

        for module in (hb, permissions):
            monkeypatch.setattr(module.storage, "get_board_items", get_board_items,
                                raising=False)
            monkeypatch.setattr(module.storage, "save_board_items", save_board_items,
                                raising=False)
            monkeypatch.setattr(module.storage, "get_all_users", get_all_users)
            monkeypatch.setattr(module.storage, "save_users", save_users)
        return self


def sent_text(update) -> str:
    """The text of the last thing sent, whether a reply or a message edit."""
    if update.callback_query is not None:
        call = update.callback_query.edit_message_text.await_args
    else:
        call = update.message.reply_text.await_args
    assert call is not None, "nothing was sent"
    return call.args[0] if call.args else call.kwargs["text"]


def sent_markup(update):
    if update.callback_query is not None:
        call = update.callback_query.edit_message_text.await_args
    else:
        call = update.message.reply_text.await_args
    return call.kwargs.get("reply_markup")


def button_labels(markup) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row]


def button_data(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row
            if b.callback_data]


# ---------------------------------------------------------------------------
# Markdown safety
# ---------------------------------------------------------------------------

NASTY = "Renew 501(c)(3) *and* file Form_990 [see notes]"


def balanced(text: str, marker: str) -> bool:
    return text.count(marker) % 2 == 0


class TestMarkdownSafety:
    def test_queue_escapes_an_action_full_of_markers(self):
        text = hb.render_queue([_item(action=NASTY)], date.today())
        assert r"Form\_990" in text
        assert balanced(text, "*"), text

    def test_detail_escapes_the_action(self):
        text = hb.render_item(_item(action=NASTY), date.today())
        assert r"Form\_990" in text
        assert balanced(text, "*"), text

    def test_detail_escapes_the_description(self):
        text = hb.render_item(_item(description="EIN 12_3456789 *draft*"), date.today())
        assert r"12\_3456789" in text
        assert balanced(text, "*"), text

    def test_detail_escapes_a_non_tappable_url(self):
        """A url that fails is_button_url is shown as text, so it must be
        escaped — an underscore in it would reject the whole message."""
        text = hb.render_item(_item(url="ask Pastor for the_link"), date.today())
        assert r"the\_link" in text
        assert balanced(text, "*"), text

    def test_no_doubled_asterisks_anywhere(self):
        """Legacy Markdown has no `**bold**`: it parses as an empty emphasis
        plus an unpaired marker, which rejects the message. Mutation check:
        writing `**OVERDUE**` in _when makes this fail."""
        today = date.today()
        overdue = _item(due=(today - timedelta(days=3)).isoformat())
        due_today = _item(due=today.isoformat())
        for item in (overdue, due_today, _item()):
            for text in (hb.render_item(item, today), hb.render_queue([item], today)):
                assert "**" not in text, text

    def test_overdue_and_due_today_stay_balanced(self):
        today = date.today()
        for offset in (-10, -1, 0, 1):
            item = _item(due=(today + timedelta(days=offset)).isoformat())
            text = hb.render_item(item, today)
            assert balanced(text, "*"), (offset, text)

    def test_an_item_with_no_due_date_renders_rather_than_crashing(self):
        text = hb.render_item(_item(due="whenever"), date.today())
        assert "no due date set" in text


# ---------------------------------------------------------------------------
# The queue view
# ---------------------------------------------------------------------------


class TestQueueView:
    def test_items_appear_in_due_date_order(self):
        today = date.today()
        items = [
            _item(id="LATE", action="Later one", due=(today + timedelta(days=200)).isoformat()),
            _item(id="SOON", action="Sooner one", due=(today + timedelta(days=5)).isoformat()),
        ]
        text = hb.render_queue(items, today)
        assert text.index("Sooner one") < text.index("Later one")

    def test_keyboard_order_matches_the_list_order(self):
        """The numbers in the text and the numbers on the buttons must agree,
        or an officer taps the wrong obligation."""
        today = date.today()
        items = [
            _item(id="LATE", action="Later one", due=(today + timedelta(days=200)).isoformat()),
            _item(id="SOON", action="Sooner one", due=(today + timedelta(days=5)).isoformat()),
        ]
        labels = button_labels(hb.queue_keyboard(items, today))
        assert "1." in labels[0] and "Sooner one" in labels[0]
        assert "2." in labels[1] and "Later one" in labels[1]

    def test_completed_and_deleted_items_are_not_listed(self):
        today = date.today()
        items = [_item(id="P", action="Pending one"),
                 _item(id="C", action="Done one", status="completed"),
                 _item(id="D", action="Gone one", status="deleted")]
        text = hb.render_queue(items, today)
        assert "Pending one" in text
        assert "Done one" not in text and "Gone one" not in text

    def test_empty_queue_says_so_and_points_at_the_command(self):
        text = hb.render_queue([], date.today())
        assert "Nothing pending" in text and "/addboarditem" in text

    def test_urgency_matches_the_notification_thresholds(self):
        """The glyph and the bot's own schedule must not disagree — a 🟡 item
        the bot is already nagging daily about would be misleading."""
        today = date.today()
        inside_daily = _item(due=(today + timedelta(days=10)).isoformat())
        inside_lead = _item(due=(today + timedelta(days=40)).isoformat())
        far_off = _item(due=(today + timedelta(days=200)).isoformat())
        overdue = _item(due=(today - timedelta(days=1)).isoformat())
        assert hb._urgency(inside_daily, today) == "🔴"
        assert hb._urgency(overdue, today) == "🔴"
        assert hb._urgency(inside_lead, today) == "🟡"
        assert hb._urgency(far_off, today) == "⚪️"
        # and the thresholds really are the ones board_items notifies on
        assert bi.notice_due(inside_daily, today) is True
        assert bi.notice_due(far_off, today) is False

    def test_long_actions_are_trimmed_for_button_labels(self):
        """Telegram caps button text; an untrimmed label fails the send."""
        label = button_labels(hb.queue_keyboard([_item(action="x" * 300)],
                                                date.today()))[0]
        assert len(label) < 80


# ---------------------------------------------------------------------------
# /boarditems — the gate and the router
# ---------------------------------------------------------------------------


class TestBoardItemsGate:
    async def test_an_officer_sees_the_queue(self, monkeypatch):
        FakeStore(items=[_item()],
                  users=[{"chat_id": 111, "board_officer": True}]).install(monkeypatch)
        upd = _update()
        assert await hb.cmd_boarditems(upd, _ctx()) == hb.BI_MENU
        assert "Board Items" in sent_text(upd)

    async def test_a_non_officer_is_refused(self, monkeypatch):
        FakeStore(items=[_item()], users=[{"chat_id": 111}]).install(monkeypatch)
        upd = _update()
        assert await hb.cmd_boarditems(upd, _ctx()) == ext.ConversationHandler.END
        assert "Unknown command" in sent_text(upd)

    async def test_the_refusal_does_not_name_the_feature(self, monkeypatch):
        FakeStore(users=[{"chat_id": 111}]).install(monkeypatch)
        upd = _update()
        await hb.cmd_boarditems(upd, _ctx())
        assert "board" not in sent_text(upd).lower()

    async def test_a_revoked_officer_cannot_use_a_stale_button(self, monkeypatch):
        """The keyboard outlives the permission. The board changes by vote, and
        the flag is a runtime value precisely so that takes effect at once."""
        FakeStore(items=[_item()], users=[{"chat_id": 111}]).install(monkeypatch)
        upd = _cb_update("bi:show:AAA11111")
        assert await hb.board_callback(upd, _ctx()) == ext.ConversationHandler.END
        assert "Unknown command" in sent_text(upd)


class TestRouter:
    def _officer_store(self, monkeypatch, items=None):
        return FakeStore(items=items if items is not None else [_item()],
                         users=[{"chat_id": 111, "board_officer": True}]
                         ).install(monkeypatch)

    async def test_show_opens_the_detail_view(self, monkeypatch):
        self._officer_store(monkeypatch)
        upd = _cb_update("bi:show:AAA11111")
        assert await hb.board_callback(upd, _ctx()) == hb.BI_MENU
        assert "Renew logo registration" in sent_text(upd)

    async def test_back_returns_to_the_queue(self, monkeypatch):
        self._officer_store(monkeypatch)
        upd = _cb_update("bi:list")
        await hb.board_callback(upd, _ctx())
        assert "Board Items" in sent_text(upd)

    async def test_close_ends_the_conversation(self, monkeypatch):
        self._officer_store(monkeypatch)
        upd = _cb_update("bi:close")
        assert await hb.board_callback(upd, _ctx()) == ext.ConversationHandler.END

    async def test_a_vanished_item_says_so_instead_of_failing(self, monkeypatch):
        """Two officers with the list open; one completes an item."""
        self._officer_store(monkeypatch, items=[])
        upd = _cb_update("bi:show:GONE1234")
        assert await hb.board_callback(upd, _ctx()) == hb.BI_MENU
        assert "no longer on the list" in sent_text(upd)

    async def test_an_unrecognised_callback_does_not_crash(self, monkeypatch):
        self._officer_store(monkeypatch)
        upd = _cb_update("bi:wat:AAA11111")
        assert await hb.board_callback(upd, _ctx()) == hb.BI_MENU


# ---------------------------------------------------------------------------
# Completing, rolling forward, deleting
# ---------------------------------------------------------------------------


class TestCompletion:
    def _store(self, monkeypatch, items=None):
        return FakeStore(items=items if items is not None else [_item()],
                         users=[{"chat_id": 111, "board_officer": True}]
                         ).install(monkeypatch)

    async def test_complete_asks_first(self, monkeypatch):
        store = self._store(monkeypatch)
        upd = _cb_update("bi:done:AAA11111")
        await hb.board_callback(upd, _ctx())
        assert bi.status_of(store.items[0]) == bi.STATUS_PENDING, "saved without asking"
        assert "completed?" in sent_text(upd)

    async def test_confirming_completes_and_persists(self, monkeypatch):
        store = self._store(monkeypatch)
        upd = _cb_update("bi:done_yes:AAA11111")
        assert await hb.board_callback(upd, _ctx()) == hb.BI_NEXT_DUE
        assert bi.status_of(store.items[0]) == bi.STATUS_COMPLETED
        assert store.item_saves == 1

    async def test_completing_records_who_did_it(self, monkeypatch):
        store = self._store(monkeypatch)
        await hb.board_callback(_cb_update("bi:done_yes:AAA11111"), _ctx())
        assert store.items[0]["completed_by"] == "Board Officer"

    async def test_it_offers_the_next_cycle(self, monkeypatch):
        self._store(monkeypatch)
        upd = _cb_update("bi:done_yes:AAA11111")
        ctx = _ctx()
        await hb.board_callback(upd, ctx)
        assert ctx.user_data["bi_next_due_id"] == "AAA11111"
        assert "/skip" in sent_text(upd)

    async def test_a_new_date_requeues_the_same_item(self, monkeypatch):
        """The whole point of the roll-forward: one item, many cycles, not a
        new item per renewal."""
        store = self._store(monkeypatch, items=[_item(status="completed",
                                                      completed_at="2026-01-01")])
        ctx = _ctx()
        ctx.user_data["bi_next_due_id"] = "AAA11111"
        upd = _update("2030-03-14")
        assert await hb.bi_next_due(upd, ctx) == hb.BI_MENU
        assert len(store.items) == 1
        assert bi.status_of(store.items[0]) == bi.STATUS_PENDING
        assert store.items[0]["due"] == "2030-03-14"

    async def test_skipping_leaves_it_retired(self, monkeypatch):
        store = self._store(monkeypatch, items=[_item(status="completed")])
        ctx = _ctx()
        ctx.user_data["bi_next_due_id"] = "AAA11111"
        upd = _update()
        assert await hb.bi_skip_next_due(upd, ctx) == ext.ConversationHandler.END
        assert bi.status_of(store.items[0]) == bi.STATUS_COMPLETED
        assert store.item_saves == 0

    async def test_a_bad_date_reprompts_without_saving(self, monkeypatch):
        store = self._store(monkeypatch, items=[_item(status="completed")])
        ctx = _ctx()
        ctx.user_data["bi_next_due_id"] = "AAA11111"
        upd = _update("next March")
        assert await hb.bi_next_due(upd, ctx) == hb.BI_NEXT_DUE
        assert store.item_saves == 0

    async def test_a_past_next_date_is_accepted_with_a_warning(self, monkeypatch):
        """Refusing it would be wrong — a lapsed obligation belongs on the
        list — but it will nag daily from now, so say so."""
        store = self._store(monkeypatch, items=[_item(status="completed")])
        ctx = _ctx()
        ctx.user_data["bi_next_due_id"] = "AAA11111"
        upd = _update((date.today() - timedelta(days=5)).isoformat())
        await hb.bi_next_due(upd, ctx)
        assert bi.status_of(store.items[0]) == bi.STATUS_PENDING
        assert "every day" in sent_text(upd)

    async def test_reopen_prompts_for_a_date(self, monkeypatch):
        self._store(monkeypatch, items=[_item(status="completed")])
        upd = _cb_update("bi:reopen:AAA11111")
        ctx = _ctx()
        assert await hb.board_callback(upd, ctx) == hb.BI_NEXT_DUE
        assert ctx.user_data["bi_next_due_id"] == "AAA11111"

    async def test_completed_items_offer_reopen_not_complete(self, monkeypatch):
        markup = hb.item_keyboard(_item(status="completed"))
        data = button_data(markup)
        assert any("reopen" in d for d in data)
        assert not any("done" in d for d in data)


class TestDeletion:
    def _store(self, monkeypatch, items=None):
        return FakeStore(items=items if items is not None else [_item()],
                         users=[{"chat_id": 111, "board_officer": True}]
                         ).install(monkeypatch)

    async def test_delete_asks_first(self, monkeypatch):
        store = self._store(monkeypatch)
        upd = _cb_update("bi:del:AAA11111")
        await hb.board_callback(upd, _ctx())
        assert bi.status_of(store.items[0]) == bi.STATUS_PENDING
        assert store.item_saves == 0

    async def test_the_prompt_steers_a_met_obligation_to_complete(self, monkeypatch):
        """Delete loses the renewal history; Complete keeps it. The difference
        matters and the officer is told at the moment of choosing."""
        self._store(monkeypatch)
        upd = _cb_update("bi:del:AAA11111")
        await hb.board_callback(upd, _ctx())
        text = sent_text(upd)
        assert "Mark completed" in text and "history" in text

    async def test_confirming_soft_deletes(self, monkeypatch):
        store = self._store(monkeypatch)
        await hb.board_callback(_cb_update("bi:del_yes:AAA11111"), _ctx())
        assert bi.status_of(store.items[0]) == bi.STATUS_DELETED
        assert store.items[0]["action"] == "Renew logo registration", "hard-deleted"

    async def test_a_deleted_item_stops_notifying(self, monkeypatch):
        store = self._store(monkeypatch)
        await hb.board_callback(_cb_update("bi:del_yes:AAA11111"), _ctx())
        assert bi.notice_due(store.items[0], date.today()) is False


# ---------------------------------------------------------------------------
# Editing
# ---------------------------------------------------------------------------


class TestEditing:
    def _store(self, monkeypatch, items=None):
        return FakeStore(items=items if items is not None else [_item()],
                         users=[{"chat_id": 111, "board_officer": True}]
                         ).install(monkeypatch)

    async def test_edit_offers_every_field(self, monkeypatch):
        self._store(monkeypatch)
        upd = _cb_update("bi:edit:AAA11111")
        await hb.board_callback(upd, _ctx())
        data = " ".join(button_data(sent_markup(upd)))
        for field, _ in hb.EDIT_FIELDS:
            assert f"ef_{field}" in data, field

    async def test_choosing_a_field_waits_for_the_value(self, monkeypatch):
        self._store(monkeypatch)
        upd = _cb_update("bi:ef_action:AAA11111")
        ctx = _ctx()
        assert await hb.board_callback(upd, ctx) == hb.BI_EDIT_VALUE
        assert ctx.user_data["bi_edit"] == {"id": "AAA11111", "field": "action"}

    async def test_an_unknown_field_falls_back_to_the_item(self, monkeypatch):
        self._store(monkeypatch)
        upd = _cb_update("bi:ef_nonsense:AAA11111")
        assert await hb.board_callback(upd, _ctx()) == hb.BI_MENU

    async def test_editing_the_action_saves_it(self, monkeypatch):
        store = self._store(monkeypatch)
        ctx = _ctx()
        ctx.user_data["bi_edit"] = {"id": "AAA11111", "field": "action"}
        assert await hb.bi_edit_value(_update("Renew trademark"), ctx) == hb.BI_MENU
        assert store.items[0]["action"] == "Renew trademark"

    async def test_editing_the_due_date_clears_the_send_marker(self, monkeypatch):
        """Moving the date without clearing last_notified holds the first
        notice of the NEW schedule back by up to a week."""
        store = self._store(monkeypatch,
                            items=[_item(last_notified=date.today().isoformat())])
        ctx = _ctx()
        ctx.user_data["bi_edit"] = {"id": "AAA11111", "field": "due"}
        await hb.bi_edit_value(_update("2031-01-05"), ctx)
        assert "last_notified" not in store.items[0]

    async def test_editing_the_lead_time_clears_the_send_marker(self, monkeypatch):
        store = self._store(monkeypatch,
                            items=[_item(last_notified=date.today().isoformat())])
        ctx = _ctx()
        ctx.user_data["bi_edit"] = {"id": "AAA11111", "field": "notify_days"}
        await hb.bi_edit_value(_update("90"), ctx)
        assert "last_notified" not in store.items[0]

    async def test_editing_the_description_keeps_the_send_marker(self, monkeypatch):
        """Only the schedule's own inputs reset it. Fixing a typo in the notes
        must not re-trigger today's notice."""
        today = date.today().isoformat()
        store = self._store(monkeypatch, items=[_item(last_notified=today)])
        ctx = _ctx()
        ctx.user_data["bi_edit"] = {"id": "AAA11111", "field": "description"}
        await hb.bi_edit_value(_update("new notes"), ctx)
        assert store.items[0]["last_notified"] == today

    async def test_a_bad_value_reprompts_without_saving(self, monkeypatch):
        store = self._store(monkeypatch)
        ctx = _ctx()
        ctx.user_data["bi_edit"] = {"id": "AAA11111", "field": "due"}
        assert await hb.bi_edit_value(_update("soon"), ctx) == hb.BI_EDIT_VALUE
        assert store.item_saves == 0

    async def test_skip_clears_an_optional_field(self, monkeypatch):
        store = self._store(monkeypatch, items=[_item(url="https://example.test/a")])
        ctx = _ctx()
        ctx.user_data["bi_edit"] = {"id": "AAA11111", "field": "url"}
        await hb.bi_skip_edit(_update(), ctx)
        assert store.items[0]["url"] == ""

    async def test_skip_refuses_to_empty_a_required_field(self, monkeypatch):
        store = self._store(monkeypatch)
        ctx = _ctx()
        ctx.user_data["bi_edit"] = {"id": "AAA11111", "field": "action"}
        upd = _update()
        assert await hb.bi_skip_edit(upd, ctx) == hb.BI_EDIT_VALUE
        assert store.item_saves == 0
        assert store.items[0]["action"] == "Renew logo registration"

    async def test_an_edit_with_no_pending_field_ends_cleanly(self, monkeypatch):
        self._store(monkeypatch)
        upd = _update("whatever")
        assert await hb.bi_edit_value(upd, _ctx()) == ext.ConversationHandler.END

    async def test_an_edit_of_a_vanished_item_ends_cleanly(self, monkeypatch):
        self._store(monkeypatch, items=[])
        ctx = _ctx()
        ctx.user_data["bi_edit"] = {"id": "AAA11111", "field": "action"}
        upd = _update("new text")
        assert await hb.bi_edit_value(upd, ctx) == ext.ConversationHandler.END


class TestValidation:
    def test_action_length_is_bounded(self):
        assert hb._validate("action", "")[1] is not None
        assert hb._validate("action", "x" * (bi.ACTION_MAX + 1))[1] is not None
        assert hb._validate("action", "ok")[0] == "ok"

    def test_due_must_parse(self):
        assert hb._validate("due", "2027-03-14")[0] == "2027-03-14"
        assert hb._validate("due", "14/03/2027")[1] is not None

    def test_lead_time_must_be_a_sane_number(self):
        assert hb._validate("notify_days", "60")[0] == 60
        for bad in ("sixty", "0", "-1", str(hb.MAX_NOTIFY_DAYS + 1), "6.5"):
            assert hb._validate("notify_days", bad)[1] is not None, bad

    def test_url_must_be_one_telegram_will_accept(self):
        assert hb._validate("url", "https://example.test/x")[0] == "https://example.test/x"
        for bad in ("example.test", "ask Pastor", "https://ex ample.test"):
            assert hb._validate("url", bad)[1] is not None, bad

    def test_description_length_is_bounded(self):
        assert hb._validate("description", "x" * (bi.DESCRIPTION_MAX + 1))[1] is not None
        assert hb._validate("description", "")[0] == ""


# ---------------------------------------------------------------------------
# /addboarditem
# ---------------------------------------------------------------------------


class TestAddBoardItem:
    def _store(self, monkeypatch, officer=True):
        user = {"chat_id": 111}
        if officer:
            user["board_officer"] = True
        return FakeStore(items=[], users=[user]).install(monkeypatch)

    async def test_a_non_officer_cannot_add(self, monkeypatch):
        self._store(monkeypatch, officer=False)
        upd = _update()
        assert await hb.cmd_addboarditem(upd, _ctx()) == ext.ConversationHandler.END
        assert "Unknown command" in sent_text(upd)

    async def _full_flow(self, monkeypatch, url="https://example.test/renew",
                         due=None, confirm="yes"):
        store = self._store(monkeypatch)
        ctx = _ctx()
        due = due or (date.today() + timedelta(days=100)).isoformat()
        assert await hb.cmd_addboarditem(_update(), ctx) == hb.AB_ACTION
        assert await hb.ab_action(_update("Renew tax exemption"), ctx) == hb.AB_DUE
        assert await hb.ab_due(_update(due), ctx) == hb.AB_NOTIFY
        assert await hb.ab_notify(_update("90"), ctx) == hb.AB_DESC
        assert await hb.ab_desc(_update("Form 990 due annually"), ctx) == hb.AB_URL
        assert await hb.ab_url(_update(url), ctx) == hb.AB_CONFIRM
        final = await hb.ab_confirm(_update(confirm), ctx)
        return store, ctx, final

    async def test_the_full_flow_saves_every_field(self, monkeypatch):
        store, _, final = await self._full_flow(monkeypatch)
        assert final == ext.ConversationHandler.END
        assert len(store.items) == 1
        item = store.items[0]
        assert item["action"] == "Renew tax exemption"
        assert item["notify_days"] == 90
        assert item["description"] == "Form 990 due annually"
        assert item["url"] == "https://example.test/renew"
        assert item["status"] == bi.STATUS_PENDING
        assert item["created_by"] == "Board Officer"
        assert len(item["id"]) == 8

    async def test_declining_saves_nothing(self, monkeypatch):
        store, _, final = await self._full_flow(monkeypatch, confirm="no")
        assert final == ext.ConversationHandler.END
        assert store.items == []
        assert store.item_saves == 0

    async def test_a_saved_item_is_immediately_schedulable(self, monkeypatch):
        """The acceptance test for the whole chain: what the conversation
        writes must be a record board_items can act on."""
        store, _, _ = await self._full_flow(
            monkeypatch, due=(date.today() + timedelta(days=30)).isoformat())
        item = store.items[0]
        assert bi.notice_due(item, date.today()) is True
        assert item in bi.pending(store.items)

    async def test_skipping_the_lead_time_uses_the_default(self, monkeypatch):
        self._store(monkeypatch)
        ctx = _ctx()
        assert await hb.ab_skip_notify(_update(), ctx) == hb.AB_DESC
        assert ctx.user_data["ab_notify"] == bi.NOTIFY_DAYS_DEFAULT

    async def test_skipping_the_details_and_link_leaves_them_empty(self, monkeypatch):
        self._store(monkeypatch)
        ctx = _ctx()
        await hb.ab_skip_desc(_update(), ctx)
        await hb.ab_skip_url(_update(), ctx)
        assert ctx.user_data["ab_desc"] == "" and ctx.user_data["ab_url"] == ""

    async def test_a_bad_link_is_caught_at_entry(self, monkeypatch):
        """Telegram rejects the ENTIRE message over a bad button url. Finding
        that out when the first notice is due, months later, is far worse."""
        self._store(monkeypatch)
        ctx = _ctx()
        upd = _update("zoom.us/j/1")
        assert await hb.ab_url(upd, ctx) == hb.AB_URL
        assert "http" in sent_text(upd)

    async def test_a_bad_date_reprompts(self, monkeypatch):
        self._store(monkeypatch)
        ctx = _ctx()
        assert await hb.ab_due(_update("March"), ctx) == hb.AB_DUE

    async def test_a_past_due_date_is_accepted_with_a_warning(self, monkeypatch):
        self._store(monkeypatch)
        ctx = _ctx()
        upd = _update((date.today() - timedelta(days=2)).isoformat())
        assert await hb.ab_due(upd, ctx) == hb.AB_NOTIFY
        warnings = [c.args[0] for c in upd.message.reply_text.await_args_list]
        assert any("every day" in w for w in warnings), warnings

    async def test_an_empty_action_reprompts(self, monkeypatch):
        self._store(monkeypatch)
        ctx = _ctx()
        assert await hb.ab_action(_update("   "), ctx) == hb.AB_ACTION

    async def test_the_confirmation_says_when_notices_will_start(self, monkeypatch):
        """An item added 100 days out with a 90-day lead time is silent for ten
        days. Without this line that is indistinguishable from a broken bot."""
        store, _, _ = await self._full_flow(monkeypatch)
        starts = bi.due_date(store.items[0]) - timedelta(days=90)
        assert hb._when_notices_start(store.items[0], date.today()) == (
            f"Notices start on {starts.isoformat()}, 90 days before it is due.")

    async def test_it_says_today_when_notices_are_already_owed(self, monkeypatch):
        item = _item(due=(date.today() + timedelta(days=5)).isoformat())
        assert hb._when_notices_start(item, date.today()) == "Notices start today."


# ---------------------------------------------------------------------------
# /setboardofficer
# ---------------------------------------------------------------------------


class TestSetBoardOfficer:
    def _store(self, monkeypatch, users=None):
        return FakeStore(items=[], users=users if users is not None else [
            {"chat_id": 111, "display_name": "Samuel Eaves", "username": "sjeaves2"},
            {"chat_id": 222, "display_name": "Christel Crowdy"},
        ]).install(monkeypatch)

    def _as_admin(self, monkeypatch):
        monkeypatch.setattr(permissions, "ADMIN_USERNAMES", {"officer"})

    async def test_a_non_admin_cannot_open_it(self, monkeypatch):
        self._store(monkeypatch)
        monkeypatch.setattr(permissions, "ADMIN_USERNAMES", set())
        monkeypatch.setattr(permissions, "_admin_chat_ids", set())
        upd = _update()
        await hb.cmd_setboardofficer(upd, _ctx())
        assert "Unknown command" in sent_text(upd)

    async def test_an_admin_sees_every_registered_user(self, monkeypatch):
        """Gated on admin, NOT on board_officer: the officer gate would need an
        officer to already exist, so nobody could create the first one."""
        self._store(monkeypatch)
        self._as_admin(monkeypatch)
        upd = _update()
        await hb.cmd_setboardofficer(upd, _ctx())
        labels = " ".join(button_labels(sent_markup(upd)))
        assert "Samuel Eaves" in labels and "Christel Crowdy" in labels

    async def test_toggling_on_writes_a_real_boolean(self, monkeypatch):
        """is_board_officer checks `is True`. A "true" string written here
        would read back as not-an-officer: the toggle would show ✅ and grant
        nothing at all."""
        store = self._store(monkeypatch)
        self._as_admin(monkeypatch)
        await hb.board_officer_callback(_cb_update("bo:toggle:222"), _ctx())
        record = next(u for u in store.users if u["chat_id"] == 222)
        assert record["board_officer"] is True
        assert not isinstance(record["board_officer"], str)

    async def test_the_toggle_takes_effect_for_the_permission_check(self, monkeypatch):
        store = self._store(monkeypatch)
        self._as_admin(monkeypatch)
        await hb.board_officer_callback(_cb_update("bo:toggle:222"), _ctx())
        assert await permissions.is_board_officer(_update(chat_id=222)) is True
        assert store.user_saves == 1

    async def test_toggling_off_removes_the_flag(self, monkeypatch):
        store = self._store(monkeypatch, users=[
            {"chat_id": 222, "display_name": "X", "board_officer": True}])
        self._as_admin(monkeypatch)
        await hb.board_officer_callback(_cb_update("bo:toggle:222"), _ctx())
        assert "board_officer" not in store.users[0]
        assert await permissions.is_board_officer(_update(chat_id=222)) is False

    async def test_toggling_leaves_the_rest_of_the_record_alone(self, monkeypatch):
        store = self._store(monkeypatch, users=[
            {"chat_id": 222, "display_name": "X", "language": "en",
             "timezone": "America/New_York", "notif_prefs": ["convocations"]}])
        self._as_admin(monkeypatch)
        await hb.board_officer_callback(_cb_update("bo:toggle:222"), _ctx())
        record = store.users[0]
        assert record["language"] == "en"
        assert record["timezone"] == "America/New_York"
        assert record["notif_prefs"] == ["convocations"]

    async def test_toggling_one_user_does_not_touch_another(self, monkeypatch):
        store = self._store(monkeypatch)
        self._as_admin(monkeypatch)
        await hb.board_officer_callback(_cb_update("bo:toggle:222"), _ctx())
        assert "board_officer" not in next(u for u in store.users if u["chat_id"] == 111)

    async def test_an_unknown_chat_id_is_ignored(self, monkeypatch):
        store = self._store(monkeypatch)
        self._as_admin(monkeypatch)
        await hb.board_officer_callback(_cb_update("bo:toggle:999"), _ctx())
        assert store.user_saves == 0

    async def test_a_malformed_callback_is_ignored(self, monkeypatch):
        store = self._store(monkeypatch)
        self._as_admin(monkeypatch)
        await hb.board_officer_callback(_cb_update("bo:toggle:notanumber"), _ctx())
        assert store.user_saves == 0

    async def test_a_non_admin_cannot_toggle_with_a_stale_button(self, monkeypatch):
        store = self._store(monkeypatch)
        monkeypatch.setattr(permissions, "ADMIN_USERNAMES", set())
        monkeypatch.setattr(permissions, "_admin_chat_ids", set())
        upd = _cb_update("bo:toggle:222")
        await hb.board_officer_callback(upd, _ctx())
        assert store.user_saves == 0
        assert "Unknown command" in sent_text(upd)

    async def test_done_lists_the_current_board(self, monkeypatch):
        self._store(monkeypatch, users=[
            {"chat_id": 111, "display_name": "Samuel Eaves", "board_officer": True},
            {"chat_id": 222, "display_name": "Someone Else"}])
        self._as_admin(monkeypatch)
        upd = _cb_update("bo:done")
        await hb.board_officer_callback(upd, _ctx())
        text = sent_text(upd)
        assert "Samuel Eaves" in text and "Someone Else" not in text

    async def test_no_registered_users_says_so(self, monkeypatch):
        self._store(monkeypatch, users=[])
        self._as_admin(monkeypatch)
        upd = _update()
        await hb.cmd_setboardofficer(upd, _ctx())
        assert "No registered users" in sent_text(upd)

    def test_a_grown_roster_degrades_with_a_message(self):
        """Telegram caps inline keyboards, and a hundred-row picker is unusable
        well before that."""
        users = [{"chat_id": i, "display_name": f"User {i}"} for i in range(80)]
        assert len(hb._officer_keyboard(users).inline_keyboard) <= \
            hb.OFFICER_PICKER_LIMIT + 1
        assert "Showing the first" in hb._officer_prompt(users)

    def test_a_small_roster_shows_no_warning(self):
        users = [{"chat_id": 1, "display_name": "A"}]
        assert "Showing the first" not in hb._officer_prompt(users)

    async def test_the_roster_escapes_a_display_name(self, monkeypatch):
        """A display name is whatever the member typed into Telegram, and the
        roster is sent as Markdown. "Sam_E *CFO*" has three markers in it."""
        self._store(monkeypatch, users=[
            {"chat_id": 1, "display_name": "Sam_E *CFO*", "board_officer": True}])
        self._as_admin(monkeypatch)
        upd = _cb_update("bo:done")
        await hb.board_officer_callback(upd, _ctx())
        text = sent_text(upd)
        assert r"Sam\_E" in text
        assert balanced(text, "*"), text


# ---------------------------------------------------------------------------
# Help text
# ---------------------------------------------------------------------------


class TestHelpGating:
    def test_the_board_block_is_shown_to_officers(self):
        import handlers.user_basics as ub
        text = ub._commands_text("en", is_adm=False, is_prayer=False, is_board=True)
        assert "/boarditems" in text and "/addboarditem" in text

    def test_the_board_block_is_hidden_from_everyone_else(self):
        """board_officer_only answers "Unknown command", so listing these for
        every admin would advertise a queue they cannot open."""
        import handlers.user_basics as ub
        for is_adm in (True, False):
            text = ub._commands_text("en", is_adm=is_adm, is_prayer=True, is_board=False)
            assert "/boarditems" not in text
            assert "/addboarditem" not in text

    def test_admins_are_told_how_to_seed_the_board(self):
        import handlers.user_basics as ub
        assert "/setboardofficer" in ub.ADMIN_COMMANDS_TEXT

    def test_the_board_block_does_not_advertise_a_command_that_does_not_exist(self):
        """Every slash command named in the officer help must be registered."""
        import re as _re

        import handlers.user_basics as ub
        named = set(_re.findall(r"/([a-z]+)", ub.BOARD_COMMANDS_TEXT))
        registered = {"boarditems", "addboarditem", "boardcalendar"}
        assert named <= registered, named - registered
