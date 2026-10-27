"""Registration order and reachability for the board handlers.

Registration order in bot.py is load-bearing: PTB runs only the FIRST matching
handler per group, and bot.py ends with two catch-alls — a free-text handler
and an unknown-command handler — that match almost anything. A board handler
registered after either of them is dead code that still looks registered, which
is exactly how /prayer was silently broken once.

The stale-button entry point gets its own test. user_data does not survive a
restart and every deploy restarts the bot, so without the callback router in
entry_points a keyboard already on an officer's screen goes dead and tapping it
does nothing at all — no error, no reply.
"""

from __future__ import annotations

import sys
import warnings
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
)

sys.path.insert(0, str(Path(__file__).parent.parent))


def _handlers():
    """Every handler main() registers, in registration order."""
    import bot

    class FakeApp:
        def __init__(self):
            self.handlers = []
            self.job_queue = MagicMock()
            self.bot = MagicMock()
            self.bot.set_my_commands = AsyncMock()

        def add_handler(self, h, group=0):
            self.handlers.append((group, h))

        def add_error_handler(self, h):
            pass

        def run_polling(self, **kw):
            pass

    fake = FakeApp()

    class FakeBuilder:
        def token(self, t):
            return self

        def post_init(self, f):
            return self

        def build(self):
            return fake

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with patch.object(bot.Application, "builder",
                          staticmethod(lambda: FakeBuilder())):
            bot.main()
    return fake.handlers


def _commands(handler) -> set:
    return set(getattr(handler, "commands", None) or [])


def _index_of_command(handlers, name) -> int:
    for i, (_group, h) in enumerate(handlers):
        if isinstance(h, ConversationHandler):
            if any(name in _commands(e) for e in h.entry_points):
                return i
        elif isinstance(h, CommandHandler) and name in _commands(h):
            return i
    raise AssertionError(f"/{name} is not registered at all")


#: The two handlers at the end of group 0 that match almost anything. Named
#: explicitly rather than detected by type: MessageHandler(filters.CONTACT) is
#: also a group-0 MessageHandler and sits near the TOP, so a type-based sweep
#: finds it first and the ordering assertion becomes nonsense.
CATCH_ALL_CALLBACKS = {"handle_counter_propose_message", "cmd_unknown"}


def _catch_all_indices(handlers) -> list[int]:
    """Where the trailing group-0 catch-alls sit."""
    found = [i for i, (group, h) in enumerate(handlers)
             if group == 0 and isinstance(h, MessageHandler)
             and getattr(h.callback, "__name__", "") in CATCH_ALL_CALLBACKS]
    assert len(found) == len(CATCH_ALL_CALLBACKS), (
        "the catch-alls were renamed or removed; this test's ordering check "
        f"is measuring nothing. Found {len(found)}, expected "
        f"{len(CATCH_ALL_CALLBACKS)}"
    )
    return found


class TestTheSweepSeesTheWiring:
    def test_handlers_were_collected(self):
        """Guards every test below: a sweep over nothing passes vacuously."""
        assert len(_handlers()) >= 25

    def test_there_are_catch_alls_to_be_registered_before(self):
        assert _catch_all_indices(_handlers()), "no catch-all found to order against"


class TestBoardCommandsAreRegistered:
    def test_every_board_command_exists(self):
        handlers = _handlers()
        for name in ("boarditems", "addboarditem", "setboardofficer"):
            _index_of_command(handlers, name)   # raises if missing

    def test_they_come_before_the_catch_alls(self):
        """A handler after the free-text or unknown-command catch-all is
        unreachable — registered, inert, and silent about it."""
        handlers = _handlers()
        first_catch_all = min(_catch_all_indices(handlers))
        for name in ("boarditems", "addboarditem", "setboardofficer"):
            assert _index_of_command(handlers, name) < first_catch_all, name


