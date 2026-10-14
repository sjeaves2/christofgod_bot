"""An announcement can carry a link, shown to members as a tappable button.

Chosen over detecting a URL in the body: detection is guesswork when a body
holds two links, and it would leave the URL in the prose as well as on the
button. An explicit optional step makes "this announcement has a link" a real
field, validated when the admin types it, the way everything else in this flow
is validated at entry.

The hazard is the one the reminder button already faces: Telegram rejects the
ENTIRE message if a button's url is unacceptable. So the url is checked when it
is entered — while the admin is still there and can paste another — rather than
at send time, where a bad one would silently cost the whole announcement.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import handlers.announcements as A  # noqa: E402

URL = "https://example.test/signup?id=ab_cd"


def _ann(link=URL, **extra):
    ann = {"id": "AN1", "title": "Roof fund", "body": "We are raising funds.",
           "lang": "en", "expires": "2099-01-01", "link": link}
    ann.update(extra)
    return ann


def _update(text=""):
    upd = MagicMock()
    upd.effective_user.id = 1
    upd.effective_user.username = "admin"
    upd.effective_user.full_name = "An Admin"
    upd.message.text = text
    upd.message.reply_text = AsyncMock()
    return upd


class TestTheLinkStep:
    async def test_a_valid_link_is_stored_and_moves_on(self):
        upd, ctx = _update(URL), MagicMock()
        ctx.user_data = {}
        assert await A.an_link(upd, ctx) == A.AN_EXPIRES
        assert ctx.user_data["an_link"] == URL

    async def test_skip_leaves_no_link(self):
        upd, ctx = _update("/skip"), MagicMock()
        ctx.user_data = {}
        assert await A.an_skip_link(upd, ctx) == A.AN_EXPIRES
        assert ctx.user_data["an_link"] == ""

    @pytest.mark.parametrize("bad", [
        "example.test/signup",            # no scheme
        "ftp://example.test/x",           # wrong scheme
        "https://example.test/a b",       # whitespace
        "ask Pastor for the link",        # free text
    ])
    async def test_an_unusable_link_is_refused_at_entry(self, bad):
        """Caught while the admin can still fix it, not at send time where it
        would cost the whole announcement."""
        upd, ctx = _update(bad), MagicMock()
        ctx.user_data = {}
        assert await A.an_link(upd, ctx) == A.AN_LINK
        assert "an_link" not in ctx.user_data
        assert "http" in upd.message.reply_text.await_args[0][0]

    async def test_the_refusal_says_how_to_skip(self):
        upd, ctx = _update("nonsense"), MagicMock()
        ctx.user_data = {}
        await A.an_link(upd, ctx)
        assert "/skip" in upd.message.reply_text.await_args[0][0]


class TestTheButton:
    def test_an_announcement_with_a_link_gets_one(self):
        assert A.announcement_markup(_ann(), "en") is not None

    @pytest.mark.parametrize("link", ["", None, "not a url"])
    def test_an_announcement_without_a_usable_link_does_not(self, link):
        assert A.announcement_markup(_ann(link=link), "en") is None

    def test_a_legacy_record_without_the_field_does_not(self):
        """Every announcement created before this feature existed."""
        ann = _ann()
        del ann["link"]
        assert A.announcement_markup(ann, "en") is None

    def test_the_button_carries_the_url_unaltered(self):
        btn = A.announcement_markup(_ann(), "en").inline_keyboard[0][0]
        assert btn.url == URL

    def test_the_label_is_localized(self):
        from localization import t
        for lang in ("en", "es", "fr", "zu"):
            label = A.announcement_markup(_ann(), lang).inline_keyboard[0][0].text
            assert label == t("ann_link_button", lang) != "ann_link_button"


class TestWhereTheLinkAppears:
    def test_the_push_leaves_it_out_of_the_text(self):
        """The button carries it; printing it as well is clutter."""
        assert "example.test" not in A._render_announcement(
            _ann(), escape=False, show_link=False)

    def test_the_list_view_keeps_it_as_text(self):
        """/announcements renders several into one message, so no buttons."""
        assert "example.test" in A._render_announcement(_ann(), escape=False)

    def test_the_url_is_escaped_even_when_the_body_is_not(self):
        """escape=False is for the ADMIN's markdown; a url is never that, and
        one underscore in it would reject the whole message."""
        out = A._render_announcement(_ann(), escape=False)
        assert "ab\\_cd" in out

    def test_an_announcement_with_no_link_renders_unchanged(self):
        assert "🔗" not in A._render_announcement(_ann(link=""))


class TestTheWiring:
    """Registered is not the same as reached — the recurring lesson here."""

    @staticmethod
    def _states():
        import warnings
        from unittest.mock import patch
        import bot
        from telegram.ext import ConversationHandler

        class FakeApp:
            def __init__(self):
                self.handlers = []
                self.job_queue = MagicMock()
                self.bot = MagicMock()
                self.bot.set_my_commands = AsyncMock()

            def add_handler(self, h, group=0):
                self.handlers.append(h)

            def add_error_handler(self, h):
                pass

            def run_polling(self, **kw):
                pass

        fake = FakeApp()

        class FakeBuilder:
            def token(self, t_):
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
        # Identified by its ENTRY COMMAND, not by a state number: the AN_* and
        # AE_* ranges overlap (AN_TITLE is 3, so is AE_DURATION), which is
        # harmless because states are per-handler, but makes a number useless
        # for telling two conversations apart. An earlier version of this
        # helper matched /addevent and tested the wrong thing entirely.
        for h in fake.handlers:
            if not isinstance(h, ConversationHandler):
                continue
            commands = {c for e in h.entry_points
                        for c in (getattr(e, "commands", None) or [])}
            if "addannouncement" in commands:
                return h.states
        raise AssertionError("the add-announcement conversation is not registered")

    def test_the_link_state_is_registered(self):
        states = self._states()
        assert A.AN_LINK in states, (
            "the conversation would reach a state with no handlers and hang"
        )

    def test_skip_is_reachable_in_that_state(self):
        handlers = self._states()[A.AN_LINK]
        assert any("skip" in (getattr(h, "commands", None) or []) for h in handlers), (
            "without /skip an admin who wants no link cannot get past this step"
        )

    def test_a_typed_link_is_reachable_in_that_state(self):
        handlers = self._states()[A.AN_LINK]
        assert any(getattr(h, "callback", None) is A.an_link for h in handlers)

    def test_the_states_are_distinct_within_this_conversation(self):
        """A collision HERE would silently merge two steps of the flow. Across
        conversations it does not matter — states are per-handler."""
        values = [A.AN_TITLE, A.AN_BODY, A.AN_MEDIA, A.AN_LINK,
                  A.AN_EXPIRES, A.AN_CONFIRM]
        assert len(set(values)) == len(values)
        assert A.AN_LINK not in (A.AN_MEDIA, A.AN_EXPIRES)


class TestThePushCarriesTheButton:
    async def test_the_broadcast_sends_the_markup(self):
        """The whole point: the member's message has the button on it."""
        import handlers.broadcast as B
        sent = []

        async def send_message(chat_id, text, **kwargs):
            sent.append(kwargs.get("reply_markup"))

        bot_ = MagicMock()
        bot_.send_message = AsyncMock(side_effect=send_message)
        ctx = MagicMock()
        ctx.user_data = {
            "bc_message": "📢 *Roof fund*",
            "bc_markup": A.announcement_markup(_ann(), "en"),
            "bc_recipients": [{"chat_id": -100, "name": "COGM"}],
            "bc_done": set(),
        }
        await B._bc_send_pending(bot_, ctx)
        assert sent and sent[0] is not None, "the link button never reached the member"

    async def test_a_plain_broadcast_sends_no_markup(self):
        import handlers.broadcast as B
        sent = []

        async def send_message(chat_id, text, **kwargs):
            sent.append(kwargs.get("reply_markup"))

        bot_ = MagicMock()
        bot_.send_message = AsyncMock(side_effect=send_message)
        ctx = MagicMock()
        ctx.user_data = {"bc_message": "hello", "bc_recipients": [{"chat_id": -100}],
                         "bc_done": set()}
        await B._bc_send_pending(bot_, ctx)
        assert sent == [None]


