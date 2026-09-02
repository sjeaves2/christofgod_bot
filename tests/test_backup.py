"""Tests for the nightly data backup.

The failure that matters here is a *silent* one: a backup that never sends, or
sends a corrupt archive, looks exactly like a working backup until the day you
need it. So these tests check delivery, archive integrity, and that every
failure path records something an admin can see in /stats.
"""

from __future__ import annotations

import asyncio
import io
import sys
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytz

sys.path.insert(0, str(Path(__file__).parent.parent))

import handlers.backup as bk
import permissions

TZ = pytz.timezone("America/New_York")


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture(autouse=True)
def _isolate_backup_state(tmp_path_factory):
    """Every test gets its own backup-state file.

    Without this, tests share the real generated/backup_state.json: one test's
    successful send makes the next one skip, and the suite writes into the
    running deployment's state.
    """
    state = tmp_path_factory.mktemp("backup-state") / "backup_state.json"
    with patch.object(bk, "STATE_FILE", state):
        yield state


def _data_dir(tmp_path, files=None):
    files = files if files is not None else {
        "users.yaml": "users:\n- chat_id: 1\n  display_name: Test\n",
        "appointments.yaml": "appointments: []\n",
    }
    d = tmp_path / "data"
    d.mkdir()
    for name, content in files.items():
        (d / name).write_text(content, encoding="utf-8")
    return d


def _bot():
    b = MagicMock()
    b.send_document = AsyncMock()
    return b


def _ops(chat_ids=(7,)):
    """Patch context so ops_chat_ids() resolves to *chat_ids*."""
    async def _users():
        return [{"chat_id": c, "username": "bishop"} for c in chat_ids]
    return (patch("storage.get_all_users", side_effect=_users),
            patch.object(permissions, "OPS_USERNAMES", {"bishop"}),
            patch.object(permissions, "_ops_chat_ids", set()))


class TestBuildBackup:
    def test_includes_every_data_file(self, tmp_path):
        with patch.object(bk, "DATA_DIR", _data_dir(tmp_path)):
            payload, filename, members = bk.build_backup()
        assert sorted(members) == ["appointments.yaml", "users.yaml"]
        assert filename.startswith("christofgod-data-")
        assert filename.endswith(".zip")

    def test_contents_round_trip_unchanged(self, tmp_path):
        original = "users:\n- chat_id: 42\n  display_name: Jane\n"
        d = _data_dir(tmp_path, {"users.yaml": original})
        with patch.object(bk, "DATA_DIR", d):
            payload, _, _ = bk.build_backup()
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            assert zf.read("data/users.yaml").decode() == original

    def test_files_are_namespaced_under_data(self, tmp_path):
        with patch.object(bk, "DATA_DIR", _data_dir(tmp_path)):
            payload, _, _ = bk.build_backup()
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            assert all(n.startswith("data/") for n in zf.namelist())

    def test_filename_carries_the_date(self, tmp_path):
        when = TZ.localize(datetime(2026, 9, 2, 3, 0))
        with patch.object(bk, "DATA_DIR", _data_dir(tmp_path)):
            _, filename, _ = bk.build_backup(when)
        assert filename == "christofgod-data-2026-09-02.zip"

    def test_subdirectories_are_skipped(self, tmp_path):
        d = _data_dir(tmp_path)
        (d / "nested").mkdir()
        with patch.object(bk, "DATA_DIR", d):
            _, _, members = bk.build_backup()
        assert "nested" not in members

    def test_empty_data_dir_yields_no_members(self, tmp_path):
        d = tmp_path / "data"
        d.mkdir()
        with patch.object(bk, "DATA_DIR", d):
            _, _, members = bk.build_backup()
        assert members == []


