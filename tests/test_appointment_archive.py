"""Tests for archiving long-past appointments out of the live file."""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytz

sys.path.insert(0, str(Path(__file__).parent.parent))

TZ = pytz.timezone("America/New_York")


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _appt(days_ago, status="confirmed", appt_id="A1"):
    dt = (datetime.now(TZ) - timedelta(days=days_ago)).replace(microsecond=0)
    return {
        "id": appt_id, "user_chat_id": 111, "official_id": "off1",
        "official_name": "Pastor Test", "user_display_name": "John Doe",
        "requested_datetime": dt.isoformat(),
        "confirmed_datetime": dt.isoformat() if status == "confirmed" else None,
        "status": status, "duration_minutes": 30, "description": "x",
    }


def _run_archive(appts, existing_archive=None):
    import bot
    saved: dict = {}
    archive_data = {"appointments": list(existing_archive or [])}

    async def _get():
        return appts

    async def _save(a):
        saved["live"] = a

    async def _arch_get():
        return archive_data

    async def _arch_save(d):
        saved["archive"] = d

    with patch("bot.get_appointments", side_effect=_get), \
         patch("bot.save_appointments", side_effect=_save), \
         patch.object(bot.appts_archive_cache, "get", side_effect=_arch_get), \
         patch.object(bot.appts_archive_cache, "save", side_effect=_arch_save):
        moved = _run(bot.archive_old_appointments())
    return moved, saved


class TestArchive:
    def test_old_appointment_moved(self):
        moved, saved = _run_archive([_appt(days_ago=120)])
        assert moved == 1
        assert saved["live"] == []
        assert saved["archive"]["appointments"][0]["id"] == "A1"

    def test_recent_appointment_kept(self):
        moved, saved = _run_archive([_appt(days_ago=30)])
        assert moved == 0
        assert "live" not in saved and "archive" not in saved

    def test_boundary_just_under_window_kept(self):
        moved, _ = _run_archive([_appt(days_ago=89)])
        assert moved == 0

    def test_future_appointment_kept_any_status(self):
        moved, _ = _run_archive([_appt(days_ago=-5, status="cancelled")])
        assert moved == 0

    def test_old_pending_and_cancelled_also_archived(self):
        appts = [_appt(days_ago=120, status="pending", appt_id="P1"),
                 _appt(days_ago=120, status="cancelled", appt_id="C1"),
                 _appt(days_ago=10, appt_id="KEEP")]
        moved, saved = _run_archive(appts)
        assert moved == 2
        assert [a["id"] for a in saved["live"]] == ["KEEP"]
        assert {a["id"] for a in saved["archive"]["appointments"]} == {"P1", "C1"}

    def test_archive_appends_not_overwrites(self):
        prior = [_appt(days_ago=400, appt_id="OLD_ARCHIVED")]
        moved, saved = _run_archive([_appt(days_ago=120, appt_id="NEW")],
                                    existing_archive=prior)
        ids = [a["id"] for a in saved["archive"]["appointments"]]
        assert ids == ["OLD_ARCHIVED", "NEW"]

    def test_unparseable_date_kept_live(self):
        bad = _appt(days_ago=120, appt_id="BAD")
        bad["requested_datetime"] = "not-a-date"
        bad["confirmed_datetime"] = None
        moved, saved = _run_archive([bad])
        assert moved == 0


class TestRetentionPurge:
    def _run_purge(self, archive):
        import bot
        data = {"appointments": list(archive)}
        saved: dict = {}

        async def _get():
            return data

        async def _save(d):
            saved["archive"] = d

        with patch.object(bot.appts_archive_cache, "get", side_effect=_get), \
             patch.object(bot.appts_archive_cache, "save", side_effect=_save):
            purged = _run(bot.purge_archived_appointments())
        return purged, saved

    def test_ancient_record_purged(self):
        purged, saved = self._run_purge([_appt(days_ago=800, appt_id="OLD")])
        assert purged == 1
        assert saved["archive"]["appointments"] == []

    def test_within_retention_kept(self):
        purged, saved = self._run_purge([_appt(days_ago=400, appt_id="KEEP")])
        assert purged == 0
        assert "archive" not in saved  # no rewrite when nothing purged

    def test_mixed_purges_only_ancient(self):
        purged, saved = self._run_purge([
            _appt(days_ago=800, appt_id="OLD"),
            _appt(days_ago=100, appt_id="RECENT"),
        ])
        assert purged == 1
        assert [a["id"] for a in saved["archive"]["appointments"]] == ["RECENT"]

    def test_unparseable_date_never_purged(self):
        bad = _appt(days_ago=800, appt_id="BAD")
        bad["requested_datetime"] = "garbage"
        bad["confirmed_datetime"] = None
        purged, saved = self._run_purge([bad])
        assert purged == 0

    def test_job_runs_archive_then_purge(self):
        import bot
        from unittest.mock import AsyncMock, MagicMock
        arch = AsyncMock(return_value=0)
        purge = AsyncMock(return_value=0)
        with patch("bot.archive_old_appointments", arch), \
             patch("bot.purge_archived_appointments", purge):
            _run(bot.appointment_archive_job(MagicMock()))
        arch.assert_awaited_once()
        purge.assert_awaited_once()