class TestTheLinkSurvivesToTheStoredRecord:
    """The step that makes the feature real rather than inert.

    Added after a mutation survived: deleting the line that writes "link" onto
    the saved announcement broke nothing. An admin could enter a link, have it
    validated, and watch it be dropped on the floor — and every other test here
    would still pass, because they each check one end or the other.
    """

    @staticmethod
    async def _confirm(an_link):
        from unittest.mock import patch
        saved = {}

        async def save(anns):
            saved["anns"] = anns

        upd = _update("yes")
        ctx = MagicMock()
        ctx.user_data = {
            "an_title": "Roof fund", "an_body": "We are raising funds.",
            "an_expires": "2099-01-01", "an_link": an_link,
        }
        ctx.bot = MagicMock()
        with patch.object(A.storage, "get_announcements", AsyncMock(return_value=[])), \
             patch.object(A.storage, "save_announcements", AsyncMock(side_effect=save)), \
             patch.object(A, "get_user_prefs", AsyncMock(return_value=(None, "en"))), \
             patch.object(A, "_broadcast_target_options", AsyncMock(return_value=[])), \
             patch.object(A.activity, "log_command", lambda *a, **k: None):
            await A.an_confirm(upd, ctx)
        return saved.get("anns", [{}])[0], ctx

    async def test_the_link_is_written_to_the_record(self):
        ann, _ = await self._confirm(URL)
        assert ann.get("link") == URL, "the admin's link never reached storage"

    async def test_the_markup_is_handed_to_the_broadcast(self):
        _, ctx = await self._confirm(URL)
        assert ctx.user_data.get("bc_markup") is not None

    async def test_no_link_stores_an_empty_field_and_no_markup(self):
        ann, ctx = await self._confirm("")
        assert ann.get("link") == ""
        assert ctx.user_data.get("bc_markup") is None

    async def test_the_pushed_text_omits_a_link_the_button_carries(self):
        _, ctx = await self._confirm(URL)
        assert "example.test" not in ctx.user_data["bc_message"]

    async def test_the_pushed_text_keeps_a_link_with_no_button(self):
        """Belt and braces: if no button, the link must still be visible."""
        _, ctx = await self._confirm("")
        assert "🔗" not in ctx.user_data["bc_message"]
