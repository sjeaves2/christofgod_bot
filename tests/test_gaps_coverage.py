"""Tests closing gaps found by mutation testing (2026-07-31).

Each class here targets a mutation that previously survived the suite:
  1. permissions.is_admin ignoring the phone-registered chat_id set,
  2. events.all_upcoming not sorting its merged result,
  3. the real storage accessors (always patched elsewhere, so never exercised).

These call the real functions rather than reimplementing their logic — the
earlier phone tests simulated the matching inline, which is why the defect
slipped through.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytz

sys.path.insert(0, str(Path(__file__).parent.parent))

TZ = pytz.timezone("America/New_York")


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _update(user_id: int, username: "str | None" = None) -> MagicMock:
    upd = MagicMock()
    upd.effective_user.id = user_id
    upd.effective_user.username = username
    return upd


# ---------------------------------------------------------------------------
# 1. Phone-registered admins are recognised by is_admin
# ---------------------------------------------------------------------------

class TestPhoneAdminRecognition:
    def test_phone_registration_grants_admin(self):
        import permissions
        with patch.object(permissions, "ADMIN_PHONES", {"15550001234"}), \
             patch.object(permissions, "ADMIN_USERNAMES", set()), \
             patch.object(permissions, "_admin_chat_ids", set()):
            upd = _update(4242)
            assert permissions.is_admin(upd) is False        # not yet known
            _run(permissions._register_admin_by_phone(4242, "+1 (555) 000-1234"))
            assert permissions.is_admin(upd) is True         # recognised after sharing

    def test_non_admin_phone_does_not_grant_admin(self):
        import permissions
        with patch.object(permissions, "ADMIN_PHONES", {"15550001234"}), \
             patch.object(permissions, "ADMIN_USERNAMES", set()), \
             patch.object(permissions, "_admin_chat_ids", set()):
            _run(permissions._register_admin_by_phone(4242, "+1 (555) 999-0000"))
            assert permissions.is_admin(_update(4242)) is False

    def test_registration_is_per_user(self):
        import permissions
        with patch.object(permissions, "ADMIN_PHONES", {"15550001234"}), \
             patch.object(permissions, "ADMIN_USERNAMES", set()), \
             patch.object(permissions, "_admin_chat_ids", set()):
            _run(permissions._register_admin_by_phone(1, "+15550001234"))
            assert permissions.is_admin(_update(1)) is True
            assert permissions.is_admin(_update(2)) is False

    def test_username_admin_still_recognised(self):
        import permissions
        with patch.object(permissions, "ADMIN_PHONES", set()), \
             patch.object(permissions, "ADMIN_USERNAMES", {"alice"}), \
             patch.object(permissions, "_admin_chat_ids", set()):
            assert permissions.is_admin(_update(7, "Alice")) is True
            assert permissions.is_admin(_update(8, "mallory")) is False

    def test_username_registration_grants_admin(self):
        import permissions
        with patch.object(permissions, "ADMIN_PHONES", set()), \
             patch.object(permissions, "ADMIN_USERNAMES", {"alice"}), \
             patch.object(permissions, "_admin_chat_ids", set()):
            _run(permissions._register_admin_by_username(55, "alice"))
            # Recognised by chat_id even if the username is later cleared.
            assert permissions.is_admin(_update(55, None)) is True


# ---------------------------------------------------------------------------
# 2. all_upcoming returns events sorted by service_time
# ---------------------------------------------------------------------------

class TestAllUpcomingOrdering:
    def _events_data(self):
        """Three special events defined out of chronological order."""
        base = datetime.now(TZ) + timedelta(days=2)
        def spec(idx, days):
            d = (base + timedelta(days=days))
            return {"id": f"ev{idx}", "name": f"Event {idx}", "type": "once",
                    "date": d.strftime("%Y-%m-%d"), "time": "10:00",
                    "duration_minutes": 60, "notification_minutes": 60,
                    "description": "", "url": "", "active": True, "targets": []}
        return {"convocation_urls": {}, "convocation_announcements": {},
                "convocation_images": {}, "convocation_documents": {},
                "notification_targets": {}, "convocation_targets": {},
                "convocation_targets_default": [],
                "special_events": [spec(3, 20), spec(1, 1), spec(2, 10)]}

    def _run_all_upcoming(self):
        import events

        async def _fake_data():
            return self._events_data()

        with patch("storage.get_all_events_data", side_effect=_fake_data), \
             patch("events.all_upcoming_events", return_value=[]):
            return _run(events.all_upcoming(days_ahead=60))

    def test_result_is_sorted_by_service_time(self):
        evs = self._run_all_upcoming()
        times = [e["service_time"] for e in evs]
        assert times == sorted(times), "all_upcoming must return events in chronological order"

    def test_specific_order_of_out_of_order_input(self):
        evs = self._run_all_upcoming()
        assert [e["name"] for e in evs] == ["Event 1", "Event 2", "Event 3"]

    def test_convocations_and_specials_interleave_chronologically(self):
        import events
        soon = datetime.now(TZ) + timedelta(days=5, hours=3)
        convo = {"key": "c1", "phase_key": "sabbath::Eve", "name": "Convocation",
                 "service_time": soon, "notification_time": soon - timedelta(hours=1),
                 "type": "convocation", "announcements": []}

        async def _fake_data():
            return self._events_data()

        with patch("storage.get_all_events_data", side_effect=_fake_data), \
             patch("events.all_upcoming_events", return_value=[convo]):
            evs = _run(events.all_upcoming(days_ahead=60))
        times = [e["service_time"] for e in evs]
        assert times == sorted(times)
        # The convocation (day 7) falls between Event 1 (day 3) and Event 2 (day 12).
        assert [e["name"] for e in evs] == ["Event 1", "Convocation", "Event 2", "Event 3"]


# ---------------------------------------------------------------------------
# 3. The real storage accessors (elsewhere always patched)
# ---------------------------------------------------------------------------

class TestStorageAccessors:
    def _cache(self, tmp_path, name):
        from cache import FileCache
        return FileCache(tmp_path / name)

    def test_users_round_trip(self, tmp_path):
        import storage
        with patch.object(storage, "users_cache", self._cache(tmp_path, "u.yaml")):
            assert _run(storage.get_all_users()) == []
            _run(storage.save_users([{"chat_id": 1, "display_name": "A"}]))
            got = _run(storage.get_all_users())
        assert got == [{"chat_id": 1, "display_name": "A"}]

    def test_appointments_round_trip(self, tmp_path):
        import storage
        with patch.object(storage, "appts_cache", self._cache(tmp_path, "a.yaml")):
            assert _run(storage.get_appointments()) == []
            _run(storage.save_appointments([{"id": "X1", "status": "pending"}]))
            got = _run(storage.get_appointments())
        assert got == [{"id": "X1", "status": "pending"}]

    def test_announcements_round_trip(self, tmp_path):
        import storage
        with patch.object(storage, "ann_cache", self._cache(tmp_path, "n.yaml")):
            assert _run(storage.get_announcements()) == []
            _run(storage.save_announcements([{"id": "A1", "title": "T"}]))
            got = _run(storage.get_announcements())
        assert got == [{"id": "A1", "title": "T"}]

    def test_notif_state_round_trip(self, tmp_path):
        import storage
        with patch.object(storage, "notif_state_cache", self._cache(tmp_path, "s.yaml")):
            assert _run(storage._load_notif_state()) == {}
            _run(storage._save_notif_state({"k": {"notified": [1]}}))
            got = _run(storage._load_notif_state())
        assert got == {"k": {"notified": [1]}}

    def test_known_groups_round_trip(self, tmp_path):
        import storage
        with patch.object(storage, "groups_cache", self._cache(tmp_path, "g.yaml")):
            assert _run(storage._load_known_groups()) == {}
            _run(storage._save_known_groups({"-100": {"title": "Main"}}))
            got = _run(storage._load_known_groups())
        assert got == {"-100": {"title": "Main"}}

    def test_users_save_preserves_other_top_level_keys(self, tmp_path):
        import storage
        cache = self._cache(tmp_path, "u2.yaml")
        cache.save_sync({"users": [], "schema_version": 3})
        with patch.object(storage, "users_cache", cache):
            _run(storage.save_users([{"chat_id": 9}]))
        assert cache.get_sync()["schema_version"] == 3


# ---------------------------------------------------------------------------
# 4. officials.yaml is written in round-trip mode (comments survive)
# ---------------------------------------------------------------------------

class TestOfficialsRoundTrip:
    """The bot rewrites officials.yaml (chat_id auto-fill, /enable_appt_proxies).
    Admins hand-edit it, so those writes must not strip comments — the same
    protection events.yaml got after the 2026-07 incident."""

    SAMPLE = """\
