"""Tests for image/document attachments on broadcasts and notifications."""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytz
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

TZ = pytz.timezone("America/New_York")


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _bot():
    b = MagicMock()
    # send_photo/document return a message exposing a file_id.
    photo_msg = MagicMock()
    photo_msg.photo = [MagicMock(file_id="PHOTOID")]
    doc_msg = MagicMock()
    doc_msg.document = MagicMock(file_id="DOCID")
    b.send_photo = AsyncMock(return_value=photo_msg)
    b.send_document = AsyncMock(return_value=doc_msg)
    b.send_message = AsyncMock()
    return b


# ---------------------------------------------------------------------------
# Source detection & resolution helpers
# ---------------------------------------------------------------------------

class TestMediaHelpers:
    def test_url_detection(self):
        import bot
        assert bot._is_media_url("https://x/y.jpg")
        assert bot._is_media_url("http://x/y.jpg")
        assert not bot._is_media_url("media/y.jpg")
        assert not bot._is_media_url("FILEID123")
        assert not bot._is_media_url(None)

    def test_looks_like_path(self):
        import bot
        assert bot._looks_like_path("media/x.jpg")
        assert bot._looks_like_path("x.png")
        assert not bot._looks_like_path("AgACAgIDFILEID")

    def test_resolve_local_media_existing(self, tmp_path):
        import bot
        f = tmp_path / "poster.jpg"
        f.write_bytes(b"\xff\xd8\xff")
        assert bot._resolve_local_media(str(f)) == f

    def test_resolve_local_media_missing(self):
        import bot
        assert bot._resolve_local_media("media/does-not-exist-xyz.jpg") is None


# ---------------------------------------------------------------------------
# _send_media
# ---------------------------------------------------------------------------

class TestSendMedia:
    def test_url_photo_passed_through(self):
        import bot
        b = _bot()
        fid = _run(bot._send_media(b, 5, "photo", "https://x/y.jpg", caption="hi"))
        b.send_photo.assert_awaited_once()
        assert b.send_photo.await_args[0][1] == "https://x/y.jpg"
        assert fid == "PHOTOID"

    def test_document_via_file_id(self):
        import bot
        b = _bot()
        _run(bot._send_media(b, 5, "document", "SOMEFILEID", caption=None))
        b.send_document.assert_awaited_once()
        assert b.send_document.await_args[0][1] == "SOMEFILEID"

    def test_local_file_uploaded_and_cached(self, tmp_path):
        import bot
        f = tmp_path / "flyer.jpg"
        f.write_bytes(b"\xff\xd8\xff\xe0")
        b = _bot()
        cache = {}
        # First send uploads (InputFile), returns + caches file_id.
        fid1 = _run(bot._send_media(b, 5, "photo", str(f), cache=cache))
        assert fid1 == "PHOTOID"
        assert cache["file_id"] == "PHOTOID"
        # Second send reuses the cached file_id (no re-upload).
        _run(bot._send_media(b, 6, "photo", str(f), cache=cache))
        second_arg = b.send_photo.await_args_list[1][0][1]
        assert second_arg == "PHOTOID"


# ---------------------------------------------------------------------------
# Notification payload (image/document + caption / overflow)
# ---------------------------------------------------------------------------

class TestNotificationPayload:
    def test_text_only_when_no_media(self):
        import bot
        b = _bot()
        _run(bot._send_notification_payload(b, 9, {}, "hello", {"image": {}, "document": {}}))
        b.send_message.assert_awaited_once()
        b.send_photo.assert_not_awaited()

    def test_image_with_caption(self):
        import bot
        b = _bot()
        _run(bot._send_notification_payload(
            b, 9, {"image": "https://x/y.jpg"}, "caption text",
            {"image": {}, "document": {}}))
        b.send_photo.assert_awaited_once()
        assert b.send_photo.await_args.kwargs["caption"] == "caption text"
        # Caption fit → no separate text message.
        b.send_message.assert_not_awaited()

    def test_long_text_sent_separately(self):
        import bot
        b = _bot()
        long_text = "x" * (bot.CAPTION_LIMIT + 50)
        _run(bot._send_notification_payload(
            b, 9, {"image": "https://x/y.jpg"}, long_text,
            {"image": {}, "document": {}}))
        # Image sent without caption, text as its own message.
        assert b.send_photo.await_args.kwargs["caption"] is None
        b.send_message.assert_awaited_once()

    def test_image_and_document_caption_on_image_only(self):
        import bot
        b = _bot()
        _run(bot._send_notification_payload(
            b, 9, {"image": "https://x/i.jpg", "document": "https://x/d.pdf"},
            "cap", {"image": {}, "document": {}}))
        assert b.send_photo.await_args.kwargs["caption"] == "cap"
        assert b.send_document.await_args.kwargs["caption"] is None


