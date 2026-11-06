"""/sharecontact — the on-demand route to the contact-share keyboard.

Why the command exists: /start offers that keyboard only to callers it does
NOT already recognise, which means an admin or official matched by username
can never reach it. Adding a `phone:` to admins.yaml for such a person is then
inert, because the only thing that writes a phone into data/users.yaml is a
contact share (bot.py handle_contact). The phone looks configured and matches
nothing.

The behaviour that matters is narrow and easy to break silently:

  * the keyboard must carry request_contact=True — a button without it sends
    plain text and no contact ever arrives;
  * it must be offered REGARDLESS of whether the caller is already known,
    which is the single thing that distinguishes this from /start;
  * it must be reachable, i.e. registered before bot.py's catch-alls.
"""

from __future__ import annotations

import sys
import warnings
from pathlib import Path
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram import KeyboardButton, ReplyKeyboardMarkup
from telegram.ext import CommandHandler, ConversationHandler, MessageHandler

sys.path.insert(0, str(Path(__file__).parent.parent))

import permissions
from handlers import user_basics as ub


def _update(uid=111, username="someone"):
    upd = MagicMock()
    upd.effective_chat.id = uid
    upd.effective_user.id = uid
    upd.effective_user.username = username
    upd.effective_user.full_name = "Church Friend"
    upd.message.reply_text = AsyncMock()
    return upd


async def _run(upd, lang="en"):
    """Call the command and return the single reply's (text, markup)."""
    with patch("handlers.user_basics.get_user_prefs",
               AsyncMock(return_value=(None, lang))):
        await ub.cmd_sharecontact(upd, MagicMock())
    assert upd.message.reply_text.await_count == 1, (
        "expected exactly one reply; got "
        f"{upd.message.reply_text.await_count}"
    )
    kwargs = upd.message.reply_text.await_args.kwargs
    args = upd.message.reply_text.await_args.args
    text = kwargs.get("text", args[0] if args else None)
    return text, kwargs.get("reply_markup")


def _buttons(markup):
    assert isinstance(markup, ReplyKeyboardMarkup), (
        f"expected a ReplyKeyboardMarkup, got {type(markup).__name__}"
    )
    return [b for row in markup.keyboard for b in row]


class TestTheKeyboard:
    async def test_the_button_requests_a_contact(self):
        """Without request_contact the button is decorative."""
        _text, markup = await _run(_update())
        buttons = _buttons(markup)
        assert len(buttons) == 1
        assert isinstance(buttons[0], KeyboardButton)
        assert buttons[0].request_contact is True

    async def test_a_prompt_is_sent_with_it(self):
        text, _markup = await _run(_update())
        assert text and text.strip(), "the keyboard arrived with no prompt text"

    async def test_the_keyboard_is_one_time_and_resized(self):
        _text, markup = await _run(_update())
        assert markup.one_time_keyboard is True
        assert markup.resize_keyboard is True


class TestItIgnoresWhoIsAsking:
    """The one thing that distinguishes this from /start.

    /start gates the keyboard on `already_known`; this must not. Each case
    patches the identity predicates so the command is called as a recognised
    admin, a recognised official, and a stranger.
    """

    @pytest.mark.parametrize("is_adm,is_official", [
        (True, False),    # the case the command exists for
        (False, True),
        (True, True),
        (False, False),
    ])
    async def test_the_keyboard_is_offered_either_way(self, is_adm, is_official):
        with patch.object(permissions, "is_admin", return_value=is_adm), \
             patch.object(permissions, "_is_known_official",
                          return_value=is_official):
            _text, markup = await _run(_update())
        assert _buttons(markup)[0].request_contact is True

    async def test_an_already_known_admin_still_gets_it(self):
        """Stated separately because this is the regression being fixed.

        Before /sharecontact existed, this person had NO route to the
        keyboard: /start recognised them and skipped it.
        """
        with patch.object(permissions, "is_admin", return_value=True):
            _text, markup = await _run(_update(username="bishop"))
        assert _buttons(markup)[0].request_contact is True


class TestLocalisation:
    @pytest.mark.parametrize("lang", ["en", "es", "fr", "zu"])
    async def test_every_shipped_language_gets_a_prompt_and_a_button(self, lang):
        """No new strings were added; these two keys must exist everywhere."""
        text, markup = await _run(_update(), lang=lang)
        assert text and text.strip(), f"no prompt text for {lang}"
        label = _buttons(markup)[0].text
        assert label and label.strip(), f"no button label for {lang}"

    async def test_two_languages_actually_differ(self):
        """Guards against t() silently falling back to English for all four,
        which would make the parametrised test above vacuous."""
        en_text, en_markup = await _run(_update(), lang="en")
        zu_text, zu_markup = await _run(_update(), lang="zu")
        assert (en_text, _buttons(en_markup)[0].text) != \
               (zu_text, _buttons(zu_markup)[0].text)


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