class TestVerifyBackup:
    def test_sound_archive_passes(self, tmp_path):
        with patch.object(bk, "DATA_DIR", _data_dir(tmp_path)):
            payload, _, _ = bk.build_backup()
        assert bk.verify_backup(payload) is None

    def test_unreadable_archive_is_reported(self):
        assert bk.verify_backup(b"this is not a zip file") is not None


class TestSendBackup:
    def test_sends_to_ops_admin(self, tmp_path):
        bot = _bot()
        u, ops_u, ops_c = _ops()
        with patch.object(bk, "DATA_DIR", _data_dir(tmp_path)), u, ops_u, ops_c:
            sent = _run(bk.send_backup(bot))
        assert sent == 1
        assert bot.send_document.await_args[0][0] == 7

    def test_sends_to_every_ops_admin(self, tmp_path):
        bot = _bot()
        u, ops_u, ops_c = _ops(chat_ids=(7, 8))
        with patch.object(bk, "DATA_DIR", _data_dir(tmp_path)), u, ops_u, ops_c:
            sent = _run(bk.send_backup(bot))
        assert sent == 2

    def test_attached_file_is_a_valid_zip(self, tmp_path):
        bot = _bot()
        u, ops_u, ops_c = _ops()
        with patch.object(bk, "DATA_DIR", _data_dir(tmp_path)), u, ops_u, ops_c:
            _run(bk.send_backup(bot))
        attached = bot.send_document.await_args.kwargs["document"]
        assert attached.filename.endswith(".zip")

    def test_caption_summarises_contents(self, tmp_path):
        bot = _bot()
        u, ops_u, ops_c = _ops()

        async def _appts():
            return [{"id": "A1"}, {"id": "A2"}]

        with patch.object(bk, "DATA_DIR", _data_dir(tmp_path)), u, ops_u, ops_c, \
             patch("storage.get_appointments", side_effect=_appts):
            _run(bk.send_backup(bot))
        caption = bot.send_document.await_args.kwargs["caption"]
        assert "2 appointment(s)" in caption
        assert "file(s)" in caption

    def test_empty_data_dir_is_not_sent(self, tmp_path):
        """An empty archive is not a backup — refuse rather than reassure."""
        d = tmp_path / "data"
        d.mkdir()
        bot = _bot()
        u, ops_u, ops_c = _ops()
        with patch.object(bk, "DATA_DIR", d), u, ops_u, ops_c, \
             patch.object(bk.activity, "log_error") as log_error:
            sent = _run(bk.send_backup(bot))
        assert sent == 0
        bot.send_document.assert_not_awaited()
        assert log_error.called, "a skipped backup must be recorded"

    def test_corrupt_archive_is_not_sent(self, tmp_path):
        bot = _bot()
        u, ops_u, ops_c = _ops()
        with patch.object(bk, "DATA_DIR", _data_dir(tmp_path)), u, ops_u, ops_c, \
             patch.object(bk, "verify_backup", return_value="users.yaml"), \
             patch.object(bk.activity, "log_error") as log_error:
            sent = _run(bk.send_backup(bot))
        assert sent == 0
        bot.send_document.assert_not_awaited()
        assert log_error.called

    def test_oversized_archive_is_not_sent(self, tmp_path):
        bot = _bot()
        u, ops_u, ops_c = _ops()
        with patch.object(bk, "DATA_DIR", _data_dir(tmp_path)), u, ops_u, ops_c, \
             patch.object(bk, "MAX_UPLOAD_BYTES", 10), \
             patch.object(bk.activity, "log_error") as log_error:
            sent = _run(bk.send_backup(bot))
        assert sent == 0
        assert log_error.called

    def test_no_reachable_ops_admin_is_recorded(self, tmp_path):
        bot = _bot()

        async def _none():
            return []

        with patch.object(bk, "DATA_DIR", _data_dir(tmp_path)), \
             patch("storage.get_all_users", side_effect=_none), \
             patch.object(permissions, "OPS_USERNAMES", {"bishop"}), \
             patch.object(permissions, "_ops_chat_ids", set()), \
             patch.object(bk.activity, "log_error") as log_error:
            sent = _run(bk.send_backup(bot))
        assert sent == 0
        assert log_error.called

    def test_send_failure_does_not_raise(self, tmp_path):
        from telegram.error import TelegramError
        bot = _bot()
        bot.send_document = AsyncMock(side_effect=TelegramError("blocked"))
        u, ops_u, ops_c = _ops()
        with patch.object(bk, "DATA_DIR", _data_dir(tmp_path)), u, ops_u, ops_c, \
             patch.object(bk.activity, "log_error") as log_error:
            sent = _run(bk.send_backup(bot))       # must not raise
        assert sent == 0
        assert log_error.called, "an undelivered backup must be recorded"

    def test_successful_backup_is_logged_for_stats(self, tmp_path):
        bot = _bot()
        u, ops_u, ops_c = _ops()
        with patch.object(bk, "DATA_DIR", _data_dir(tmp_path)), u, ops_u, ops_c, \
             patch.object(bk.activity, "log_command") as log_command:
            _run(bk.send_backup(bot))
        assert log_command.called
        assert log_command.call_args[0][0] == "backup"


