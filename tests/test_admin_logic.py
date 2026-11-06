"""Tests for admin/official identification logic.

Exercises normalize_phone(), the *_USERNAMES / *_PHONES set construction,
_register_admin_by_phone() and _register_official_if_known() — every one of
them by calling the shipped function, not a copy of it.

THAT IS THE POINT OF THIS FILE'S HISTORY. Until 2026-11-06 most of what was
here SIMULATED the code it named: `TestPhoneNormalisation` asserted on
`re.sub` (that is a test of the standard library), `TestAdminUsernameSet`
rebuilt the set comprehensions inline, and `TestOfficialMatching`
reimplemented the whole matching loop and asserted against its own version.
The v0.28.6 phone-normalisation change passed all three untouched while
changing the real behaviour of all three — which is the only evidence anyone
needs that a test asserting against a reimplementation asserts nothing.

Converting them also removed `_load_bot_module`, a 30-line helper that was
never called from anywhere in the suite and that patched `yaml.safe_load` and
`builtins.open` process-wide when it was.

The construction tests reload `permissions` against a constructed config
directory. Note it is `settings.CONFIG_DIR` that must be patched, not
`permissions.CONFIG_DIR`: the reload re-executes
`from settings import CONFIG_DIR`, so patching the name on permissions is
overwritten immediately. Every reload is paired with a restoring reload in a
`finally`, OUTSIDE the patch, or the module is left loaded from a temp
directory that has since been deleted and every later test in the session
sees it.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import permissions


def _reload_permissions_with(tmp_path, admins_yaml, officials_yaml="officials: []\n"):
    """Rebuild the permissions module from a config directory we control."""
    import importlib
    import settings
    (tmp_path / "admins.yaml").write_text(admins_yaml)
    (tmp_path / "officials.yaml").write_text(officials_yaml)
    with patch.object(settings, "CONFIG_DIR", tmp_path):
        return importlib.reload(permissions)


def _restore_permissions():
    """Reload from the real config. MUST run outside the CONFIG_DIR patch."""
    import importlib
    importlib.reload(permissions)


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


class TestUsernameSetsAreBuiltFromConfig:
    """The *_USERNAMES sets, built from a real file by the real module code.

    Replaces `TestAdminUsernameSet`, which rebuilt the set comprehensions
    inline and asserted against its own copy — so it could not fail when the
    real construction changed, which is the only thing it was for.

    The role sets are the part worth guarding. `prayer: true` decides who may
    read prayer requests, and members are told that stays with the designated
    leadership; a construction bug that widened PRAYER_USERNAMES would be a
    disclosure, not a glitch.
    """

    ADMINS_YAML = (
        "admins:\n"
        '  - username: "@MixedCase"\n'          # leading @ and capitals
        '  - username: "plainadmin"\n'
        '  - phone: "5550001234"\n'             # phone-only: no username set entry
        '  - username: "opsadmin"\n'
        "    ops: true\n"
        '  - username: "prayeradmin"\n'
        "    prayer: true\n"
        '  - username: "bothroles"\n'
        "    ops: true\n"
        "    prayer: true\n"
    )

    def test_usernames_are_lowercased_and_stripped_of_the_at(self, tmp_path):
        try:
            p = _reload_permissions_with(tmp_path, self.ADMINS_YAML)
            assert p.ADMIN_USERNAMES == {
                "mixedcase", "plainadmin", "opsadmin", "prayeradmin", "bothroles",
            }, f"got {sorted(p.ADMIN_USERNAMES)}"
            assert "@mixedcase" not in p.ADMIN_USERNAMES
            assert "MixedCase" not in p.ADMIN_USERNAMES
        finally:
            _restore_permissions()

    def test_a_phone_only_entry_adds_no_username(self, tmp_path):
        try:
            p = _reload_permissions_with(
                tmp_path, 'admins:\n  - phone: "5550001234"\n')
            assert p.ADMIN_USERNAMES == set()
            assert p.ADMIN_PHONES == {"5550001234"}
        finally:
            _restore_permissions()

    def test_the_role_sets_contain_only_their_own_holders(self, tmp_path):
        try:
            p = _reload_permissions_with(tmp_path, self.ADMINS_YAML)
            assert p.OPS_USERNAMES == {"opsadmin", "bothroles"}
            assert p.PRAYER_USERNAMES == {"prayeradmin", "bothroles"}
        finally:
            _restore_permissions()

    def test_a_role_set_is_never_wider_than_the_admin_set(self, tmp_path):
        """Not a tautology in the direction that matters: it would fail if a
        role set were ever built from a different source than the admin list,
        which is how a prayer-queue disclosure would most plausibly arrive."""
        try:
            p = _reload_permissions_with(tmp_path, self.ADMINS_YAML)
            assert p.OPS_USERNAMES <= p.ADMIN_USERNAMES
            assert p.PRAYER_USERNAMES <= p.ADMIN_USERNAMES
            assert p.OPS_USERNAMES and p.PRAYER_USERNAMES, "both sets empty"
        finally:
            _restore_permissions()

    def test_an_admin_with_no_flags_holds_no_role(self, tmp_path):
        try:
            p = _reload_permissions_with(
                tmp_path, 'admins:\n  - username: "plainadmin"\n')
            assert p.ADMIN_USERNAMES == {"plainadmin"}
            assert p.OPS_USERNAMES == set()
            assert p.PRAYER_USERNAMES == set()
        finally:
            _restore_permissions()

    def test_an_empty_admin_list_yields_empty_sets(self, tmp_path):
        try:
            p = _reload_permissions_with(tmp_path, "admins: []\n")
            assert p.ADMIN_USERNAMES == set()
            assert p.ADMIN_PHONES == set()
        finally:
            _restore_permissions()


class TestPhoneSetsAreBuiltCanonically:
    """The *_PHONES sets, built from a real file by the real module code.

    Found by mutation on 2026-11-05: reverting ADMIN_PHONES and OPS_PHONES to
    a raw digit-strip passed the ENTIRE suite. Every other test patches those
    sets, so nothing exercised their construction — and construction is the
    config half of the normalisation fix. A `phone:` spelled with its country
    code would build an 11-digit member that the canonical inbound number can
    never equal, which is the original bug with the sides swapped.

    Uses the module-level reload harness; see this module's docstring for why
    `settings.CONFIG_DIR` is the name that must be patched.
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

    def test_every_spelling_lands_canonically_in_the_sets(self, tmp_path):
        try:
            p = _reload_permissions_with(tmp_path, self.ADMINS_YAML)
            assert p.ADMIN_PHONES == {"5550001111", "5550002222", "5550003333"}, (
                "a config phone did not reach ADMIN_PHONES in canonical form; "
                f"got {sorted(p.ADMIN_PHONES)}"
            )
            assert p.OPS_PHONES == {"5550002222"}
            assert p.PRAYER_PHONES == {"5550003333"}
        finally:
            _restore_permissions()

    def test_no_set_member_carries_a_country_code(self, tmp_path):
        """The property, stated independently of the exact numbers above."""
        try:
            p = _reload_permissions_with(tmp_path, self.ADMINS_YAML)
            every = p.ADMIN_PHONES | p.OPS_PHONES | p.PRAYER_PHONES
            assert every, "the sets came out empty — this test proves nothing"
            offenders = [n for n in every if len(n) == 11 and n.startswith("1")]
            assert not offenders, (
                f"these kept their country code: {offenders}. An inbound "
                "number is canonicalised, so these can never be matched."
            )
        finally:
            _restore_permissions()

    def test_the_real_config_is_loaded_again_afterwards(self, tmp_path):
        """Paired guard: a reload left pointing at tmp_path would silently
        corrupt every later test in the session."""
        try:
            _reload_permissions_with(tmp_path, self.ADMINS_YAML)
        finally:
            _restore_permissions()
        import settings
        assert permissions.CONFIG_DIR == settings.CONFIG_DIR
        assert permissions.CONFIG_DIR != tmp_path

    def test_an_entry_with_no_phone_adds_nothing(self, tmp_path):
        try:
            p = _reload_permissions_with(
                tmp_path, 'admins:\n  - username: "plainadmin"\n')
            assert p.ADMIN_PHONES == set()
            assert p.ADMIN_USERNAMES == {"plainadmin"}
        finally:
            _restore_permissions()


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
# Official matching by username, with both routes present
# ---------------------------------------------------------------------------