#: Same reasoning as tests/test_board_wiring.py: named explicitly, because
#: MessageHandler(filters.CONTACT) is also a group-0 MessageHandler sitting
#: near the top, so a type-based sweep finds it first.
CATCH_ALL_CALLBACKS = {"handle_counter_propose_message", "cmd_unknown"}


class TestItIsReachable:
    def test_the_sweep_sees_the_wiring(self):
        """Guards the two tests below from passing vacuously."""
        assert len(_handlers()) >= 25

    def test_sharecontact_is_registered(self):
        handlers = _handlers()
        found = [h for _g, h in handlers
                 if isinstance(h, CommandHandler) and "sharecontact" in _commands(h)]
        assert len(found) == 1, (
            f"/sharecontact is registered {len(found)} times; expected once"
        )
        assert found[0].callback is ub.cmd_sharecontact

    def test_it_is_registered_before_the_catch_alls(self):
        """A command registered after cmd_unknown is dead code that still
        looks registered — how /prayer was silently broken once."""
        handlers = _handlers()
        idx = None
        for i, (_g, h) in enumerate(handlers):
            if isinstance(h, ConversationHandler):
                if any("sharecontact" in _commands(e) for e in h.entry_points):
                    idx = i
                    break
            elif isinstance(h, CommandHandler) and "sharecontact" in _commands(h):
                idx = i
                break
        assert idx is not None, "/sharecontact is not registered at all"

        catch_alls = [i for i, (group, h) in enumerate(handlers)
                      if group == 0 and isinstance(h, MessageHandler)
                      and getattr(h.callback, "__name__", "") in CATCH_ALL_CALLBACKS]
        assert len(catch_alls) == len(CATCH_ALL_CALLBACKS), (
            "the catch-alls were renamed or removed; this ordering check is "
            f"measuring nothing (found {len(catch_alls)})"
        )
        assert idx < min(catch_alls), (
            f"/sharecontact is registered at {idx}, after a catch-all at "
            f"{min(catch_alls)} — it would never run"
        )


class TestItIsDocumented:
    """Discoverability, which this command lacked until v0.28.9.

    It began life in ADMIN_COMMANDS_TEXT only. That was wrong twice over: an
    official who is not an admin could run it but never see it, and the people
    it exists for are precisely those whose config entry carries a `phone:` —
    officials included. It now appears in the officials block, where the
    explanation belongs, and in the Telegram menu, where everyone can find it.
    """

    def test_the_officials_block_explains_it(self):
        assert "/sharecontact" in ub.OFFICIAL_COMMANDS_TEXT

    def test_it_is_not_duplicated_in_the_admin_block(self):
        """All current admins are also officials, so listing it in both would
        print it twice in one /help for every one of them."""
        assert "/sharecontact" not in ub.ADMIN_COMMANDS_TEXT

    def test_the_telegram_menu_advertises_it(self):
        """The route for an admin who is not an official, and for members."""
        import bot
        assert "sharecontact" in {c.command for c in bot.PUBLIC_COMMANDS}

    def test_an_official_who_is_not_an_admin_sees_it(self):
        """The gap that prompted all of this, stated as the real assembly."""
        text = ub._commands_text("en", is_adm=False, is_prayer=False,
                                 is_board=False, is_official=True)
        assert "/sharecontact" in text
        assert "/adminhelp" not in text, "the admin block leaked in"

    def test_an_ordinary_member_does_not_get_the_officials_block(self):
        text = ub._commands_text("en", is_adm=False, is_prayer=False,
                                 is_board=False, is_official=False)
        assert "/enable_appt_proxies" not in text
        assert "*Officials:*" not in text

    def test_the_officials_block_documents_the_proxy_command_too(self):
        """It was documented NOWHERE before this block existed — not in any
        help text and not in the Telegram menu."""
        assert "/enable_appt_proxies" in ub.OFFICIAL_COMMANDS_TEXT