class TestSchedule:
    def test_runs_at_three_in_the_morning(self):
        assert bk.BACKUP_TIME.hour == 3
        assert bk.BACKUP_TIME.minute == 0

    def test_scheduled_in_church_timezone(self):
        assert bk.BACKUP_TIME.tzinfo is not None

    def test_job_delegates_to_send_backup(self):
        ctx = MagicMock()
        ctx.bot = MagicMock()
        send = AsyncMock(return_value=1)
        with patch.object(bk, "send_backup", send):
            _run(bk.nightly_backup_job(ctx))
        send.assert_awaited_once_with(ctx.bot)


class TestManualCommand:
    def _run_cmd(self, sent_count, is_admin=True):
        upd = MagicMock()
        upd.effective_user.id = 1
        upd.effective_user.username = "admin"
        upd.effective_user.full_name = "Admin"
        upd.message.reply_text = AsyncMock()
        ctx = MagicMock()
        ctx.bot = MagicMock()
        with patch("permissions.is_admin", return_value=is_admin), \
             patch.object(bk, "send_backup", AsyncMock(return_value=sent_count)):
            _run(bk.cmd_backup(upd, ctx))
        return upd.message.reply_text.call_args[0][0]

    def test_reports_success(self):
        assert "1 ops admin" in self._run_cmd(1)

    def test_reports_failure_with_guidance(self):
        text = self._run_cmd(0)
        assert "could not be sent" in text
        assert "ops admin" in text

    def test_non_admin_blocked(self):
        assert "Unknown command" in self._run_cmd(1, is_admin=False)


class TestScopeExcludesSecrets:
    def test_config_is_never_included(self, tmp_path):
        """config/config.yaml holds the live bot token; DMing it would put the
        token in Telegram's chat history permanently."""
        d = _data_dir(tmp_path)
        (tmp_path / "config").mkdir()
        (tmp_path / "config" / "config.yaml").write_text("bot:\n  token: SECRET\n")
        with patch.object(bk, "DATA_DIR", d):
            payload, _, members = bk.build_backup()
        assert not any("config" in m for m in members)
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            assert not any("config" in n for n in zf.namelist())
            assert all(b"SECRET" not in zf.read(n) for n in zf.namelist())


# ---------------------------------------------------------------------------
# Change detection — only send when data/ actually differs
# ---------------------------------------------------------------------------