# ---------------------------------------------------------------------------
# deliver_event_notifications with media
# ---------------------------------------------------------------------------

class TestDeliverWithMedia:
    def _event(self, **extra):
        svc = datetime.now(TZ) + timedelta(hours=2)
        ev = {
            "key": "ev1", "name": "Service",
            "service_time": svc, "notification_time": svc - timedelta(hours=1),
            "target_chat_ids": [-100, -200], "announcements": [],
        }
        ev.update(extra)
        return ev

    def _deliver(self, event):
        import bot
        b = _bot()
        state = {"states": {}}

        async def _load():
            return dict(state["states"])

        async def _save(s):
            state["states"] = dict(s)

        with patch("bot._load_notif_state", side_effect=_load), \
             patch("bot._save_notif_state", side_effect=_save):
            sent = _run(bot.deliver_event_notifications(b, event))
        return b, sent

    def test_image_posted_to_each_target(self):
        b, sent = self._deliver(self._event(image="https://x/y.jpg"))
        assert sent == 2
        assert b.send_photo.await_count == 2
        b.send_message.assert_not_awaited()

    def test_missing_local_file_falls_back_to_text(self):
        b, sent = self._deliver(self._event(image="media/nope-missing.jpg"))
        # Local file missing → dropped → plain text reminder still sent.
        assert sent == 2
        b.send_photo.assert_not_awaited()
        assert b.send_message.await_count == 2


# ---------------------------------------------------------------------------
# Broadcast media capture + send
# ---------------------------------------------------------------------------

class TestBroadcastMedia:
    def _ctx(self):
        ctx = MagicMock()
        ctx.user_data = {}
        ctx.bot = _bot()
        return ctx

    def _photo_update(self, caption=None):
        upd = MagicMock()
        m = upd.message
        m.chat_id = 1
        m.photo = [MagicMock(file_id="PIC1")]
        m.document = None
        m.caption = caption
        m.reply_text = AsyncMock()
        return upd

    def test_photo_captured_into_bc_media(self):
        import bot
        ctx = self._ctx()
        upd = self._photo_update(caption="Hello *world*")
        with patch("bot._broadcast_target_options", return_value=[]):
            result = _run(bot.bc_media(upd, ctx))
        assert result == bot.BC_SELECT
        assert ctx.user_data["bc_media"] == {"kind": "photo", "file_id": "PIC1",
                                             "caption": "Hello *world*"}
        assert "bc_message" not in ctx.user_data

    def test_bad_caption_markdown_stays(self):
        import bot
        from telegram.error import BadRequest
        ctx = self._ctx()
        ctx.bot.send_photo = AsyncMock(side_effect=BadRequest("can't parse entities"))
        upd = self._photo_update(caption="bad *markdown")
        result = _run(bot.bc_media(upd, ctx))
        assert result == bot.BC_MESSAGE
        assert "bc_media" not in ctx.user_data

    def test_send_pending_uses_media(self):
        import bot
        ctx = self._ctx()
        ctx.user_data.update({
            "bc_media": {"kind": "photo", "file_id": "PIC1", "caption": "cap"},
            "bc_recipients": [{"chat_id": 10, "label": "A"}, {"chat_id": 20, "label": "B"}],
            "bc_done": set(),
        })
        failures = _run(bot._bc_send_pending(ctx.bot, ctx))
        assert failures == []
        assert ctx.bot.send_photo.await_count == 2
        ctx.bot.send_message.assert_not_awaited()
