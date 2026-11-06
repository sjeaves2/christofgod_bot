"""The command menu Telegram shows every user.

Found by mutation on 2026-10-29: nothing verified this menu at all. Deleting
the `set_my_commands` call passed the whole suite, and so did adding
`/boarditems` to it.

Both matter. The menu is shown to everyone who opens a chat with the bot, and
there are ~30 people in the group against 14 registered. Every admin, prayer
and board command is gated and answers "Unknown command" to anyone else — so a
gated command in this menu would advertise a queue most people cannot open and
then rebuff them for trying. That is the same reasoning that keeps the prayer
and board blocks out of /help for non-holders; it simply had no test on this
side.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))


def _public_names() -> set[str]:
    import bot
    return {c.command for c in bot.PUBLIC_COMMANDS}


#: Commands that appear in a role block but are NOT role-gated, so listing
#: them publicly is correct rather than a leak.
#:
#: /sharecontact is described in the officials block because that is where the
#: explanation matters — an official's `phone:` only binds to their chat_id
#: through a contact share — but anyone may run it: it stores the caller's own
#: number and nothing else, and handle_contact refuses someone else's contact.
#:
#: An exemption is a hole, so this one is PROVEN, not asserted:
#: test_every_exemption_is_really_ungated below calls each handler as an
#: unprivileged user and fails if it is refused. Add a name here only with
#: that proof passing.
UNGATED_BUT_ROLE_DOCUMENTED = {"sharecontact"}


def _gated_names() -> set[str]:
    """Commands the role-gated help blocks advertise to their holders only.

    OFFICIAL_COMMANDS_TEXT is included deliberately. It was added in v0.28.9
    and, until this line, was the one block this sweep could not see — so a
    genuinely official-only command listed in both it and the menu would have
    passed unnoticed. A new block must be added here or the guard quietly
    stops covering it.
    """
    import handlers.user_basics as ub
    names: set[str] = set()
    for block in (ub.ADMIN_COMMANDS_TEXT, ub.PRAYER_COMMANDS_TEXT,
                  ub.BOARD_COMMANDS_TEXT, ub.OFFICIAL_COMMANDS_TEXT):
        names |= set(re.findall(r"^`?/([a-z_]+)", block, re.M))
    return names - UNGATED_BUT_ROLE_DOCUMENTED


class TestTheMenuIsPublished:
    async def test_post_init_sends_it_to_telegram(self):
        """Mutation check: replacing the set_my_commands call with `pass` used
        to pass the entire suite, so the menu could have silently stopped
        being published."""
        import bot

        app = MagicMock()
        app.bot.set_my_commands = AsyncMock()
        with patch.object(bot, "schedule_all_upcoming", AsyncMock()), \
             patch.object(bot, "alert_on_bad_service_links", AsyncMock()), \
             patch.object(bot, "_register_jobs", lambda a: None), \
             patch.object(bot.error_reporting, "reset_error_log",
                          lambda *a, **k: 0):
            await bot.post_init(app)

        app.bot.set_my_commands.assert_awaited_once()
        sent = app.bot.set_my_commands.await_args.args[0]
        assert sent is bot.PUBLIC_COMMANDS

    def test_the_menu_is_not_empty(self):
        assert len(_public_names()) >= 10


class TestNoGatedCommandIsAdvertised:
    def test_the_sweep_sees_both_lists(self):
        """Guards the test below: an empty gated list would make it vacuous."""
        assert len(_gated_names()) >= 10, sorted(_gated_names())
        assert len(_public_names()) >= 10

    def test_public_and_gated_do_not_overlap(self):
        """Mutation check: adding /boarditems to PUBLIC_COMMANDS fails here."""
        leaked = _public_names() & _gated_names()
        assert not leaked, (
            f"these are role-gated but advertised to everyone: {sorted(leaked)}. "
            "Tapping one gets 'Unknown command', and listing it tells ~30 "
            "people a queue exists that they cannot open."
        )

    def test_the_officials_block_is_in_the_sweep(self):
        """The block this sweep could not see until v0.28.9.

        /enable_appt_proxies is genuinely official-only and must therefore be
        counted as gated; if it is not, the overlap check above is blind to
        the whole block.
        """
        assert "enable_appt_proxies" in _gated_names(), (
            "the officials block is not being read, so a gated official "
            "command could be published to all ~30 members unnoticed"
        )

    async def test_every_exemption_is_really_ungated(self):
        """An exemption is a hole, so prove the premise rather than trust it.

        Each exempted command is called as a plain member — no admin, no
        official, no roles — and must NOT answer with the gating refusal.
        If someone later wraps one of these in @admin_only while leaving it
        in UNGATED_BUT_ROLE_DOCUMENTED, this fails instead of silently
        advertising a command that rebuffs everyone who taps it.
        """
        import handlers.user_basics as ub
        import permissions

        assert UNGATED_BUT_ROLE_DOCUMENTED, "nothing exempted; nothing to prove"
        handlers = {"sharecontact": ub.cmd_sharecontact}
        missing = UNGATED_BUT_ROLE_DOCUMENTED - handlers.keys()
        assert not missing, f"no handler wired into this proof for: {missing}"

        for name in sorted(UNGATED_BUT_ROLE_DOCUMENTED):
            upd = MagicMock()
            upd.effective_user.id = 999000001
            upd.effective_user.username = "plainmember"
            upd.effective_user.full_name = "Plain Member"
            upd.effective_chat.id = 999000001
            upd.message.reply_text = AsyncMock()
            with patch.object(permissions, "is_admin", lambda u: False), \
                 patch.object(permissions, "_is_known_official", lambda *a: False), \
                 patch.object(permissions, "is_prayer_admin", lambda u: False), \
                 patch.object(ub, "get_user_prefs",
                              AsyncMock(return_value=(None, "en"))), \
                 patch.object(ub.activity, "log_command", lambda *a, **k: None):
                await handlers[name](upd, MagicMock())
            said = " ".join(str(c.args[0]) for c in
                            upd.message.reply_text.call_args_list if c.args)
            assert said, f"/{name} replied nothing to a plain member"
            assert "Unknown command" not in said, (
                f"/{name} is exempted from the overlap rule as ungated, but it "
                f"refused a plain member: {said!r}"
            )

    def test_the_known_gated_families_are_absent(self):
        """Named explicitly, so a gated command that stops appearing in a help
        block does not quietly become publishable."""
        public = _public_names()
        for name in ("boarditems", "addboarditem", "boardcalendar",
                     "setboardofficer", "prayerrequests", "respondprayer",
                     "dismissprayer", "addevent", "modifyevent", "deleteevent",
                     "setservicelink", "addannouncement", "backup", "stats",
                     "userlist", "usercount", "adminhelp"):
            assert name not in public, f"/{name} is gated but publicly listed"


class TestEveryPublicCommandIsReachable:
    """A menu entry whose handler is not registered is a dead button.

    This is vacuity pattern #4 in reverse — asserting registration without
    reachability. Here the risk is the opposite: advertising reachability that
    does not exist.
    """

    def test_each_one_has_a_handler(self):
        from telegram.ext import CommandHandler, ConversationHandler

        from tests.test_board_wiring import _handlers

        registered: set[str] = set()
        for _group, h in _handlers():
            if isinstance(h, ConversationHandler):
                for entry in h.entry_points:
                    registered |= set(getattr(entry, "commands", None) or [])
            elif isinstance(h, CommandHandler):
                registered |= set(getattr(h, "commands", None) or [])

        assert registered, "no command handlers found — this test sees nothing"
        missing = _public_names() - registered
        assert not missing, (
            f"advertised in the menu but not registered: {sorted(missing)}"
        )
