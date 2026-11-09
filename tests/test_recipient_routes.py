"""Every "who do we send this to" function must survive a restart.

Three functions answer that question, and until v0.28.10 they disagreed:

    error_reporting.ops_chat_ids()   in-memory set / username / STORED PHONE
    bot._known_admin_chat_ids()      in-memory set / username
    prayer.prayer_admin_chat_ids()   in-memory set / username

The in-memory sets (`permissions._admin_chat_ids` and friends) are rebuilt
from EMPTY on every process start. So the stored-phone route is the only one
that survives a restart for somebody configured by `phone:` with no username.

This mattered more than "a missing feature", because permissions.py ALREADY
grants rights by phone — `_register_admin_by_phone` adds a PRAYER_PHONES match
to `_prayer_chat_ids`, and `is_prayer_admin` honours it. So such a person could
read prayer requests for one process lifetime and then silently stop being sent
any, while admins.yaml still said they were a prayer admin. The grant was real;
only its delivery expired.

Every test here patches the in-memory set to an EMPTY set. **That empty set IS
the restart** — before this change, every assertion below returned set() for
the two siblings.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import permissions  # noqa: E402

#: One row per recipient function: (label, getter, usernames, phones, cache).
#: The parametrized tests below run the IDENTICAL battery against each, which
#: is the point — the bug was that these three drifted apart.
ROUTES = [
    ("ops", "error_reporting", "ops_chat_ids",
     "OPS_USERNAMES", "OPS_PHONES", "_ops_chat_ids"),
    ("admin", "bot", "_known_admin_chat_ids",
     "ADMIN_USERNAMES", "ADMIN_PHONES", "_admin_chat_ids"),
    ("prayer", "handlers.prayer", "prayer_admin_chat_ids",
     "PRAYER_USERNAMES", "PRAYER_PHONES", "_prayer_chat_ids"),
]

IDS = [r[0] for r in ROUTES]

#: Recipient functions the stored-phone battery does NOT apply to, with the
#: reason. `board_officer_chat_ids` takes no route through admins.yaml at all:
#: it reads the per-user `board_officer` flag persisted in data/users.yaml by
#: /setboardofficer, so there is no BOARD_PHONES set to consult and nothing
#: in-memory to lose. It is therefore already restart-safe.
#:
#: An exemption is a hole, so this one is PROVEN, not asserted — see
#: test_exempt_functions_are_genuinely_restart_safe. Add a name here only with
#: that proof extended to cover it.
EXEMPT_FROM_PHONE_ROUTE = {
    "permissions:board_officer_chat_ids":
        "reads the persisted board_officer flag, not admins.yaml",
}


async def _ids(route, users, usernames=frozenset(), phones=frozenset()):
    """Call one recipient function with a given world, post-restart."""
    _label, modname, funcname, uattr, pattr, cattr = route
    __import__(modname)
    func = getattr(sys.modules[modname], funcname)

    async def _users():
        return users

    with patch("storage.get_all_users", side_effect=_users), \
         patch.object(permissions, uattr, set(usernames)), \
         patch.object(permissions, pattr, set(phones)), \
         patch.object(permissions, cattr, set()):
        return await func()


@pytest.mark.parametrize("route", ROUTES, ids=IDS)
class TestTheStoredPhoneRouteExists:
    async def test_stored_phone_is_matched_after_a_restart(self, route):
        """The whole point: no live contact-share, no username match.

        Mutation check: removing the phone branch from either sibling fails
        here. Before v0.28.10 this returned set() for admin and prayer.
        """
        users = [{"chat_id": 999000001, "username": "renamed",
                  "phone": "15550001234"}]
        assert await _ids(route, users, phones={"5550001234"}) == {999000001}

    @pytest.mark.parametrize("stored", [
        "15550001234",          # what Telegram actually delivers
        "5550001234",           # the same number without its country code
        "+1 (555) 000-1234",    # hand-edited, with punctuation
    ])
    async def test_the_stored_form_does_not_matter(self, route, stored):
        """normalize_phone, not a raw digit compare. Guards against each
        sibling growing its own ad-hoc stripping."""
        users = [{"chat_id": 999000001, "phone": stored}]
        assert await _ids(route, users, phones={"5550001234"}) == {999000001}

    async def test_a_phone_that_is_not_listed_is_ignored(self, route):
        users = [{"chat_id": 999000001, "phone": "15550009999"}]
        assert await _ids(route, users, phones={"5550001234"}) == set()

    async def test_a_matching_phone_without_a_chat_id_is_skipped(self, route):
        """Unreachable is not reachable — a chat_id is how we send anything."""
        users = [{"chat_id": None, "phone": "15550001234"},
                 {"phone": "15550001234"}]
        assert await _ids(route, users, phones={"5550001234"}) == set()

    async def test_username_and_phone_on_one_person_yields_one_id(self, route):
        users = [{"chat_id": 999000001, "username": "plainadmin",
                  "phone": "15550001234"}]
        assert await _ids(route, users, usernames={"plainadmin"},
                          phones={"5550001234"}) == {999000001}

    async def test_both_routes_together_reach_both_people(self, route):
        users = [{"chat_id": 999000001, "username": "plainadmin"},
                 {"chat_id": 999000002, "username": "renamed",
                  "phone": "15550001234"}]
        assert await _ids(route, users, usernames={"plainadmin"},
                          phones={"5550001234"}) == {999000001, 999000002}

    async def test_an_empty_phones_set_matches_nobody(self, route):
        """Guards against a falsy-phone bug matching every registered user."""
        users = [{"chat_id": 999000001, "phone": ""},
                 {"chat_id": 999000002},
                 {"chat_id": 999000003, "phone": None}]
        assert await _ids(route, users, phones=set()) == set()

    async def test_the_phone_route_is_what_makes_this_pass(self, route):
        """Paired guard: identical input with the phone route's input removed.

        Without this, a bug returning every chat_id unconditionally would
        satisfy every assertion above.
        """
        users = [{"chat_id": 999000001, "username": "renamed",
                  "phone": "15550001234"}]
        assert await _ids(route, users, phones={"5550001234"}) == {999000001}
        assert await _ids(route, users, phones=set()) == set()

    async def test_the_in_memory_set_is_still_honoured(self, route):
        """The contact-share route must not be lost to the new one."""
        _l, modname, funcname, uattr, pattr, cattr = route
        __import__(modname)
        func = getattr(sys.modules[modname], funcname)

        async def _users():
            return []

        with patch("storage.get_all_users", side_effect=_users), \
             patch.object(permissions, uattr, set()), \
             patch.object(permissions, pattr, set()), \
             patch.object(permissions, cattr, {999000009}):
            assert await func() == {999000009}


class TestNoRecipientFunctionIsLeftBehind:
    """The guard that outlives this change.

    `_gated_names()` in test_public_command_menu taught this: a sweep that
    enumerates things by hand goes quietly blind when a fourth one appears.
    So rather than trust the ROUTES table, discover every `*_chat_ids`
    function in the tree and fail if one is not under test above.
    """

    @staticmethod
    def _discover() -> set[str]:
        root = Path(__file__).parent.parent
        files = list(root.glob("*.py")) + list((root / "handlers").glob("*.py"))
        found: set[str] = set()
        for py in files:
            src = py.read_text(encoding="utf-8")
            for m in re.finditer(r"^async def (\w*_chat_ids)\s*\(", src, re.M):
                found.add(f"{py.stem}:{m.group(1)}")
        return found

    def test_the_discovery_actually_finds_things(self):
        """Vacuity guard: a broken regex would make the next test trivially
        pass and the real check would be gone."""
        found = self._discover()
        assert len(found) >= 3, f"discovery found almost nothing: {found}"

    def test_every_recipient_function_is_covered(self):
        found = self._discover()
        covered = {f"{m.rsplit('.', 1)[-1]}:{f}" for _l, m, f, *_ in ROUTES}
        missing = found - covered - set(EXEMPT_FROM_PHONE_ROUTE)
        assert not missing, (
            f"these decide who receives messages but are not in ROUTES: "
            f"{sorted(missing)}. Add each to ROUTES so the stored-phone "
            f"battery runs against it — a recipient function with only the "
            f"in-memory and username routes loses phone-configured people at "
            f"the next restart, silently."
        )

    def test_the_exemptions_still_exist(self):
        """A stale exemption is worse than none: it silences the sweep for a
        function that may since have been renamed or rewritten."""
        found = self._discover()
        stale = set(EXEMPT_FROM_PHONE_ROUTE) - found
        assert not stale, (
            f"exempted but no longer found in the tree: {sorted(stale)} — "
            f"remove the entry so the sweep stops being weakened by it"
        )

    async def test_exempt_functions_are_genuinely_restart_safe(self):
        """Prove the premise of every exemption.

        The claim is "this one does not need the stored-phone route because it
        reads persisted state". So run it with EVERY in-memory permissions
        cache emptied — the restart — and it must still name the right people.
        If someone later reworks one of these to depend on a config set or an
        in-memory cache while leaving it exempted, this fails instead of
        silently losing recipients at the next restart.
        """
        assert EXEMPT_FROM_PHONE_ROUTE, "nothing exempted; nothing to prove"
        handlers = {
            "permissions:board_officer_chat_ids": permissions.board_officer_chat_ids,
        }
        unproven = set(EXEMPT_FROM_PHONE_ROUTE) - handlers.keys()
        assert not unproven, f"no proof wired up for: {sorted(unproven)}"

        users = [
            {"chat_id": 999000001, "board_officer": True},
            {"chat_id": 999000002, "board_officer": False},
            {"chat_id": 999000003},
            {"chat_id": None, "board_officer": True},
        ]

        async def _users():
            return users

        for name, func in sorted(handlers.items()):
            with patch("storage.get_all_users", side_effect=_users), \
                 patch.object(permissions, "_admin_chat_ids", set()), \
                 patch.object(permissions, "_ops_chat_ids", set()), \
                 patch.object(permissions, "_prayer_chat_ids", set()), \
                 patch.object(permissions, "ADMIN_USERNAMES", set()), \
                 patch.object(permissions, "ADMIN_PHONES", set()), \
                 patch.object(permissions, "PRAYER_USERNAMES", set()), \
                 patch.object(permissions, "PRAYER_PHONES", set()), \
                 patch.object(permissions, "OPS_USERNAMES", set()), \
                 patch.object(permissions, "OPS_PHONES", set()):
                got = set(await func())
            assert got == {999000001}, (
                f"{name} is exempted from the stored-phone route on the "
                f"grounds that it reads persisted state, but across a restart "
                f"it returned {sorted(got)} instead of the one board officer "
                f"with a chat_id"
            )

    def test_each_one_consults_the_canonical_normalizer(self):
        """Source-level, deliberately: a sibling that hand-rolls its own digit
        strip would pass the behavioural tests above for the three spellings
        they try and still diverge on a fourth."""
        root = Path(__file__).parent.parent
        for _label, modname, funcname, _u, pattr, _c in ROUTES:
            rel = modname.replace(".", "/") + ".py"
            src = (root / rel).read_text(encoding="utf-8")
            body = src.split(f"async def {funcname}(")[1]
            body = body.split("\nasync def ")[0].split("\ndef ")[0]
            assert "normalize_phone" in body, (
                f"{modname}.{funcname} does not call permissions."
                f"normalize_phone — a private digit strip will drift"
            )
            assert pattr in body, (
                f"{modname}.{funcname} never consults permissions.{pattr}"
            )