class TestTheBlockReachesPeopleThroughTheRealCommands:
    """Every entry point that assembles a command list, driven for real.

    Found by mutation: hard-coding `is_official=False` at all three call
    sites — cmd_help, cmd_start and bot.handle_contact — passed the ENTIRE
    suite of 2247 tests. Asserting on `_commands_text` proves the block can
    be assembled; it proves nothing about anyone ever receiving it, and a
    threaded argument that no test supplies is an argument that can be
    dropped silently.

    `is_board_officer` is patched because it is async and reads the real
    data/users.yaml; everything else is patched to put a plain official in
    front of the handler.
    """

    @staticmethod
    def _as_official():
        """Patches making the caller an official and nothing else."""
        return [
            patch.object(ub.permissions, "_is_known_official", lambda *a: True),
            patch.object(ub.permissions, "is_admin", lambda u: False),
            patch.object(ub.permissions, "is_prayer_admin", lambda u: False),
            patch.object(ub.permissions, "is_board_officer",
                         AsyncMock(return_value=False)),
            patch.object(ub, "get_user_prefs", AsyncMock(return_value=(None, "en"))),
            patch.object(ub.activity, "log_command", lambda *a, **k: None),
        ]

    @staticmethod
    def _replies(upd):
        return " ".join(
            str(c.args[0]) for c in upd.message.reply_text.call_args_list if c.args
        )

    async def test_help_shows_it_to_an_official(self):
        """/help is where people actually look."""
        upd, ctx = _update(), MagicMock()
        ctx.args = []
        with ExitStack() as stack:
            for p in self._as_official():
                stack.enter_context(p)
            await ub.cmd_help(upd, ctx)
        text = self._replies(upd)
        assert "/sharecontact" in text, "an official running /help cannot see it"
        assert "/adminhelp" not in text, "the admin block leaked to a non-admin"

    async def test_start_shows_it_to_an_official(self):
        upd, ctx = _update(), MagicMock()
        ctx.args = []
        with ExitStack() as stack:
            for p in self._as_official():
                stack.enter_context(p)
            stack.enter_context(patch.object(
                ub.storage, "get_all_users",
                AsyncMock(return_value=[{"chat_id": 111, "username": "someone"}])))
            stack.enter_context(patch.object(ub.storage, "save_users", AsyncMock()))
            stack.enter_context(patch.object(ub, "_register_official_if_known",
                                             AsyncMock()))
            stack.enter_context(patch.object(ub.permissions,
                                             "_register_admin_by_username",
                                             AsyncMock()))
            await ub.cmd_start(upd, ctx)
        assert "/sharecontact" in self._replies(upd)

    async def test_the_contact_share_reply_shows_it_to_an_official(self):
        """bot.handle_contact — the moment an official has just identified
        themselves is exactly when the officials block is most useful."""
        import bot
        upd = _update()
        upd.message.contact.user_id = upd.effective_user.id
        upd.message.contact.phone_number = "+15550001234"
        ctx = MagicMock()
        with ExitStack() as stack:
            stack.enter_context(patch.object(
                bot.permissions, "_is_known_official", lambda *a: True))
            stack.enter_context(patch.object(bot.permissions, "is_admin",
                                             lambda u: False))
            stack.enter_context(patch.object(bot.permissions, "is_prayer_admin",
                                             lambda u: False))
            stack.enter_context(patch.object(bot.permissions, "is_board_officer",
                                             AsyncMock(return_value=False)))
            stack.enter_context(patch.object(bot.permissions,
                                             "_register_admin_by_phone", AsyncMock()))
            stack.enter_context(patch.object(bot, "_register_official_if_known",
                                             AsyncMock()))
            stack.enter_context(patch.object(bot, "get_user_prefs",
                                             AsyncMock(return_value=(None, "en"))))
            stack.enter_context(patch.object(
                bot.storage, "get_all_users",
                AsyncMock(return_value=[{"chat_id": 111}])))
            stack.enter_context(patch.object(bot.storage, "save_users", AsyncMock()))
            stack.enter_context(patch.object(bot.activity, "log_command",
                                             lambda *a, **k: None))
            await bot.handle_contact(upd, ctx)
        assert "/sharecontact" in self._replies(upd)

    async def test_an_ordinary_member_gets_none_of_it_through_help(self):
        """Paired guard: the three tests above would pass just as well if the
        block were appended unconditionally."""
        upd, ctx = _update(), MagicMock()
        ctx.args = []
        with patch.object(ub.permissions, "_is_known_official", lambda *a: False), \
             patch.object(ub.permissions, "is_admin", lambda u: False), \
             patch.object(ub.permissions, "is_prayer_admin", lambda u: False), \
             patch.object(ub.permissions, "is_board_officer",
                          AsyncMock(return_value=False)), \
             patch.object(ub, "get_user_prefs", AsyncMock(return_value=(None, "en"))), \
             patch.object(ub.activity, "log_command", lambda *a, **k: None):
            await ub.cmd_help(upd, ctx)
        text = self._replies(upd)
        assert "*Officials:*" not in text
        assert "/enable_appt_proxies" not in text
