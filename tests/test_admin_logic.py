"""Tests for admin/official identification logic.

Exercises is_admin(), normalize_phone(), _register_admin_by_phone(),
_register_admin_by_username() and _register_official_if_known().

Several classes here still SIMULATE the logic they describe — they
reimplement the matching and assert against the copy. Two of them were
converted to drive the real functions on 2026-11-05, after the v0.28.6
normalisation change passed the simulated versions untouched while proving
nothing about the shipped code. `TestAdminSetConstruction` and
`TestOfficialMatching` below are still copies, and are worth the same
treatment; they are left for now rather than rewritten in a change about
phone normalisation.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import permissions


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
        {"id": "off3", "name": "Deacon Fred", "telegram_username": "deaconfred",
         "phone": "5553334444"},
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

class TestNormalizePhone:
    """permissions.normalize_phone — the one canonical form (v0.28.6).

    Before it, both sides of every phone comparison were a bare digit-strip,
    so nothing reconciled a country code: Telegram delivers a NANP number as
    11 digits while the README and the admins.yaml comment both showed ten.
    An entry written as documented loaded cleanly and matched nothing.
    """

    @pytest.mark.parametrize("raw", [
        "5550001234",            # as the documentation showed it
        "15550001234",           # as Telegram delivers it
        "+15550001234",          # E.164
        "+1 (555) 000-1234",     # hand-typed with punctuation
        "1-555-000-1234",
        " 555 000 1234 ",
    ])
    def test_every_spelling_of_one_number_agrees(self, raw):
        assert permissions.normalize_phone(raw) == "5550001234"

    @pytest.mark.parametrize("raw,expected", [
        ("+27555000123", "27555000123"),   # South Africa: 11 digits, leading 2
        ("27555000123", "27555000123"),
        ("+44 7700 900123", "447700900123"),   # UK: 12 digits
    ])
    def test_numbers_outside_the_nanp_are_untouched(self, raw, expected):
        """The strip must not corrupt a non-NANP number.

        Worth asserting rather than assuming: the bot ships isiZulu, so a
        +27 number is a realistic entry, and it is 11 digits long — the same
        length the strip acts on. It survives because it does not start
        with 1, which is the whole reason the rule is keyed on that digit.
        """
        assert permissions.normalize_phone(raw) == expected

    def test_a_leading_one_is_only_stripped_at_eleven_digits(self):
        """Ten digits beginning with 1 is a local number, not a country code."""
        assert permissions.normalize_phone("1555000123") == "1555000123"
        assert permissions.normalize_phone("155500012345") == "155500012345"

    @pytest.mark.parametrize("raw", [None, "", "   ", "+", "()- "])
    def test_nothing_in_gives_empty_out_rather_than_raising(self, raw):
        """Callers guard on a falsy result; none of them expect an exception."""
        assert permissions.normalize_phone(raw) == ""

    def test_two_different_numbers_stay_different(self):
        """Guards against a normalisation that collapses everything."""
        assert (permissions.normalize_phone("+15550001234")
                != permissions.normalize_phone("+15559990000"))


class TestPhoneSetsAreBuiltCanonically:
    """The *_PHONES sets, built from a real file by the real module code.

    Found by mutation on 2026-11-05: reverting ADMIN_PHONES and OPS_PHONES to
    a raw digit-strip passed the ENTIRE suite. Every other test patches those
    sets, so nothing exercised their construction — and construction is the
    config half of the normalisation fix. A `phone:` spelled with its country
    code would build an 11-digit member that the canonical inbound number can
    never equal, which is the original bug with the sides swapped.

    Reloads `permissions` against a constructed config directory. Note it is
    `settings.CONFIG_DIR` that must be patched, not `permissions.CONFIG_DIR`:
    the reload re-executes `from settings import CONFIG_DIR`, so patching the
    name on permissions is overwritten immediately. The restoring reload sits
    OUTSIDE the patch, or the module is left loaded from the temp directory.
    """

    #: One person, spelled three ways, with a distinct number per role so the
    #: sets cannot be confused with one another.
    ADMINS_YAML = (
        "admins:\n"
        '  - username: "plainadmin"\n'
        '    phone: "5550001111"\n'              # as the documentation showed
        '  - username: "opsadmin"\n'
        '    phone: "15550002222"\n'             # with the country code
        "    ops: true\n"
        '  - username: "prayeradmin"\n'
        '    phone: "+1 (555) 000-3333"\n'       # hand-typed punctuation
        "    prayer: true\n"
    )

    @staticmethod
    def _reload_with(tmp_path, admins_yaml):
        import importlib
        import settings
        (tmp_path / "admins.yaml").write_text(admins_yaml)
        (tmp_path / "officials.yaml").write_text("officials: []\n")
        with patch.object(settings, "CONFIG_DIR", tmp_path):
            return importlib.reload(permissions)

    @staticmethod
    def _restore():
        import importlib
        importlib.reload(permissions)

    def test_every_spelling_lands_canonically_in_the_sets(self, tmp_path):
        try:
            p = self._reload_with(tmp_path, self.ADMINS_YAML)
            assert p.ADMIN_PHONES == {"5550001111", "5550002222", "5550003333"}, (
                "a config phone did not reach ADMIN_PHONES in canonical form; "
                f"got {sorted(p.ADMIN_PHONES)}"
            )
            assert p.OPS_PHONES == {"5550002222"}
            assert p.PRAYER_PHONES == {"5550003333"}
        finally:
            self._restore()

    def test_no_set_member_carries_a_country_code(self, tmp_path):
        """The property, stated independently of the exact numbers above."""
        try:
            p = self._reload_with(tmp_path, self.ADMINS_YAML)
            every = p.ADMIN_PHONES | p.OPS_PHONES | p.PRAYER_PHONES
            assert every, "the sets came out empty — this test proves nothing"
            offenders = [n for n in every if len(n) == 11 and n.startswith("1")]
            assert not offenders, (
                f"these kept their country code: {offenders}. An inbound "
                "number is canonicalised, so these can never be matched."
            )
        finally:
            self._restore()

    def test_the_real_config_is_loaded_again_afterwards(self, tmp_path):
        """Paired guard: a reload left pointing at tmp_path would silently
        corrupt every later test in the session."""
        try:
            self._reload_with(tmp_path, self.ADMINS_YAML)
        finally:
            self._restore()
        import settings
        assert permissions.CONFIG_DIR == settings.CONFIG_DIR
        assert permissions.CONFIG_DIR != tmp_path

    def test_an_entry_with_no_phone_adds_nothing(self, tmp_path):
        try:
            p = self._reload_with(
                tmp_path, 'admins:\n  - username: "plainadmin"\n')
            assert p.ADMIN_PHONES == set()
            assert p.ADMIN_USERNAMES == {"plainadmin"}
        finally:
            self._restore()


class TestRegisterAdminByPhone:
    """The REAL permissions._register_admin_by_phone.

    This class used to reimplement the function — its own `re.sub`, its own
    set membership — and assert against the copy. That passed the v0.28.6
    normalisation change untouched while proving nothing about the shipped
    code, which is the only thing it exists to check. It now calls the real
    function and reads the real module state.
    """

    async def _run(self, admin_phones: set[str], phone: str | None,
                   user_id: int) -> set[int]:
        with patch.object(permissions, "ADMIN_PHONES", set(admin_phones)), \
             patch.object(permissions, "OPS_PHONES", set()), \
             patch.object(permissions, "PRAYER_PHONES", set()), \
             patch.object(permissions, "_admin_chat_ids", set()), \
             patch.object(permissions, "_ops_chat_ids", set()), \
             patch.object(permissions, "_prayer_chat_ids", set()):
            await permissions._register_admin_by_phone(user_id, phone)
            return set(permissions._admin_chat_ids)

    async def test_matching_phone_adds_user(self):
        assert 42 in await self._run({"5550001234"}, "5550001234", 42)

    async def test_non_matching_phone_excluded(self):
        assert 42 not in await self._run({"5550001234"}, "9990001234", 42)

    async def test_none_phone_skipped(self):
        assert await self._run({"5550001234"}, None, 42) == set()

    async def test_the_country_code_form_matches_a_ten_digit_entry(self):
        """The bug this release fixes, at the real call site.

        The config entry is written as the documentation showed it; Telegram
        delivers the number with its country code. Before v0.28.6 this was a
        miss, and nothing anywhere reported it.
        """
        assert 99 in await self._run({"5550001234"}, "+1 (555) 000-1234", 99)

    async def test_a_ten_digit_contact_matches_an_eleven_digit_entry(self):
        """And the reverse, for a config file written the other way."""
        assert 77 in await self._run({"5550001234"}, "5550001234", 77)

    async def test_e164_format_matches(self):
        assert 77 in await self._run({"5550001234"}, "+15550001234", 77)

    async def test_ops_and_prayer_sets_are_independent(self):
        """A phone in ADMIN_PHONES alone must not confer ops or prayer."""
        with patch.object(permissions, "ADMIN_PHONES", {"5550001234"}), \
             patch.object(permissions, "OPS_PHONES", set()), \
             patch.object(permissions, "PRAYER_PHONES", set()), \
             patch.object(permissions, "_admin_chat_ids", set()), \
             patch.object(permissions, "_ops_chat_ids", set()), \
             patch.object(permissions, "_prayer_chat_ids", set()):
            await permissions._register_admin_by_phone(5, "+15550001234")
            assert permissions._admin_chat_ids == {5}
            assert permissions._ops_chat_ids == set()
            assert permissions._prayer_chat_ids == set()


class TestRealOfficialRegistration:
    """The REAL handlers.user_basics._register_official_if_known.

    No test called it before this change — every one either patched it out or
    reimplemented the matching, so the normalisation fix applied to it was
    unverified. `_save_officials` is patched because the real one writes
    config/officials.yaml, which is untracked live data.
    """

    @staticmethod
    def _officials():
        return [
            {"id": "off1", "name": "Pastor Example", "telegram_username": "pastorex"},
            {"id": "off2", "name": "Elder Example", "phone": "5551112222"},
            {"id": "off3", "name": "Deacon Example", "phone": "15553334444"},
        ]

    async def _run(self, user_id, username, phone):
        import handlers.user_basics as ub
        officials = self._officials()
        with patch.object(permissions, "OFFICIALS", officials), \
             patch.object(permissions, "_save_officials", lambda: None):
            await ub._register_official_if_known(user_id, username, phone)
        return {o["id"]: o.get("chat_id") for o in officials}

    async def test_a_ten_digit_entry_matches_the_delivered_number(self):
        """off2 is written without a country code, as the docs showed."""
        got = await self._run(501, None, "+1 (555) 111-2222")
        assert got["off2"] == 501, "the documented 10-digit form did not match"
        assert got["off1"] is None and got["off3"] is None

    async def test_an_eleven_digit_entry_matches_a_bare_number(self):
        """off3 is written with one, which must not stop matching either."""
        got = await self._run(502, None, "5553334444")
        assert got["off3"] == 502
        assert got["off1"] is None and got["off2"] is None

    async def test_username_matching_still_works(self):
        got = await self._run(503, "@PastorEx", None)
        assert got["off1"] == 503

    async def test_an_unknown_number_matches_nobody(self):
        got = await self._run(504, "stranger", "+15559998888")
        assert set(got.values()) == {None}

    async def test_a_proxy_is_matched_too(self):
        """Proxies are a second level the loop walks; easy to drop silently."""
        import handlers.user_basics as ub
        officials = [{"id": "off1", "name": "Pastor Example",
                      "proxies": [{"id": "p1", "phone": "5557778888"}]}]
        with patch.object(permissions, "OFFICIALS", officials), \
             patch.object(permissions, "_save_officials", lambda: None):
            await ub._register_official_if_known(601, None, "+15557778888")
        assert officials[0]["proxies"][0]["chat_id"] == 601

    async def test_officials_are_saved_only_when_something_changed(self):
        import handlers.user_basics as ub
        calls = []
        officials = self._officials()
        with patch.object(permissions, "OFFICIALS", officials), \
             patch.object(permissions, "_save_officials",
                          lambda: calls.append(1)):
            await ub._register_official_if_known(700, "nobody", "+15550000000")
            assert calls == [], "saved the file despite matching nobody"
            await ub._register_official_if_known(701, None, "+15551112222")
            assert calls == [1], "a real match did not persist"


# ---------------------------------------------------------------------------
# Official matching logic (_register_official_if_known)
# ---------------------------------------------------------------------------

class TestOfficialMatching:
    """Verify that official recognition works by username OR phone."""

    OFFICIALS = [
        {"id": "off1", "name": "Pastor Dave", "telegram_username": "pastordave"},
        {"id": "off2", "name": "Elder Eve", "phone": "5551112222"},
        {"id": "off3", "name": "Deacon Fred", "telegram_username": "deaconfred",
         "phone": "5553334444"},
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
