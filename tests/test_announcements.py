"""Tests for the general announcements feature:

  - /announcements (user view: active only, localized, Markdown-safe)
  - /addannouncement admin flow (validation, save, broadcast hand-off)
  - /delannouncement (expires immediately)
  - 30-day purge of expired announcements
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytz

sys.path.insert(0, str(Path(__file__).parent.parent))

import handlers.announcements as han
import handlers.broadcast as hb
import telegram.ext as ext

TZ = pytz.timezone("America/New_York")




def _make_context() -> MagicMock:
    ctx = MagicMock()
    ctx.bot = MagicMock()
    ctx.user_data = {}
    return ctx


def _make_update(text: str = "", chat_id: int = 111) -> MagicMock:
    upd = MagicMock()
    upd.effective_chat.id = chat_id
    upd.effective_user.id = chat_id
    upd.effective_user.username = "admin"
    upd.effective_user.full_name = "Elder Admin"
    upd.effective_user.first_name = "Elder"
    upd.message.text = text
    upd.message.reply_text = AsyncMock()
    return upd


def _ann(days_until_expiry=7, ann_id="ANN1", title="Test Title", body="Test body.",
         created_offset_min=0):
    expires = (datetime.now(TZ) + timedelta(days=days_until_expiry)).strftime("%Y-%m-%d")
    created = (datetime.now(TZ) - timedelta(minutes=created_offset_min)).isoformat()
    return {"id": ann_id, "title": title, "body": body, "created": created,
            "created_by": "Elder Admin", "expires": expires}


# ---------------------------------------------------------------------------
# Lifecycle helpers
# ---------------------------------------------------------------------------

class TestLifecycleHelpers:
    def test_active_until_end_of_expiry_day(self):
        # Expires "today" → still active right now (end-of-day semantics).
        assert han._ann_is_active(_ann(days_until_expiry=0)) is True

    def test_expired_yesterday_inactive(self):
        assert han._ann_is_active(_ann(days_until_expiry=-1)) is False

    def test_bad_date_is_inactive(self):
        assert han._ann_is_active({"expires": "soon"}) is False

    def test_active_sorted_newest_first(self):
        anns = [_ann(ann_id="OLDER", created_offset_min=60),
                _ann(ann_id="NEWER", created_offset_min=1),
                _ann(ann_id="GONE", days_until_expiry=-2)]
        out = han.active_announcements(anns)
        assert [a["id"] for a in out] == ["NEWER", "OLDER"]


class TestPurge:
    async def _run_purge(self, anns):
        saved: dict = {}

        async def _get():
            return anns

        async def _save(a):
            saved["anns"] = a

        with patch("storage.get_announcements", side_effect=_get), \
             patch("storage.save_announcements", side_effect=_save):
            purged = await han.purge_old_announcements()
        return purged, saved

    async def test_long_expired_purged(self):
        purged, saved = await self._run_purge([_ann(days_until_expiry=-45)])
        assert purged == 1
        assert saved["anns"] == []

    async def test_recently_expired_kept(self):
        purged, saved = await self._run_purge([_ann(days_until_expiry=-10)])
        assert purged == 0
        assert "anns" not in saved  # no rewrite when nothing purged

    async def test_bad_date_never_purged(self):
        purged, _ = await self._run_purge([{"id": "BAD", "expires": "garbage"}])
        assert purged == 0


# ---------------------------------------------------------------------------
# /announcements (user view)
# ---------------------------------------------------------------------------

class TestCmdAnnouncements:
    async def _run_cmd(self, anns, lang="en"):
        ctx = _make_context()
        upd = _make_update()

        async def _get_anns():
            return anns

        async def _get_users():
            return [{"chat_id": 111, "language": lang}]

        with patch("storage.get_announcements", side_effect=_get_anns), \
             patch("storage.get_all_users", side_effect=_get_users):
            await han.cmd_announcements(upd, ctx)
        return upd.message.reply_text.call_args[0][0]

    async def test_shows_active(self):
        msg = await self._run_cmd([_ann(title="Roof fund", body="Fully funded!")])
        assert "Roof fund" in msg and "Fully funded!" in msg

    async def test_hides_expired(self):
        msg = await self._run_cmd([_ann(days_until_expiry=-1, title="Old news"),
                             _ann(title="Fresh")])
        assert "Old news" not in msg and "Fresh" in msg

    async def test_none_message(self):
        msg = await self._run_cmd([_ann(days_until_expiry=-1)])
        assert "no announcements" in msg.lower()

    async def test_markdown_preserved_in_the_source_language(self):
        """Admins can format announcements; their own language shows it as typed."""
        msg = await self._run_cmd([_ann(title="Q_A *update*")])
        assert "*update*" in msg
        assert "\\*update\\*" not in msg

    async def test_localized_for_spanish_user(self):
        msg = await self._run_cmd([_ann()], lang="es")
        assert "Anuncios" in msg


# ---------------------------------------------------------------------------
# /addannouncement admin flow
# ---------------------------------------------------------------------------

class TestAddAnnouncement:
    async def test_non_admin_blocked(self):
        ctx = _make_context()
        upd = _make_update()
        with patch("permissions.is_admin", return_value=False):
            result = await han.cmd_addannouncement(upd, ctx)
        assert result == ext.ConversationHandler.END

    async def test_title_too_long_stays(self):
        ctx = _make_context()
        upd = _make_update(text="x" * (han.ANN_TITLE_MAX + 1))
        result = await han.an_title(upd, ctx)
        assert result == han.AN_TITLE

    async def test_bad_expiry_format_stays(self):
        ctx = _make_context()
        ctx.user_data.update({"an_title": "T", "an_body": "B"})
        upd = _make_update(text="tomorrow")
        result = await han.an_expires(upd, ctx)
        assert result == han.AN_EXPIRES

    async def test_past_expiry_stays(self):
        ctx = _make_context()
        ctx.user_data.update({"an_title": "T", "an_body": "B"})
        past = (datetime.now(TZ) - timedelta(days=2)).strftime("%Y-%m-%d")
        upd = _make_update(text=past)
        result = await han.an_expires(upd, ctx)
        assert result == han.AN_EXPIRES

    async def test_today_expiry_accepted(self):
        ctx = _make_context()
        ctx.user_data.update({"an_title": "T", "an_body": "B"})
        today = datetime.now(TZ).strftime("%Y-%m-%d")
        upd = _make_update(text=today)
        result = await han.an_expires(upd, ctx)
        assert result == han.AN_CONFIRM

    async def _run_confirm(self, answer="yes"):
        ctx = _make_context()
        future = (datetime.now(TZ) + timedelta(days=7)).strftime("%Y-%m-%d")
        ctx.user_data.update({"an_title": "New Roof", "an_body": "Details here.",
                              "an_expires": future})
        upd = _make_update(text=answer)
        saved: dict = {}

        async def _get_anns():
            return []

        async def _save_anns(a):
            saved["anns"] = a

        async def _get_users():
            return [{"chat_id": 111}]

        async def _options(bot_, lang):
            return [{"key": "all", "kind": "all", "chat_id": None, "label": "Subscribers"}]

        with patch("storage.get_announcements", side_effect=_get_anns), \
             patch("storage.save_announcements", side_effect=_save_anns), \
             patch("storage.get_all_users", side_effect=_get_users), \
             patch("handlers.announcements._broadcast_target_options", side_effect=_options):
            result = await han.an_confirm(upd, ctx)
        return result, ctx, saved

    async def test_confirm_saves_and_enters_broadcast_selection(self):
        result, ctx, saved = await self._run_confirm()
        assert result == hb.BC_SELECT
        ann = saved["anns"][0]
        assert ann["title"] == "New Roof" and ann["id"]
        # Broadcast hand-off prepared with the rendered announcement.
        assert "New Roof" in ctx.user_data["bc_message"]
        assert ctx.user_data["bc_selected"] == set()

    async def test_decline_discards(self):
        result, ctx, saved = await self._run_confirm(answer="no")
        assert result == ext.ConversationHandler.END
        assert "anns" not in saved


# ---------------------------------------------------------------------------
# /delannouncement
# ---------------------------------------------------------------------------

class TestDelAnnouncement:
    async def test_expires_selected_announcement_now(self):
        ctx = _make_context()
        target = _ann(ann_id="KILL", days_until_expiry=7)
        ctx.user_data["da_anns"] = [target]
        upd = _make_update(text="1")
        saved: dict = {}

        async def _get():
            return [dict(target)]

        async def _save(a):
            saved["anns"] = a

        with patch("storage.get_announcements", side_effect=_get), \
             patch("storage.save_announcements", side_effect=_save):
            result = await han.da_select(upd, ctx)
        assert result == ext.ConversationHandler.END
        assert han._ann_is_active(saved["anns"][0]) is False

    async def test_invalid_number_stays(self):
        ctx = _make_context()
        ctx.user_data["da_anns"] = [_ann()]
        upd = _make_update(text="9")
        result = await han.da_select(upd, ctx)
        assert result == han.DA_SELECT


# ---------------------------------------------------------------------------
# On-the-fly translation (cache-or-translate, failure-safe)
# ---------------------------------------------------------------------------

class TestAnnouncementTranslation:
    async def _run_for_lang(self, ann, lang, translate_result="TRANSLATED", store=None):
        store = store if store is not None else [dict(ann)]
        saved: dict = {}

        async def _get():
            return store

        async def _save(a):
            saved["anns"] = a

        with patch("storage.get_announcements", side_effect=_get), \
             patch("storage.save_announcements", side_effect=_save), \
             patch("translation.translate", return_value=translate_result) as tr:
            out = await han._announcement_for_lang(ann, lang)
        return out, saved, tr

    async def test_same_language_returns_record_untouched(self):
        ann = _ann()
        ann["lang"] = "en"
        out, saved, tr = await self._run_for_lang(ann, "en")
        assert out is ann
        tr.assert_not_called()

    async def test_legacy_record_without_lang_untouched(self):
        ann = _ann()  # no "lang" key
        out, saved, tr = await self._run_for_lang(ann, "fr")
        assert out is ann
        tr.assert_not_called()

    async def test_translates_from_the_source_language_to_the_viewer_s(self):
        """Direction matters: swapping src and dst translates the wrong way and
        would otherwise pass unnoticed — the mock only records that it was called."""
        ann = _ann(title="Sabbath service", body="Bring your Bible")
        ann["lang"] = "en"
        _, _, tr = await self._run_for_lang(ann, "es")
        for call in tr.call_args_list:
            text, src, dst = call.args
            assert src == "en", f"source must be the announcement's language, got {src!r}"
            assert dst == "es", f"target must be the viewer's language, got {dst!r}"

    async def test_translates_both_title_and_body(self):
        ann = _ann(title="Sabbath service", body="Bring your Bible")
        ann["lang"] = "en"
        _, _, tr = await self._run_for_lang(ann, "fr")
        sent = {call.args[0] for call in tr.call_args_list}
        assert sent == {"Sabbath service", "Bring your Bible"}

    async def test_translates_and_caches_on_first_view(self):
        ann = _ann(ann_id="TX1")
        ann["lang"] = "en"
        out, saved, tr = await self._run_for_lang(ann, "es")
        assert out["title"] == "TRANSLATED" and out["body"] == "TRANSLATED"
        assert tr.call_count == 2  # title + body
        cached = saved["anns"][0]["translations"]["es"]
        assert cached == {"title": "TRANSLATED", "body": "TRANSLATED"}

    async def test_cached_translation_skips_translator(self):
        ann = _ann(ann_id="TX2")
        ann["lang"] = "en"
        ann["translations"] = {"es": {"title": "Techo", "body": "Cuerpo"}}
        out, saved, tr = await self._run_for_lang(ann, "es")
        assert out["title"] == "Techo" and out["body"] == "Cuerpo"
        tr.assert_not_called()
        assert "anns" not in saved  # no rewrite for a cache hit

    async def test_translator_failure_falls_back_to_original(self):
        ann = _ann(ann_id="TX3", title="Original", body="Body")
        ann["lang"] = "en"
        out, saved, tr = await self._run_for_lang(ann, "fr", translate_result=None)
        assert out["title"] == "Original" and out["body"] == "Body"
        assert "anns" not in saved  # failures are not cached

    async def test_expiry_metadata_preserved_in_translated_copy(self):
        ann = _ann(ann_id="TX4")
        ann["lang"] = "en"
        out, _, _ = await self._run_for_lang(ann, "zu")
        assert out["expires"] == ann["expires"]
        assert out["id"] == "TX4"


class TestTranslationModule:
    def test_same_language_short_circuits(self):
        import translation
        assert translation.translate("hello", "en", "en") is None

    def test_unknown_language_returns_none(self):
        import translation
        assert translation.translate("hello", "en", "xx") is None

    def test_empty_text_returns_none(self):
        import translation
        assert translation.translate("", "en", "es") is None

    def test_backend_exception_returns_none(self):
        import translation
        with patch("deep_translator.GoogleTranslator", side_effect=RuntimeError("down")):
            assert translation.translate("hello", "en", "es") is None
