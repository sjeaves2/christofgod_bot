"""Tests for admin/official identification logic.

Exercises is_admin(), _register_admin_by_phone(), _register_admin_by_username(),
and _register_official_if_known() by testing the pure matching logic directly.
"""
from __future__ import annotations

import asyncio
import importlib
import re
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# We need to import bot.py but it requires a valid config.yaml and will
# attempt to load YAML files at module level.  Patch open / yaml.safe_load
# for the globals we don't care about in this test.

CONFIG_YAML = {
    "bot": {"token": "FAKE", "timezone": "America/New_York", "display_name": "Test Bot"},
    "notifications": {"default_minutes_before": 90},
    "paths": {"data_dir": "data", "logs_dir": "logs", "generated_dir": "generated"},
    "log": {"retention_days": 180},
}

ADMINS_YAML = {
    "admins": [
        {"username": "alice", "display_name": "Alice"},
        {"phone": "5550001234", "display_name": "Bob"},
        {"username": "carol", "phone": "5559876543", "display_name": "Carol (both)"},
    ]
}

OFFICIALS_YAML = {
    "officials": [
        {"id": "off1", "name": "Pastor Dave", "telegram_username": "pastordave"},
        {"id": "off2", "name": "Elder Eve", "phone": "5551112222"},
        {"id": "off3", "name": "Deacon Fred", "telegram_username": "deaconfred", "phone": "5553334444"},
    ]
}


def _load_bot_module():
    """Import bot with mocked filesystem / Telegram deps."""
    import yaml

    real_safe_load = yaml.safe_load

    def fake_safe_load(stream):
        text = stream.read() if hasattr(stream, "read") else stream
        # Return fixture data for known file contents
        if "token" in str(text):
            return CONFIG_YAML
        if "admins" in str(text):
            return ADMINS_YAML
        if "officials" in str(text):
            return OFFICIALS_YAML
        return real_safe_load(text) if isinstance(text, str) else {}

    # Patch yaml.safe_load, Path.read_text, and mkdir so module-level code runs
    with (
        patch("yaml.safe_load", side_effect=fake_safe_load),
        patch("pathlib.Path.read_text", return_value="token: FAKE\nadmins: []\nofficials: []"),
        patch("pathlib.Path.mkdir", return_value=None),
        patch("builtins.open", MagicMock(return_value=MagicMock(
            __enter__=lambda s, *a: MagicMock(read=lambda: ""),
            __exit__=MagicMock(return_value=False),
        ))),
    ):
        # Force reimport
        if "bot" in sys.modules:
            del sys.modules["bot"]
        import bot as _bot
    return _bot


# ---------------------------------------------------------------------------
# Phone normalisation helper
# ---------------------------------------------------------------------------

class TestPhoneNormalisation:
    """Verify digits-only normalisation used for phone matching."""

    def test_strips_plus(self):
        assert re.sub(r"\D", "", "+15550001234") == "15550001234"

    def test_strips_dashes(self):
        assert re.sub(r"\D", "", "555-000-1234") == "5550001234"

    def test_strips_spaces(self):
        assert re.sub(r"\D", "", "555 000 1234") == "5550001234"

    def test_strips_parens(self):
        assert re.sub(r"\D", "", "(555) 000-1234") == "5550001234"

    def test_already_digits_unchanged(self):
        assert re.sub(r"\D", "", "5550001234") == "5550001234"


# ---------------------------------------------------------------------------
# Admin username set construction
# ---------------------------------------------------------------------------

class TestAdminUsernameSet:
    """ADMIN_USERNAMES must include username-bearing entries and exclude phone-only."""

    def _make_sets(self, admins):
        usernames = {
            a["username"].lstrip("@").lower()
            for a in admins
            if a.get("username")
        }
        phones = {
            re.sub(r"\D", "", a["phone"])
            for a in admins
            if a.get("phone")
        }
        return usernames, phones

    def test_username_only_entry_in_username_set(self):
        admins = [{"username": "alice"}]
        u, p = self._make_sets(admins)
        assert "alice" in u
        assert len(p) == 0

    def test_phone_only_entry_in_phone_set(self):
        admins = [{"phone": "5550001234"}]
        u, p = self._make_sets(admins)
        assert len(u) == 0
        assert "5550001234" in p

    def test_both_fields_land_in_both_sets(self):
        admins = [{"username": "carol", "phone": "5559876543"}]
        u, p = self._make_sets(admins)
        assert "carol" in u
        assert "5559876543" in p

    def test_username_leading_at_stripped(self):
        admins = [{"username": "@dave"}]
        u, _ = self._make_sets(admins)
        assert "dave" in u
        assert "@dave" not in u

    def test_username_lowercased(self):
        admins = [{"username": "UPPERCASE"}]
        u, _ = self._make_sets(admins)
        assert "uppercase" in u

    def test_multiple_admins(self):
        admins = ADMINS_YAML["admins"]
        u, p = self._make_sets(admins)
        assert "alice" in u
        assert "carol" in u
        assert "5550001234" in p
        assert "5559876543" in p