class TestOfficialMatchingRoutes:
    """The username/phone routes of the REAL _register_official_if_known.

    These cases came from `TestOfficialMatching`, which reimplemented the
    whole matching loop — its own `lstrip("@")`, its own `re.sub`, its own
    precedence rule — and asserted against that copy. It passed the v0.28.6
    normalisation change without a murmur while the shipped behaviour it
    claimed to describe changed underneath it.

    `off3` carries BOTH a username and a phone, which is the configuration
    the precedence rule exists for.
    """

    @staticmethod
    def _officials():
        return [
            {"id": "off1", "name": "Pastor Example", "telegram_username": "pastorex"},
            {"id": "off2", "name": "Elder Example", "phone": "5551112222"},
            {"id": "off3", "name": "Deacon Example",
             "telegram_username": "deaconex", "phone": "5553334444"},
        ]

    async def _matched(self, user_id, username, phone):
        """IDs whose chat_id the real function set to user_id."""
        import handlers.user_basics as ub
        officials = self._officials()
        with patch.object(permissions, "OFFICIALS", officials), \
             patch.object(permissions, "_save_officials", lambda: None):
            await ub._register_official_if_known(user_id, username, phone)
        return [o["id"] for o in officials if o.get("chat_id") == user_id]

    async def test_match_by_username(self):
        assert await self._matched(1, "pastorex", None) == ["off1"]

    async def test_match_by_phone(self):
        assert await self._matched(2, None, "5551112222") == ["off2"]

    async def test_match_by_username_when_both_are_present(self):
        assert await self._matched(3, "deaconex", None) == ["off3"]

    async def test_match_by_phone_when_both_are_present(self):
        assert await self._matched(3, None, "5553334444") == ["off3"]

    async def test_one_person_is_matched_once_when_both_routes_hit(self):
        matched = await self._matched(3, "deaconex", "5553334444")
        assert matched == ["off3"]

    async def test_no_match_returns_nothing(self):
        assert await self._matched(99, "nobody", "5559990000") == []

    async def test_neither_username_nor_phone_matches_nobody(self):
        """A user with no username who has not shared a contact."""
        assert await self._matched(1, None, None) == []

    async def test_username_match_is_case_insensitive(self):
        assert await self._matched(1, "PastorEx", None) == ["off1"]

    async def test_a_leading_at_is_stripped(self):
        assert await self._matched(1, "@pastorex", None) == ["off1"]

    async def test_a_formatted_phone_is_normalised(self):
        assert await self._matched(2, None, "(555) 111-2222") == ["off2"]

    async def test_a_partial_phone_does_not_match(self):
        """Matching is equality, not a substring test."""
        assert await self._matched(2, None, "111222") == []

    async def test_an_unknown_username_does_not_match(self):
        assert await self._matched(5, "randomuser", None) == []

    async def test_an_official_with_no_username_is_not_matched_by_an_empty_one(self):
        """off2 has no telegram_username; an empty incoming one must not
        compare equal to it, or every username-less official would be claimed
        by the first user who has no Telegram handle.

        The guard that does this is the OUTER `if uname_lower:`, not the inner
        `if rname and ...`. Established by mutation, not by reading: removing
        the inner one alone changes nothing (the outer has already returned),
        while removing the outer one fails this test and four others. The
        inner condition is therefore an EQUIVALENT mutation — unkillable, and
        a test written to try would be asserting on an unreachable branch.
        """
        assert await self._matched(7, "", "5551112222") == ["off2"]