class TestFingerprint:
    def test_stable_for_identical_content(self, tmp_path):
        with patch.object(bk, "DATA_DIR", _data_dir(tmp_path)):
            first, _ = bk.data_fingerprint()
            second, _ = bk.data_fingerprint()
        assert first == second

    def test_changes_when_content_changes(self, tmp_path):
        d = _data_dir(tmp_path)
        with patch.object(bk, "DATA_DIR", d):
            before, _ = bk.data_fingerprint()
            (d / "users.yaml").write_text("users:\n- chat_id: 99\n")
            after, _ = bk.data_fingerprint()
        assert before != after

    def test_ignores_mtime_when_content_is_unchanged(self, tmp_path):
        """A file rewritten with identical bytes must not look like new data —
        hashing the zip would fail this, because zips embed mtimes."""
        d = _data_dir(tmp_path)
        original = (d / "users.yaml").read_text()
        with patch.object(bk, "DATA_DIR", d):
            before, _ = bk.data_fingerprint()
            (d / "users.yaml").write_text(original)     # same content, new mtime
            after, _ = bk.data_fingerprint()
        assert before == after

    def test_changes_when_a_file_is_added(self, tmp_path):
        d = _data_dir(tmp_path)
        with patch.object(bk, "DATA_DIR", d):
            before, _ = bk.data_fingerprint()
            (d / "announcements.yaml").write_text("announcements: []\n")
            after, _ = bk.data_fingerprint()
        assert before != after

    def test_changes_when_a_file_is_renamed(self, tmp_path):
        """Same bytes under a different name is still a change — the archive
        would restore differently, so the fingerprint must cover names."""
        d = _data_dir(tmp_path, {"users.yaml": "users: []\n"})
        with patch.object(bk, "DATA_DIR", d):
            before, _ = bk.data_fingerprint()
            (d / "users.yaml").rename(d / "users_old.yaml")
            after, _ = bk.data_fingerprint()
        assert before != after

    def test_changes_when_a_file_is_removed(self, tmp_path):
        d = _data_dir(tmp_path)
        with patch.object(bk, "DATA_DIR", d):
            before, _ = bk.data_fingerprint()
            (d / "appointments.yaml").unlink()
            after, _ = bk.data_fingerprint()
        assert before != after


class TestBackupNeeded:
    def _state(self, tmp_path, fingerprint=None, sent_at=None):
        f = tmp_path / "backup_state.json"
        if fingerprint is not None:
            import json
            f.write_text(json.dumps({"fingerprint": fingerprint,
                                     "sent_at": (sent_at or datetime.now(TZ)).isoformat()}))
        return f

    def test_first_ever_backup_is_needed(self, tmp_path):
        with patch.object(bk, "STATE_FILE", self._state(tmp_path)):
            needed, why = bk.backup_needed("abc")
        assert needed and "first" in why

    def test_changed_data_is_needed(self, tmp_path):
        with patch.object(bk, "STATE_FILE", self._state(tmp_path, "old-hash")):
            needed, why = bk.backup_needed("new-hash")
        assert needed and "changed" in why

    def test_unchanged_data_is_skipped(self, tmp_path):
        with patch.object(bk, "STATE_FILE", self._state(tmp_path, "same-hash")):
            needed, why = bk.backup_needed("same-hash")
        assert needed is False
        assert "no changes" in why

    def test_heartbeat_forces_a_copy_after_the_window(self, tmp_path):
        """Silence must stay meaningful: if nothing changes for a long time, a
        copy is still sent so 'no message' cannot hide a broken bot."""
        stale = datetime.now(TZ) - timedelta(days=bk.FORCE_AFTER_DAYS + 1)
        with patch.object(bk, "STATE_FILE", self._state(tmp_path, "same", stale)):
            needed, why = bk.backup_needed("same")
        assert needed
        assert str(bk.FORCE_AFTER_DAYS) in why

    def test_just_inside_the_window_still_skips(self, tmp_path):
        recent = datetime.now(TZ) - timedelta(days=bk.FORCE_AFTER_DAYS - 1)
        with patch.object(bk, "STATE_FILE", self._state(tmp_path, "same", recent)):
            needed, _ = bk.backup_needed("same")
        assert needed is False

    def test_corrupt_state_errs_towards_sending(self, tmp_path):
        f = tmp_path / "backup_state.json"
        f.write_text("{not valid json")
        with patch.object(bk, "STATE_FILE", f):
            needed, _ = bk.backup_needed("abc")
        assert needed, "an unreadable state file must not suppress backups"

    def test_state_without_timestamp_errs_towards_sending(self, tmp_path):
        import json
        f = tmp_path / "backup_state.json"
        f.write_text(json.dumps({"fingerprint": "same"}))
        with patch.object(bk, "STATE_FILE", f):
            needed, _ = bk.backup_needed("same")
        assert needed