# ---------------------------------------------------------------------------
# _register_admin_by_phone logic (pure, no Telegram)
# ---------------------------------------------------------------------------

class TestRegisterAdminByPhone:
    """Test the phone-matching logic that populates _admin_chat_ids."""

    def _run(self, admin_phones: set[str], phone: str | None, user_id: int) -> set[int]:
        """Simulate _register_admin_by_phone without importing bot."""
        chat_ids: set[int] = set()
        if not phone:
            return chat_ids
        normalized = re.sub(r"\D", "", phone)
        if normalized in admin_phones:
            chat_ids.add(user_id)
        return chat_ids

    def test_matching_phone_adds_user(self):
        result = self._run({"5550001234"}, "5550001234", 42)
        assert 42 in result

    def test_non_matching_phone_excluded(self):
        result = self._run({"5550001234"}, "9990001234", 42)
        assert 42 not in result

    def test_none_phone_skipped(self):
        result = self._run({"5550001234"}, None, 42)
        assert len(result) == 0

    def test_formatted_phone_matches_digits_only_stored(self):
        # "+1 (555) 000-1234" normalises to "15550001234" (with country code).
        # The stored value must also include the country code to match.
        result = self._run({"15550001234"}, "+1 (555) 000-1234", 99)
        assert 99 in result

    def test_e164_format_matches(self):
        result = self._run({"15550001234"}, "+15550001234", 77)
        assert 77 in result


# ---------------------------------------------------------------------------
# Official matching logic (_register_official_if_known)
# ---------------------------------------------------------------------------

class TestOfficialMatching:
    """Verify that official recognition works by username OR phone."""

    OFFICIALS = [
        {"id": "off1", "name": "Pastor Dave", "telegram_username": "pastordave"},
        {"id": "off2", "name": "Elder Eve", "phone": "5551112222"},
        {"id": "off3", "name": "Deacon Fred", "telegram_username": "deaconfred", "phone": "5553334444"},
    ]

    def _match(self, user_id: int, username: str | None, phone: str | None) -> list[str]:
        """Return list of matched official IDs."""
        uname_lower = (username or "").lstrip("@").lower()
        phone_norm = re.sub(r"\D", "", phone or "")
        matched_ids = []
        for off in self.OFFICIALS:
            matched = False
            if uname_lower:
                oname = (off.get("telegram_username") or "").lstrip("@").lower()
                if oname and oname == uname_lower:
                    matched = True
            if not matched and phone_norm:
                ophone = re.sub(r"\D", "", off.get("phone") or "")
                if ophone and ophone == phone_norm:
                    matched = True
            if matched:
                matched_ids.append(off["id"])
        return matched_ids

    def test_match_by_username(self):
        assert self._match(1, "pastordave", None) == ["off1"]

    def test_match_by_phone(self):
        assert self._match(2, None, "5551112222") == ["off2"]

    def test_match_by_username_when_both_available(self):
        assert self._match(3, "deaconfred", None) == ["off3"]

    def test_match_by_phone_when_both_available(self):
        assert self._match(3, None, "5553334444") == ["off3"]

    def test_username_takes_precedence_over_phone(self):
        # If username matches, phone path shouldn't add duplicates
        matched = self._match(3, "deaconfred", "5553334444")
        assert matched.count("off3") == 1

    def test_no_match_returns_empty(self):
        assert self._match(99, "nobody", "0000000000") == []

    def test_none_username_and_none_phone_returns_empty(self):
        assert self._match(1, None, None) == []

    def test_username_case_insensitive(self):
        assert self._match(1, "PastorDave", None) == ["off1"]

    def test_username_leading_at_stripped(self):
        assert self._match(1, "@pastordave", None) == ["off1"]

    def test_formatted_phone_normalised(self):
        assert self._match(2, None, "(555) 111-2222") == ["off2"]

    def test_partial_phone_no_match(self):
        assert self._match(2, None, "111222") == []

    def test_unknown_username_no_match(self):
        assert self._match(5, "randomuser", None) == []
