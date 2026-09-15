"""Tests for image/document attachments on broadcasts and notifications."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytz

sys.path.insert(0, str(Path(__file__).parent.parent))

import handlers.notifications as hn

TZ = pytz.timezone("America/New_York")




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
        assert hn._is_media_url("https://x/y.jpg")
        assert hn._is_media_url("http://x/y.jpg")
        assert not hn._is_media_url("media/y.jpg")
        assert not hn._is_media_url("FILEID123")
        assert not hn._is_media_url(None)

    def test_looks_like_path(self):
        assert hn._looks_like_path("media/x.jpg")
        assert hn._looks_like_path("x.png")
        assert not hn._looks_like_path("AgACAgIDFILEID")

    def test_resolve_local_media_existing(self, tmp_path):
        f = tmp_path / "poster.jpg"
        f.write_bytes(b"\xff\xd8\xff")
        assert hn._resolve_local_media(str(f)) == f

    def test_resolve_local_media_missing(self):
        assert hn._resolve_local_media("media/does-not-exist-xyz.jpg") is None


# ---------------------------------------------------------------------------
# _send_media
# ---------------------------------------------------------------------------

class TestSendMedia:
    async def test_url_photo_passed_through(self):
        b = _bot()
        fid = await hn._send_media(b, 5, "photo", "https://x/y.jpg", caption="hi")
        b.send_photo.assert_awaited_once()
        assert b.send_photo.await_args[0][1] == "https://x/y.jpg"
        assert fid == "PHOTOID"

    async def test_document_via_file_id(self):
        b = _bot()
        await hn._send_media(b, 5, "document", "SOMEFILEID", caption=None)
        b.send_document.assert_awaited_once()
        assert b.send_document.await_args[0][1] == "SOMEFILEID"

    async def test_local_file_uploaded_and_cached(self, tmp_path):
        f = tmp_path / "flyer.jpg"
        f.write_bytes(b"\xff\xd8\xff\xe0")
        b = _bot()
        cache = {}
        # First send uploads (InputFile), returns + caches file_id.
        fid1 = await hn._send_media(b, 5, "photo", str(f), cache=cache)
        assert fid1 == "PHOTOID"
        assert cache["file_id"] == "PHOTOID"
        # Second send reuses the cached file_id (no re-upload).
        await hn._send_media(b, 6, "photo", str(f), cache=cache)
        second_arg = b.send_photo.await_args_list[1][0][1]
        assert second_arg == "PHOTOID"


# ---------------------------------------------------------------------------
# Notification payload (image/document + caption / overflow)
# ---------------------------------------------------------------------------

class TestNotificationPayload:
    async def test_text_only_when_no_media(self):
        b = _bot()
        await hn._send_notification_payload(b, 9, {}, "hello", {"image": {}, "document": {}})
        b.send_message.assert_awaited_once()
        b.send_photo.assert_not_awaited()

    async def test_image_with_caption(self):
        b = _bot()
        await hn._send_notification_payload(
            b, 9, {"image": "https://x/y.jpg"}, "caption text",
            {"image": {}, "document": {}})
        b.send_photo.assert_awaited_once()
        assert b.send_photo.await_args.kwargs["caption"] == "caption text"
        # Caption fit → no separate text message.
        b.send_message.assert_not_awaited()

    async def test_long_text_sent_separately(self):
        b = _bot()
        long_text = "x" * (hn.CAPTION_LIMIT + 50)
        await hn._send_notification_payload(
            b, 9, {"image": "https://x/y.jpg"}, long_text,
            {"image": {}, "document": {}})
        # Image sent without caption, text as its own message.
        assert b.send_photo.await_args.kwargs["caption"] is None
        b.send_message.assert_awaited_once()

    async def test_image_and_document_caption_on_image_only(self):
        b = _bot()
        await hn._send_notification_payload(
            b, 9, {"image": "https://x/i.jpg", "document": "https://x/d.pdf"},
            "cap", {"image": {}, "document": {}})
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

    async def _deliver(self, event):
        b = _bot()
        state = {"states": {}}

        async def _load():
            return dict(state["states"])

        async def _save(s):
            state["states"] = dict(s)

        with patch("storage._load_notif_state", side_effect=_load), \
             patch("storage._save_notif_state", side_effect=_save):
            sent = await hn.deliver_event_notifications(b, event)
        return b, sent

    async def test_image_posted_to_each_target(self):
        b, sent = await self._deliver(self._event(image="https://x/y.jpg"))
        assert sent == 2
        assert b.send_photo.await_count == 2
        b.send_message.assert_not_awaited()

    async def test_missing_local_file_falls_back_to_text(self):
        b, sent = await self._deliver(self._event(image="media/nope-missing.jpg"))
        # Local file missing → dropped → plain text reminder still sent.
        assert sent == 2
        b.send_photo.assert_not_awaited()
        assert b.send_message.await_count == 2


# ---------------------------------------------------------------------------
# Broadcast media capture + send