class TestBoardConversations:
    @staticmethod
    def _conv(handlers, name):
        for _group, h in handlers:
            if isinstance(h, ConversationHandler) and any(
                    name in _commands(e) for e in h.entry_points):
                return h
        raise AssertionError(f"no conversation entered by /{name}")

    def test_both_board_conversations_are_reentrant(self):
        handlers = _handlers()
        for name in ("boarditems", "addboarditem"):
            assert self._conv(handlers, name).allow_reentry is True, name

    def test_a_stale_button_can_re_enter_the_queue(self):
        """The reason this is an entry point and not only a state handler."""
        conv = self._conv(_handlers(), "boarditems")
        assert any(isinstance(e, CallbackQueryHandler) for e in conv.entry_points), (
            "no CallbackQueryHandler entry point: after a restart, a keyboard "
            "already on an officer's screen is dead"
        )

    def test_the_stale_entry_matches_the_board_callback_data(self):
        import handlers.board as hb
        conv = self._conv(_handlers(), "boarditems")
        entry = next(e for e in conv.entry_points
                     if isinstance(e, CallbackQueryHandler))
        assert entry.pattern.match(f"{hb.CB_BOARD_PREFIX}show:AAA11111")

    def test_the_stale_entry_does_not_swallow_other_features_buttons(self):
        """A greedy pattern here would steal taps from appointments, language,
        notification preferences and the broadcast flow."""
        import handlers.board as hb
        conv = self._conv(_handlers(), "boarditems")
        entry = next(e for e in conv.entry_points
                     if isinstance(e, CallbackQueryHandler))
        for foreign in ("np:toggle:convocations", "bc:send", "tz:America/New_York",
                        "lang:en", f"{hb.CB_BOFF_PREFIX}toggle:111"):
            assert not entry.pattern.match(foreign), foreign

    def test_every_board_state_can_be_left(self):
        """Without a fallback an officer who changes their mind is stuck in the
        conversation, and /cancel is the only documented way out."""
        handlers = _handlers()
        for name in ("boarditems", "addboarditem"):
            conv = self._conv(handlers, name)
            assert any("cancel" in _commands(f) for f in conv.fallbacks), name

    def test_every_skip_prompt_has_a_skip_handler(self):
        """The add flow tells the officer to send /skip three times. A state
        whose prompt offers /skip but whose handlers are TEXT & ~COMMAND would
        drop it into the unknown-command catch-all."""
        import handlers.board as hb
        conv = self._conv(_handlers(), "addboarditem")
        for state in (hb.AB_NOTIFY, hb.AB_DESC, hb.AB_URL):
            assert any("skip" in _commands(h) for h in conv.states[state]), state

    def test_the_edit_and_next_due_states_accept_skip_too(self):
        import handlers.board as hb
        conv = self._conv(_handlers(), "boarditems")
        for state in (hb.BI_EDIT_VALUE, hb.BI_NEXT_DUE):
            assert any("skip" in _commands(h) for h in conv.states[state]), state


class TestStateNumbersDoNotCollide:
    def test_board_states_are_distinct_from_each_other(self):
        import handlers.board as hb
        states = [hb.BI_MENU, hb.BI_EDIT_VALUE, hb.BI_NEXT_DUE, hb.AB_ACTION,
                  hb.AB_DUE, hb.AB_NOTIFY, hb.AB_DESC, hb.AB_URL, hb.AB_CONFIRM]
        assert len(set(states)) == len(states)

    def test_board_states_do_not_collide_with_another_conversation(self):
        """States are plain ints shared across the whole module. An overlap
        does not fail loudly — it silently routes one conversation's reply into
        another's handler. A state number is also why an earlier test
        identified the wrong conversation: AN_TITLE and AE_DURATION are both 3.
        """
        import handlers.announcements as han
        import handlers.board as hb
        import handlers.prayer as hp
        board = {hb.BI_MENU, hb.BI_EDIT_VALUE, hb.BI_NEXT_DUE, hb.AB_ACTION,
                 hb.AB_DUE, hb.AB_NOTIFY, hb.AB_DESC, hb.AB_URL, hb.AB_CONFIRM}
        others = {han.AN_TITLE, han.AN_BODY, han.AN_MEDIA, han.AN_LINK,
                  han.AN_EXPIRES, han.AN_CONFIRM, han.DA_SELECT,
                  hp.PR_TEXT, hp.PR_CONFIRM, hp.RESP_TEXT}
        assert not board & others, sorted(board & others)


class TestTheOfficerPickerIsReachable:
    def test_the_toggle_callback_is_registered_outside_a_conversation(self):
        """/setboardofficer is not a conversation, so its buttons need a
        top-level CallbackQueryHandler or they do nothing."""
        import handlers.board as hb
        matched = [h for _g, h in _handlers()
                   if isinstance(h, CallbackQueryHandler)
                   and h.pattern
                   and h.pattern.match(f"{hb.CB_BOFF_PREFIX}toggle:111")]
        assert matched, "no handler matches the board-officer toggle buttons"

    def test_that_handler_does_not_also_match_board_item_buttons(self):
        import handlers.board as hb
        matched = [h for _g, h in _handlers()
                   if isinstance(h, CallbackQueryHandler)
                   and h.pattern
                   and h.pattern.match(f"{hb.CB_BOFF_PREFIX}toggle:111")]
        for h in matched:
            assert not h.pattern.match(f"{hb.CB_BOARD_PREFIX}show:AAA11111")
