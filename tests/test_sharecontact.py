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
    def test_the_admin_help_text_mentions_it(self):
        """An admin who needs this has no way to discover it otherwise."""
        assert "/sharecontact" in ub.ADMIN_COMMANDS_TEXT
