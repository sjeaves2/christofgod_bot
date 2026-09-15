"""Announcement formatting, the optional media step, and /broadcast's removal.

/broadcast was retired: /addannouncement does the same push plus a stored,
translatable, expiring record. The delivery ENGINE (target selection, send,
retry) survives and is what pushes announcements out — only the entry point and
its text/media capture steps were removed.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from telegram.error import BadRequest

sys.path.insert(0, str(Path(__file__).parent.parent))

import handlers.announcements as A  # noqa: E402




def _ctx(**user_data):
    ctx = MagicMock()
    ctx.user_data = dict(user_data)
    return ctx


def _update(text=None, photo=None, document=None):
    upd = MagicMock()
    upd.message.text = text
    upd.message.photo = photo
    upd.message.document = document
    upd.message.reply_text = AsyncMock()
    upd.effective_user.id = 7
    upd.effective_user.username = "admin"
    upd.effective_user.full_name = "Admin User"
    return upd


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

class TestRenderAnnouncement:
    def test_escape_false_passes_markdown_through(self):
        out = A._render_announcement({"title": "T", "body": "Bring *your Bible*"},
                                     escape=False)
        assert "*your Bible*" in out
        assert "\\*" not in out

    def test_escape_true_neutralises_markdown(self):
        out = A._render_announcement({"title": "T", "body": "Bring *your Bible*"},
                                     escape=True)
        assert "\\*your Bible\\*" in out

    def test_escape_defaults_to_true(self):
        """The safe default: callers must opt in to trusting the markup."""
        assert A._render_announcement({"title": "T", "body": "*x*"}) == \
            A._render_announcement({"title": "T", "body": "*x*"}, escape=True)

    def test_underscores_survive_when_unescaped(self):
        out = A._render_announcement({"title": "T", "body": "_emphasis_"}, escape=False)
        assert "_emphasis_" in out


# ---------------------------------------------------------------------------
# Markdown validation at entry
# ---------------------------------------------------------------------------

class TestBodyValidatesMarkdown:
    @staticmethod
    async def _submit(body, markdown_ok=True):
        ctx = _ctx(an_title="Notice")
        upd = _update(text=body)

        async def reply(text, **kwargs):
            if kwargs.get("parse_mode") is not None and not markdown_ok:
                raise BadRequest("can't parse entities")

        upd.message.reply_text = AsyncMock(side_effect=reply)
        return await A.an_body(upd, ctx), ctx, upd

    async def test_valid_markdown_advances_to_the_media_step(self):
        state, ctx, _ = await self._submit("Bring *your Bible*")
        assert state == A.AN_MEDIA
        assert ctx.user_data["an_body"] == "Bring *your Bible*"

    async def test_unbalanced_markdown_is_rejected_at_entry(self):
        """One stray marker makes Telegram reject the WHOLE message — catch it
        while the admin is still typing, not mid-push."""
        state, ctx, _ = await self._submit("Bring *your Bible", markdown_ok=False)
        assert state == A.AN_BODY
        assert "an_body" not in ctx.user_data

    async def test_rejection_explains_how_to_fix_it(self):
        _, _, upd = await self._submit("bad *markup", markdown_ok=False)
        said = " ".join(str(c.args[0]) for c in upd.message.reply_text.call_args_list)
        assert "matching pair" in said          # how to fix it
        assert "parse entities" in said         # what Telegram actually objected to

    async def test_overlong_body_still_rejected(self):
        state, ctx, _ = await self._submit("x" * (A.ANN_BODY_MAX + 1))
        assert state == A.AN_BODY
        assert "an_body" not in ctx.user_data


# ---------------------------------------------------------------------------
# Optional media step
# ---------------------------------------------------------------------------

class TestAnnouncementMedia:
    @staticmethod
    def _ctx_ready(body="Short body"):
        return _ctx(an_title="Notice", an_body=body)

    async def test_photo_is_captured(self):
        ctx = self._ctx_ready()
        upd = _update(photo=[MagicMock(file_id="PIC1")])
        assert await A.an_media(upd, ctx) == A.AN_EXPIRES
        assert ctx.user_data["an_media"] == {"kind": "photo", "file_id": "PIC1"}

    async def test_document_is_captured(self):
        ctx = self._ctx_ready()
        upd = _update(document=MagicMock(file_id="DOC1"))
        assert await A.an_media(upd, ctx) == A.AN_EXPIRES
        assert ctx.user_data["an_media"] == {"kind": "document", "file_id": "DOC1"}

    async def test_skip_leaves_it_text_only(self):
        ctx = self._ctx_ready()
        ctx.user_data["an_media"] = {"kind": "photo", "file_id": "OLD"}
        upd = _update(text="/skip")
        assert await A.an_skip_media(upd, ctx) == A.AN_EXPIRES
        assert "an_media" not in ctx.user_data

    async def test_plain_text_at_the_media_step_reprompts(self):
        ctx = self._ctx_ready()
        upd = _update(text="not a photo")
        assert await A.an_media(upd, ctx) == A.AN_MEDIA
        assert "an_media" not in ctx.user_data

    async def test_body_too_long_for_a_caption_is_refused(self):
        """Media carries the text as a caption; Telegram caps that length.
        Refuse rather than silently truncating the announcement."""
        ctx = self._ctx_ready(body="x" * (A.CAPTION_LIMIT + 50))
        upd = _update(photo=[MagicMock(file_id="PIC1")])
        assert await A.an_media(upd, ctx) == A.AN_MEDIA
        assert "an_media" not in ctx.user_data
        said = str(upd.message.reply_text.call_args[0][0])
        assert str(A.CAPTION_LIMIT) in said


# ---------------------------------------------------------------------------
# Hand-off to the delivery engine
# ---------------------------------------------------------------------------

class TestConfirmHandsOffToDelivery:
    @staticmethod
    async def _confirm(user_data):
        ctx = _ctx(**user_data)
        upd = _update(text="yes")

        async def _opts(_bot, _lang=None):
            return [{"key": "all", "kind": "all", "chat_id": None, "label": "All"}]

        with patch.object(A.storage, "get_announcements", AsyncMock(return_value=[])), \
             patch.object(A.storage, "save_announcements", AsyncMock()), \
             patch.object(A, "get_user_prefs", AsyncMock(return_value=(None, "en"))), \
             patch.object(A, "_broadcast_target_options", side_effect=_opts), \
             patch.object(A.activity, "log_command", lambda *a, **k: None):
            await A.an_confirm(upd, ctx)
        return ctx

    _BASE = {"an_title": "Notice", "an_body": "Bring *your Bible*",
             "an_expires": "2099-01-01"}

    async def test_text_announcement_keeps_the_admins_formatting(self):
        ctx = await self._confirm(dict(self._BASE))
        assert "*your Bible*" in ctx.user_data["bc_message"]
        assert "\\*" not in ctx.user_data["bc_message"]

    async def test_text_announcement_carries_the_sender_attribution(self):
        ctx = await self._confirm(dict(self._BASE))
        assert "posted by" in ctx.user_data["bc_message"]

    async def test_text_announcement_sets_no_media(self):
        ctx = await self._confirm(dict(self._BASE))
        assert "bc_media" not in ctx.user_data

    async def test_media_announcement_is_handed_to_the_engine(self):
        ctx = await self._confirm(
            dict(self._BASE, an_media={"kind": "photo", "file_id": "PIC1"}))
        assert ctx.user_data["bc_media"]["file_id"] == "PIC1"
        assert ctx.user_data["bc_media"]["kind"] == "photo"
        assert "*your Bible*" in ctx.user_data["bc_media"]["caption"]

    async def test_media_announcement_sends_no_separate_text(self):
        """Telegram has no 'photo plus separate message' single send."""
        ctx = await self._confirm(
            dict(self._BASE, an_media={"kind": "photo", "file_id": "PIC1"}))
        assert "bc_message" not in ctx.user_data


# ---------------------------------------------------------------------------
# /broadcast is gone, but the engine it shared is not
# ---------------------------------------------------------------------------

class TestBroadcastRetired:
    def test_broadcast_command_is_not_registered(self):
        import bot
        app = MagicMock()
        registered: dict = {}

        def add_handler(handler, group=0):
            registered.setdefault(group, []).append(handler)

        app.add_handler.side_effect = add_handler
        builder = MagicMock()
        builder.token.return_value = builder
        builder.post_init.return_value = builder
        builder.build.return_value = app
        with patch.object(bot, "Application") as App:
            App.builder.return_value = builder
            bot.main()

        commands = set()
        from telegram.ext import CommandHandler, ConversationHandler
        for handlers in registered.values():
            for h in handlers:
                if isinstance(h, CommandHandler):
                    commands |= set(h.commands)
                elif isinstance(h, ConversationHandler):
                    for entry in h.entry_points:
                        if isinstance(entry, CommandHandler):
                            commands |= set(entry.commands)
        assert "broadcast" not in commands
        # The replacement must still be there.
        assert "addannouncement" in commands

    def test_broadcast_entry_points_are_gone_from_the_module(self):
        import handlers.broadcast as hb
        for name in ("cmd_broadcast", "bc_message", "bc_media", "BC_MESSAGE"):
            assert not hasattr(hb, name), f"{name} should have been removed"

    def test_the_delivery_engine_survives(self):
        """Announcements depend on all of this — removing it would break them."""
        import handlers.broadcast as hb
        for name in ("bc_select", "bc_retry", "_broadcast_target_options",
                     "_bc_keyboard", "_append_sender", "_bc_send_pending",
                     "BC_SELECT", "BC_RETRY", "CB_BC_PREFIX"):
            assert hasattr(hb, name), f"{name} is still needed by announcements"

    def test_admin_help_no_longer_advertises_broadcast(self):
        """Where the admin command list actually lives — a hardcoded constant,
        not the locale files (locales never mentioned /broadcast at all)."""
        from handlers.user_basics import ADMIN_COMMANDS_TEXT
        assert "/broadcast" not in ADMIN_COMMANDS_TEXT
        assert "/addannouncement" in ADMIN_COMMANDS_TEXT


# ---------------------------------------------------------------------------
# Viewing: formatting in the source language, escaped once translated
# ---------------------------------------------------------------------------

class TestViewEscapesOnlyTranslations:
    @staticmethod
    async def _view(ann, viewer_lang, translated_body=None):
        """Render /announcements for a viewer and return the message sent."""
        upd = _update(text="/announcements")
        upd.message.reply_text = AsyncMock()
        ctx = _ctx()

        async def _for_lang(a, lang):
            if translated_body is not None and a.get("lang") != lang:
                return {**a, "body": translated_body}
            return a

        with patch.object(A.storage, "get_announcements", AsyncMock(return_value=[ann])), \
             patch.object(A, "get_user_prefs", AsyncMock(return_value=(None, viewer_lang))), \
             patch.object(A, "_announcement_for_lang", _for_lang), \
             patch.object(A.activity, "log_command", lambda *a, **k: None), \
             patch.object(A, "t", lambda key, lang, **kw: key):
            await A.cmd_announcements(upd, ctx)
        return upd.message.reply_text.call_args[0][0]

    _ANN = {"id": "A1", "title": "Notice", "body": "Bring *your Bible*",
            "lang": "en", "expires": "2099-01-01", "created": "2026-01-01"}

    async def test_source_language_viewer_sees_real_formatting(self):
        msg = await self._view(dict(self._ANN), "en")
        assert "*your Bible*" in msg
        assert "\\*" not in msg

    async def test_translated_viewer_gets_escaped_markup(self):
        """A translator treats * as punctuation and may move or drop it, so the
        markers in a translated copy can no longer be trusted."""
        msg = await self._view(dict(self._ANN), "es",
                         translated_body="Traiga *su Biblia")  # unbalanced!
        assert "\\*su Biblia" in msg
        assert "Traiga \\*su Biblia" in msg

    async def test_legacy_record_without_a_source_lang_is_trusted(self):
        """No 'lang' means it was never translated, so it is the admin's own text."""
        ann = {k: v for k, v in self._ANN.items() if k != "lang"}
        msg = await self._view(ann, "es")
        assert "*your Bible*" in msg

    async def test_unparseable_markup_falls_back_to_an_escaped_send(self):
        """An older record whose literal asterisks are now read as markup must
        not stop the whole announcement list from being shown."""
        upd = _update(text="/announcements")
        calls = []

        async def reply(text, **kwargs):
            calls.append(text)
            if len(calls) == 1:
                raise BadRequest("can't parse entities")

        upd.message.reply_text = AsyncMock(side_effect=reply)
        ctx = _ctx()
        ann = dict(self._ANN, body="Unbalanced *markup")

        async def _for_lang(a, lang):
            return a

        with patch.object(A.storage, "get_announcements", AsyncMock(return_value=[ann])), \
             patch.object(A, "get_user_prefs", AsyncMock(return_value=(None, "en"))), \
             patch.object(A, "_announcement_for_lang", _for_lang), \
             patch.object(A.activity, "log_command", lambda *a, **k: None), \
             patch.object(A, "t", lambda key, lang, **kw: key):
            await A.cmd_announcements(upd, ctx)

        assert len(calls) == 2, "should retry with everything escaped"
        assert "\\*markup" in calls[1]