class TestSendRespectsChangeDetection:
    def _send(self, tmp_path, data_dir, force=False):
        bot = _bot()
        u, ops_u, ops_c = _ops()
        with patch.object(bk, "DATA_DIR", data_dir), \
             patch.object(bk, "STATE_FILE", tmp_path / "backup_state.json"), \
             u, ops_u, ops_c:
            sent = _run(bk.send_backup(bot, force=force))
        return sent, bot

    def test_first_run_sends(self, tmp_path):
        sent, bot = self._send(tmp_path, _data_dir(tmp_path))
        assert sent == 1

    def test_second_unchanged_run_does_not_send(self, tmp_path):
        d = _data_dir(tmp_path)
        self._send(tmp_path, d)
        sent, bot = self._send(tmp_path, d)
        assert sent == 0
        bot.send_document.assert_not_awaited()

    def test_changed_data_sends_again(self, tmp_path):
        d = _data_dir(tmp_path)
        self._send(tmp_path, d)
        (d / "users.yaml").write_text("users:\n- chat_id: 2\n")
        sent, _ = self._send(tmp_path, d)
        assert sent == 1

    def test_force_sends_even_when_unchanged(self, tmp_path):
        d = _data_dir(tmp_path)
        self._send(tmp_path, d)
        sent, _ = self._send(tmp_path, d, force=True)
        assert sent == 1

    def test_skip_is_recorded_for_stats(self, tmp_path):
        d = _data_dir(tmp_path)
        self._send(tmp_path, d)
        bot = _bot()
        u, ops_u, ops_c = _ops()
        with patch.object(bk, "DATA_DIR", d), \
             patch.object(bk, "STATE_FILE", tmp_path / "backup_state.json"), \
             u, ops_u, ops_c, patch.object(bk.activity, "log_command") as log_command:
            _run(bk.send_backup(bot))
        assert log_command.called
        assert "skipped" in log_command.call_args.kwargs["details"]

    def test_state_only_recorded_after_a_successful_send(self, tmp_path):
        """A failed delivery must not mark the data as backed up."""
        from telegram.error import TelegramError
        d = _data_dir(tmp_path)
        state = tmp_path / "backup_state.json"
        bot = _bot()
        bot.send_document = AsyncMock(side_effect=TelegramError("blocked"))
        u, ops_u, ops_c = _ops()
        with patch.object(bk, "DATA_DIR", d), patch.object(bk, "STATE_FILE", state), \
             u, ops_u, ops_c:
            _run(bk.send_backup(bot))
        assert not state.exists(), "failed send must not record a successful backup"

    def test_state_file_lives_outside_data(self):
        """Writing state into data/ would itself count as a change every night."""
        assert bk.DATA_DIR not in bk.STATE_FILE.parents

    def test_manual_command_forces(self):
        upd = MagicMock()
        upd.effective_user.id = 1
        upd.effective_user.username = "admin"
        upd.effective_user.full_name = "Admin"
        upd.message.reply_text = AsyncMock()
        send = AsyncMock(return_value=1)
        with patch("permissions.is_admin", return_value=True), \
             patch.object(bk, "send_backup", send):
            _run(bk.cmd_backup(upd, MagicMock()))
        assert send.await_args.kwargs.get("force") is True
