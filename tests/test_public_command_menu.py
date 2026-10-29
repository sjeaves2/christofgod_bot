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


def _gated_names() -> set[str]:
    """Commands the role-gated help blocks advertise to their holders only."""
    import handlers.user_basics as ub
    names: set[str] = set()
    for block in (ub.ADMIN_COMMANDS_TEXT, ub.PRAYER_COMMANDS_TEXT,
                  ub.BOARD_COMMANDS_TEXT):
        names |= set(re.findall(r"^/([a-z_]+)", block, re.M))
    return names


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