# Officials available for appointments.
# Example:
#   proxies_enabled: true
officials:
- chat_id: 111
  id: pastor_a
  name: Pastor A
  telegram_username: pastora
  # trailing note about this official
- id: elder_b
  name: Elder B
  telegram_username: elderb
"""

    def _point_at(self, tmp_path, text=None):
        """Load permissions' officials document from a temp file."""
        import permissions
        p = tmp_path / "officials.yaml"
        p.write_text(text if text is not None else self.SAMPLE)
        permissions._officials_path = p
        with open(p, encoding="utf-8") as fh:
            permissions._officials_doc = permissions._rt_yaml.load(fh)
        permissions.OFFICIALS = permissions._officials_doc["officials"]
        return p, permissions

    def test_save_preserves_header_comments(self, tmp_path):
        p, permissions = self._point_at(tmp_path)
        permissions._save_officials()
        out = p.read_text()
        assert "# Officials available for appointments." in out
        assert "#   proxies_enabled: true" in out

    def test_save_preserves_inline_comment(self, tmp_path):
        p, permissions = self._point_at(tmp_path)
        permissions._save_officials()
        assert "# trailing note about this official" in p.read_text()

    def test_auto_filled_chat_id_written(self, tmp_path):
        import yaml as pyyaml
        p, permissions = self._point_at(tmp_path)
        permissions.OFFICIALS[1]["chat_id"] = 999
        permissions._save_officials()
        data = pyyaml.safe_load(p.read_text())
        assert data["officials"][1]["chat_id"] == 999
        assert "# Officials available for appointments." in p.read_text()

    def test_proxies_toggle_written(self, tmp_path):
        import yaml as pyyaml
        p, permissions = self._point_at(tmp_path)
        permissions.OFFICIALS[0]["proxies_enabled"] = True
        permissions._save_officials()
        assert pyyaml.safe_load(p.read_text())["officials"][0]["proxies_enabled"] is True

    def test_no_officials_are_lost(self, tmp_path):
        import yaml as pyyaml
        p, permissions = self._point_at(tmp_path)
        permissions._save_officials()
        data = pyyaml.safe_load(p.read_text())
        assert [o["id"] for o in data["officials"]] == ["pastor_a", "elder_b"]

    def test_output_is_still_plain_yaml_parseable(self, tmp_path):
        import yaml as pyyaml
        p, permissions = self._point_at(tmp_path)
        permissions.OFFICIALS[0]["chat_id"] = 222
        permissions._save_officials()
        assert pyyaml.safe_load(p.read_text())["officials"][0]["chat_id"] == 222

    def test_repeated_saves_are_stable(self, tmp_path):
        p, permissions = self._point_at(tmp_path)
        permissions._save_officials()
        first = p.read_text()
        permissions._save_officials()
        assert p.read_text() == first, "saving twice must not drift the file"

    def test_save_uses_current_officials_when_list_replaced(self, tmp_path):
        """If OFFICIALS is rebound (as tests do), the save must write the new
        list — not the stale one still attached to the loaded document."""
        import yaml as pyyaml
        p, permissions = self._point_at(tmp_path)
        replacement = [{"id": "new_only", "name": "New Official"}]
        with patch.object(permissions, "OFFICIALS", replacement):
            permissions._save_officials()
        data = pyyaml.safe_load(p.read_text())
        assert [o["id"] for o in data["officials"]] == ["new_only"]
        assert "# Officials available for appointments." in p.read_text()
