"""/prayer — submission, answering, dismissal, redaction and retention.

The behaviour that matters most here is not the happy path but the promises:
that only `prayer: true` admins can read a request, that the text is destroyed
the moment it is answered or dismissed, and that a request is never lost even
when nobody is reachable.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram.error import Forbidden
from telegram.ext import ConversationHandler

sys.path.insert(0, str(Path(__file__).parent.parent))

import handlers.prayer as P  # noqa: E402
import permissions  # noqa: E402
import storage  # noqa: E402
from cache import FileCache  # noqa: E402
from localization import AVAILABLE_LANGUAGES, t  # noqa: E402

LANGS = list(AVAILABLE_LANGUAGES)
MEMBER = 555


@pytest.fixture(autouse=True)
def _isolate(tmp_path):
    """Never touch the real prayer_requests.yaml."""
    storage.prayer_cache = FileCache(tmp_path / "prayer_requests.yaml")
    yield


def _update(text=None, uid=MEMBER, name="A Member", username="member"):
    upd = MagicMock()
    upd.effective_user.id = uid
    upd.effective_user.username = username
    upd.effective_user.full_name = name
    upd.message.text = text
    upd.message.reply_text = AsyncMock()
    return upd


def _ctx(**user_data):
    ctx = MagicMock()
    ctx.user_data = dict(user_data)
    ctx.bot.send_message = AsyncMock()
    ctx.args = []
    return ctx


async def _submit(text="Please pray for my mother's surgery on the 24th.",
                  admins=(101, 102), lang="en"):
    """Run the member flow to completion; returns (ctx, stored requests)."""
    ctx = _ctx()
    with patch.object(P, "get_user_prefs", AsyncMock(return_value=(None, lang))), \
         patch.object(P, "prayer_admin_chat_ids", AsyncMock(return_value=set(admins))), \
         patch.object(P.activity, "log_command", lambda *a, **k: None), \
         patch.object(P.activity, "log_error", lambda *a, **k: None):
        assert await P.pr_text(_update(text), ctx) == P.PR_CONFIRM
        end = await P.pr_confirm(_update("yes"), ctx)
    assert end == ConversationHandler.END
    return ctx, await storage.get_prayer_requests()


# ---------------------------------------------------------------------------
# Submission
# ---------------------------------------------------------------------------

class TestSubmission:
    async def test_request_is_stored_pending(self):
        _, reqs = await _submit()
        assert len(reqs) == 1
        assert reqs[0]["status"] == P.STATUS_PENDING
        assert "surgery" in reqs[0]["text"]

    async def test_request_gets_an_id(self):
        _, reqs = await _submit()
        assert reqs[0]["id"].startswith("PR-")

    async def test_ids_are_unique(self):
        assert len({P.new_request_id() for _ in range(200)}) == 200

    async def test_forwarded_to_every_prayer_admin(self):
        ctx, _ = await _submit(admins=(101, 102))
        assert {c.args[0] for c in ctx.bot.send_message.call_args_list} == {101, 102}

    async def test_member_gets_the_reference_id(self):
        """The member needs the id to refer to their request later."""
        ctx = _ctx()
        with patch.object(P, "get_user_prefs", AsyncMock(return_value=(None, "en"))), \
             patch.object(P, "prayer_admin_chat_ids", AsyncMock(return_value={101})), \
             patch.object(P.activity, "log_command", lambda *a, **k: None):
            await P.pr_text(_update("Pray for me"), ctx)
            confirm = _update("yes")
            await P.pr_confirm(confirm, ctx)
        req_id = (await storage.get_prayer_requests())[0]["id"]
        assert req_id in confirm.message.reply_text.call_args[0][0]

    async def test_over_the_limit_is_rejected(self):
        ctx = _ctx()
        upd = _update("x" * (P.PRAYER_MAX + 1))
        with patch.object(P, "get_user_prefs", AsyncMock(return_value=(None, "en"))):
            assert await P.pr_text(upd, ctx) == P.PR_TEXT
        assert "pr_text" not in ctx.user_data

    async def test_empty_is_rejected(self):
        ctx = _ctx()
        with patch.object(P, "get_user_prefs", AsyncMock(return_value=(None, "en"))):
            assert await P.pr_text(_update("   "), ctx) == P.PR_TEXT

    async def test_exactly_at_the_limit_is_accepted(self):
        ctx = _ctx()
        with patch.object(P, "get_user_prefs", AsyncMock(return_value=(None, "en"))):
            assert await P.pr_text(_update("y" * P.PRAYER_MAX), ctx) == P.PR_CONFIRM


class TestConfirmationStep:
    async def _confirm(self, answer, text="Pray for me"):
        ctx = _ctx(pr_text=text)
        upd = _update(answer)
        with patch.object(P, "get_user_prefs", AsyncMock(return_value=(None, "en"))), \
             patch.object(P, "prayer_admin_chat_ids", AsyncMock(return_value={101})), \
             patch.object(P.activity, "log_command", lambda *a, **k: None):
            state = await P.pr_confirm(upd, ctx)
        return state, ctx, upd

    async def test_cancel_discards_and_stores_nothing(self):
        state, ctx, _ = await self._confirm("cancel")
        assert state == ConversationHandler.END
        assert await storage.get_prayer_requests() == []
        assert "pr_text" not in ctx.user_data

    async def test_modify_returns_to_the_text_step(self):
        state, _, _ = await self._confirm("modify")
        assert state == P.PR_TEXT
        assert await storage.get_prayer_requests() == []

    async def test_modify_hands_back_the_text_to_copy(self):
        """A bot cannot pre-fill the compose box, so the text comes back in a
        monospace block the member can tap to copy."""
        _, _, upd = await self._confirm("modify", text="Pray for my father")
        said = upd.message.reply_text.call_args[0][0]
        assert "Pray for my father" in said
        assert "```" in said

    async def test_unclear_answer_asks_again(self):
        state, _, _ = await self._confirm("maybe later")
        assert state == P.PR_CONFIRM
        assert await storage.get_prayer_requests() == []


class TestDeliveryFailures:
    async def test_stored_even_when_no_admin_is_reachable(self):
        """A request must never be lost because nobody was available."""
        _, reqs = await _submit(admins=())
        assert len(reqs) == 1
        assert reqs[0]["status"] == P.STATUS_PENDING

    async def test_unreachable_admin_is_logged_as_an_error(self):
        ctx = _ctx()
        with patch.object(P, "get_user_prefs", AsyncMock(return_value=(None, "en"))), \
             patch.object(P, "prayer_admin_chat_ids", AsyncMock(return_value=set())), \
             patch.object(P.activity, "log_command", lambda *a, **k: None), \
             patch.object(P.activity, "log_error") as log_error:
            await P.pr_text(_update("Pray for me"), ctx)
            await P.pr_confirm(_update("yes"), ctx)
        assert log_error.called
        assert "no prayer admin" in log_error.call_args[0][0].lower()

    async def test_one_blocked_admin_does_not_stop_the_others(self):
        ctx = _ctx()
        ctx.bot.send_message = AsyncMock(
            side_effect=[Forbidden("blocked"), None])
        with patch.object(P, "get_user_prefs", AsyncMock(return_value=(None, "en"))), \
             patch.object(P, "prayer_admin_chat_ids", AsyncMock(return_value={101, 102})), \
             patch.object(P.activity, "log_command", lambda *a, **k: None), \
             patch.object(P.activity, "log_error", lambda *a, **k: None):
            await P.pr_text(_update("Pray for me"), ctx)
            await P.pr_confirm(_update("yes"), ctx)
        reqs = await storage.get_prayer_requests()
        assert reqs[0]["status"] == P.STATUS_PENDING


# ---------------------------------------------------------------------------
# The promise: text is destroyed when the request is closed
# ---------------------------------------------------------------------------

class TestRedaction:
    def test_redact_removes_the_text(self):
        req = {"id": "PR-1", "text": "something deeply personal",
               "status": P.STATUS_PENDING}
        P.redact(req, P.STATUS_ANSWERED, "Bishop Eaves")
        assert "text" not in req
        assert req["status"] == P.STATUS_ANSWERED
        assert req["closed_by"] == "Bishop Eaves"

    def test_redact_keeps_the_stub_fields(self):
        """Enough to trace a follow-up, nothing of what was shared."""
        req = {"id": "PR-1", "text": "private", "requester_name": "A Member",
               "created": "2026-09-16T10:00:00", "status": P.STATUS_PENDING}
        P.redact(req, P.STATUS_DISMISSED, "Pastor Crowdy")
        assert req["id"] == "PR-1"
        assert req["requester_name"] == "A Member"
        assert req["created"] == "2026-09-16T10:00:00"

    async def test_answering_deletes_the_text_immediately(self):
        _, reqs = await _submit()
        req_id = reqs[0]["id"]
        ctx = _ctx(pr_respond_id=req_id)
        with patch.object(P, "prayer_admin_chat_ids", AsyncMock(return_value={101})), \
             patch.object(P.activity, "log_command", lambda *a, **k: None):
            await P.resp_text(_update("We are praying with you.", uid=101,
                                      name="Bishop Eaves"), ctx)
        stored = await storage.get_prayer_requests()
        assert stored[0]["status"] == P.STATUS_ANSWERED
        assert "text" not in stored[0], "the member's words must be gone"

    async def test_dismissing_deletes_the_text_immediately(self):
        _, reqs = await _submit()
        ctx = _ctx()
        ctx.args = [reqs[0]["id"]]
        with patch.object(P, "prayer_admin_chat_ids", AsyncMock(return_value={101})), \
             patch.object(P.activity, "log_command", lambda *a, **k: None), \
             patch.object(permissions, "is_prayer_admin", lambda u: True):
            await P.cmd_dismissprayer(_update(uid=101), ctx)
        stored = await storage.get_prayer_requests()
        assert stored[0]["status"] == P.STATUS_DISMISSED
        assert "text" not in stored[0]

    async def test_undelivered_response_leaves_it_pending(self):
        """An answer nobody received is not an answered request."""
        _, reqs = await _submit()
        ctx = _ctx(pr_respond_id=reqs[0]["id"])
        ctx.bot.send_message = AsyncMock(side_effect=Forbidden("blocked"))
        with patch.object(P, "prayer_admin_chat_ids", AsyncMock(return_value={101})), \
             patch.object(P.activity, "log_command", lambda *a, **k: None):
            await P.resp_text(_update("Peace be with you", uid=101), ctx)
        stored = await storage.get_prayer_requests()
        assert stored[0]["status"] == P.STATUS_PENDING
        assert "text" in stored[0], "text must survive a failed delivery"


class TestRetention:
    async def test_pending_is_never_purged(self):
        await storage.save_prayer_requests(
            [{"id": "PR-1", "status": P.STATUS_PENDING, "created": "2020-01-01T00:00:00"}])
        assert await P.purge_old_stubs() == 0
        assert len(await storage.get_prayer_requests()) == 1

    async def test_pending_survives_even_with_a_stale_closed_date(self):
        """Status governs, not the date. Guards the pending check itself: a
        request with no closed date is kept by a different branch, so that case
        alone cannot prove pending requests are protected."""
        old = (P.now_tz() - timedelta(days=P.STUB_RETENTION_DAYS + 99)).isoformat()
        await storage.save_prayer_requests(
            [{"id": "PR-1", "status": P.STATUS_PENDING, "closed": old}])
        assert await P.purge_old_stubs() == 0
        assert len(await storage.get_prayer_requests()) == 1

    async def test_old_stub_is_purged(self):
        old = (P.now_tz() - timedelta(days=P.STUB_RETENTION_DAYS + 1)).isoformat()
        await storage.save_prayer_requests(
            [{"id": "PR-1", "status": P.STATUS_ANSWERED, "closed": old}])
        assert await P.purge_old_stubs() == 1
        assert await storage.get_prayer_requests() == []

    async def test_recent_stub_is_kept(self):
        recent = (P.now_tz() - timedelta(days=1)).isoformat()
        await storage.save_prayer_requests(
            [{"id": "PR-1", "status": P.STATUS_ANSWERED, "closed": recent}])
        assert await P.purge_old_stubs() == 0

    async def test_unreadable_close_date_is_kept(self):
        """Never silently discard a record we cannot date."""
        await storage.save_prayer_requests(
            [{"id": "PR-1", "status": P.STATUS_ANSWERED, "closed": "not-a-date"}])
        assert await P.purge_old_stubs() == 0


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------

class TestAccessControl:
    def test_prayer_flag_is_read_from_admins_yaml(self):
        assert permissions.PRAYER_USERNAMES, "no prayer admins configured"

    def test_plain_admin_is_not_a_prayer_admin(self):
        """Being an administrator is not enough to read prayer requests."""
        upd = MagicMock()
        upd.effective_user.id = 999
        upd.effective_user.username = "curtisadairjr"   # admin, no prayer flag
        assert permissions.is_admin(upd) is True
        assert permissions.is_prayer_admin(upd) is False

    def test_configured_prayer_admin_passes(self):
        upd = MagicMock()
        upd.effective_user.id = 1
        upd.effective_user.username = sorted(permissions.PRAYER_USERNAMES)[0]
        assert permissions.is_prayer_admin(upd) is True

    async def test_non_prayer_admin_cannot_list_requests(self):
        upd = _update(uid=999, username="curtisadairjr")
        with patch.object(permissions, "is_prayer_admin", lambda u: False):
            await P.cmd_prayerrequests(upd, _ctx())
        assert "Unknown command" in upd.message.reply_text.call_args[0][0]


# ---------------------------------------------------------------------------
# Helpers and localisation
# ---------------------------------------------------------------------------

class TestLookup:
    def test_find_is_case_insensitive(self):
        reqs = [{"id": "PR-4A7C2E"}]
        assert P.find_request(reqs, "pr-4a7c2e") is not None

    def test_find_tolerates_a_missing_prefix(self):
        reqs = [{"id": "PR-4A7C2E"}]
        assert P.find_request(reqs, "4A7C2E") is not None

    def test_find_returns_none_for_unknown(self):
        assert P.find_request([{"id": "PR-1"}], "PR-2") is None

    def test_pending_is_oldest_first(self):
        reqs = [
            {"id": "PR-2", "status": P.STATUS_PENDING, "created": "2026-09-16T10:00:00"},
            {"id": "PR-1", "status": P.STATUS_PENDING, "created": "2026-09-15T10:00:00"},
            {"id": "PR-3", "status": P.STATUS_ANSWERED, "created": "2026-09-14T10:00:00"},
        ]
        assert [r["id"] for r in P.pending_requests(reqs)] == ["PR-1", "PR-2"]


class TestAdminView:
    def test_members_markdown_is_escaped(self):
        """An unbalanced marker typed in distress must not stop delivery."""
        req = {"id": "PR-1", "requester_name": "A Member", "text": "pray *now",
               "created": datetime.now().isoformat()}
        assert "\\*now" in P.format_for_admin(req)

    def test_requester_name_is_escaped(self):
        req = {"id": "PR-1", "requester_name": "A_Member", "text": "hi",
               "created": datetime.now().isoformat()}
        assert "A\\_Member" in P.format_for_admin(req)

    def test_non_english_request_flags_the_language(self):
        """The bot does not translate prayer requests; say which language it is."""
        req = {"id": "PR-1", "requester_name": "X", "text": "Ore por mi",
               "lang": "es", "created": datetime.now().isoformat()}
        assert "language: es" in P.format_for_admin(req)

    def test_english_request_does_not_mention_language(self):
        req = {"id": "PR-1", "requester_name": "X", "text": "Pray for me",
               "lang": "en", "created": datetime.now().isoformat()}
        assert "language:" not in P.format_for_admin(req)


class TestLocalisation:
    @pytest.mark.parametrize("lang", LANGS)
    def test_all_prayer_strings_exist(self, lang):
        for key in ("prayer_prompt", "prayer_confirm", "prayer_modify",
                    "prayer_cancelled", "prayer_received", "prayer_response_intro",
                    "prayer_empty", "prayer_too_long", "help_prayer"):
            assert t(key, lang).strip(), f"{lang}: {key} is empty"

    @pytest.mark.parametrize("lang", LANGS)
    def test_prompt_asks_for_the_three_things(self, lang):
        """Kind of prayer, who it is for, and any date — in every language."""
        text = t("prayer_prompt", lang, limit=P.PRAYER_MAX)
        assert str(P.PRAYER_MAX) in text
        assert text.count("•") >= 3, f"{lang}: prompt lost its guidance bullets"

    @pytest.mark.parametrize("lang", LANGS)
    def test_confirmation_carries_the_scriptural_assurance(self, lang):
        """Step 7: strong enough to stand alone if nobody responds further."""
        text = t("prayer_received", lang, request_id="PR-1")
        assert len(text) > 200, f"{lang}: confirmation is too thin to stand alone"
        assert "PR-1" in text

    @pytest.mark.parametrize("lang", LANGS)
    def test_translations_are_not_english_copies(self, lang):
        if lang == "en":
            return
        assert t("prayer_received", lang) != t("prayer_received", "en")
        assert t("prayer_prompt", lang) != t("prayer_prompt", "en")

    @pytest.mark.parametrize("lang", LANGS)
    def test_command_list_advertises_prayer(self, lang):
        assert "/prayer" in t("user_commands", lang)

    @pytest.mark.parametrize("lang", LANGS)
    async def test_member_is_answered_in_their_own_language(self, lang):
        ctx = _ctx()
        with patch.object(P, "get_user_prefs", AsyncMock(return_value=(None, lang))), \
             patch.object(P, "prayer_admin_chat_ids", AsyncMock(return_value={101})), \
             patch.object(P.activity, "log_command", lambda *a, **k: None):
            upd = _update("Pray for me")
            await P.pr_text(upd, ctx)
            confirm = _update("yes")
            await P.pr_confirm(confirm, ctx)
        sent = confirm.message.reply_text.call_args[0][0]
        assert sent == t("prayer_received", lang,
                         request_id=(await storage.get_prayer_requests())[0]["id"])


# ---------------------------------------------------------------------------
# Wiring — a correct feature nothing registers is dead code
# ---------------------------------------------------------------------------

class TestPrayerIsWired:
    @staticmethod
    def _registered():
        import bot
        from telegram import BotCommand  # noqa: F401
        from telegram.ext import CommandHandler, ConversationHandler
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
        cmds = set()
        for h in seen:
            if isinstance(h, CommandHandler):
                cmds |= set(h.commands)
            elif isinstance(h, ConversationHandler):
                for e in h.entry_points:
                    if isinstance(e, CommandHandler):
                        cmds |= set(e.commands)
        return cmds

    @pytest.mark.parametrize("command", [
        "prayer", "prayerrequests", "respondprayer", "dismissprayer",
    ])
    def test_command_is_registered(self, command):
        assert command in self._registered()

    def test_prayer_is_in_the_telegram_command_menu(self):
        src = (Path(__file__).resolve().parents[1] / "bot.py").read_text()
        assert 'BotCommand("prayer"' in src

    async def test_daily_maintenance_purges_stubs(self):
        """Without this the stubs accumulate forever, quietly."""
        import bot
        with patch.object(bot, "archive_old_appointments", AsyncMock()), \
             patch.object(bot, "purge_archived_appointments", AsyncMock()), \
             patch.object(bot, "purge_old_announcements", AsyncMock()), \
             patch.object(bot, "purge_old_stubs", AsyncMock()) as purge, \
             patch.object(bot, "prune_log_file", lambda *a, **k: None):
            await bot.daily_maintenance_job(MagicMock())
        assert purge.called, "daily maintenance must purge old prayer stubs"


class TestStatsShowsPendingCount:
    def test_count_appears_in_the_system_report(self, tmp_path):
        from handlers import stats as st
        with patch.object(st, "ACTIVITY_LOG", tmp_path / "empty.log"):
            text = st.build_system_report(7, False, pending_prayers=3)
        assert "3" in text
        assert "rayer" in text

    def test_zero_is_reported_as_none_waiting(self, tmp_path):
        from handlers import stats as st
        with patch.object(st, "ACTIVITY_LOG", tmp_path / "empty.log"):
            text = st.build_system_report(7, False, pending_prayers=0)
        assert "none" in text.lower()

    def test_omitted_when_unavailable(self, tmp_path):
        """None means 'could not count' — better silent than wrong."""
        from handlers import stats as st
        with patch.object(st, "ACTIVITY_LOG", tmp_path / "empty.log"):
            text = st.build_system_report(7, False, pending_prayers=None)
        assert "Prayer requests waiting" not in text

    async def test_cmd_stats_supplies_the_count(self):
        """The report parameter is useless if /stats never fills it in."""
        from handlers import stats as st
        upd = _update(uid=1, username="sjeaves2")
        ctx = _ctx()
        ctx.args = []
        await storage.save_prayer_requests(
            [{"id": "PR-1", "status": P.STATUS_PENDING, "created": "2026-09-16T10:00:00"}])
        captured = {}

        def fake_report(days, clamped, now=None, job_queue=None, pending_prayers=None):
            captured["pending"] = pending_prayers
            return "report"

        with patch.object(st, "build_system_report", fake_report), \
             patch.object(st.activity, "log_command", lambda *a, **k: None), \
             patch("permissions.is_admin", lambda u: True):
            await st.cmd_stats(upd, ctx)
        assert captured["pending"] == 1


# ---------------------------------------------------------------------------
# Discoverability — a command nobody can find is barely shipped
# ---------------------------------------------------------------------------

class TestPrayerAdminHelp:
    """The prayer commands must be visible to prayer admins and ONLY to them.

    Listing them for every admin would advertise a queue that prayer_admin_only
    refuses with "Unknown command", contradicting the promise that requests stay
    with the designated leadership.
    """

    async def test_prayer_admin_sees_the_commands_in_adminhelp(self):
        import handlers.user_basics as UB
        upd = _update(uid=1, username="sjeaves2")
        with patch.object(UB.permissions, "is_prayer_admin", lambda u: True), \
             patch.object(UB.permissions, "is_admin", lambda u: True), \
             patch.object(UB.activity, "log_command", lambda *a, **k: None):
            await UB.cmd_adminhelp(upd, _ctx())
        text = upd.message.reply_text.call_args[0][0]
        for cmd in ("/prayerrequests", "/respondprayer", "/dismissprayer"):
            assert cmd in text, f"{cmd} missing from /adminhelp for a prayer admin"

    async def test_plain_admin_does_not_see_them(self):
        import handlers.user_basics as UB
        upd = _update(uid=2, username="curtisadairjr")
        with patch.object(UB.permissions, "is_prayer_admin", lambda u: False), \
             patch.object(UB.permissions, "is_admin", lambda u: True), \
             patch.object(UB.activity, "log_command", lambda *a, **k: None):
            await UB.cmd_adminhelp(upd, _ctx())
        text = upd.message.reply_text.call_args[0][0]
        for cmd in ("/prayerrequests", "/respondprayer", "/dismissprayer"):
            assert cmd not in text, f"{cmd} shown to an admin who cannot use it"
        assert "/addevent" in text, "ordinary admin commands should still be listed"

    def test_commands_text_gates_the_prayer_block(self):
        import handlers.user_basics as UB
        with_prayer = UB._commands_text("en", is_adm=True, is_prayer=True)
        without = UB._commands_text("en", is_adm=True, is_prayer=False)
        assert "/prayerrequests" in with_prayer
        assert "/prayerrequests" not in without

    def test_member_sees_neither_block(self):
        import handlers.user_basics as UB
        text = UB._commands_text("en", is_adm=False, is_prayer=False)
        assert "/prayerrequests" not in text
        assert "/addevent" not in text
        assert "/prayer" in text, "the member-facing /prayer must still be listed"

    def test_the_block_explains_the_deletion(self):
        """An admin should know that answering destroys the member's words."""
        import handlers.user_basics as UB
        assert "deletes" in UB.PRAYER_COMMANDS_TEXT.lower()

    async def test_help_shows_them_to_a_prayer_admin(self):
        """/help, not just /adminhelp — it is where people actually look."""
        import handlers.user_basics as UB
        upd = _update(uid=1, username="sjeaves2")
        ctx = _ctx()
        ctx.args = []
        with patch.object(UB.permissions, "is_prayer_admin", lambda u: True), \
             patch.object(UB.permissions, "is_admin", lambda u: True), \
             patch.object(UB, "get_user_prefs", AsyncMock(return_value=(None, "en"))), \
             patch.object(UB.activity, "log_command", lambda *a, **k: None):
            await UB.cmd_help(upd, ctx)
        assert "/prayerrequests" in upd.message.reply_text.call_args[0][0]
