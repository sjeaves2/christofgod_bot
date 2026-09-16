"""/privacy — what the bot stores, and a link to the full policy.

Exists as a command rather than relying on BotFather's privacy-policy field:
discoverable in /help and the command menu, works on every client, and under
our control. Added 2026-09-16 alongside PRIVACY.md.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import handlers.user_basics as UB  # noqa: E402
import settings  # noqa: E402
from localization import AVAILABLE_LANGUAGES, t  # noqa: E402

LANGS = list(AVAILABLE_LANGUAGES)


def _update():
    upd = MagicMock()
    upd.effective_user.id = 7
    upd.effective_user.username = "member"
    upd.effective_user.full_name = "A Member"
    upd.message.reply_text = AsyncMock()
    return upd


async def _invoke(lang="en"):
    upd = _update()
    with patch.object(UB, "get_user_prefs", AsyncMock(return_value=(None, lang))), \
         patch.object(UB.activity, "log_command", lambda *a, **k: None):
        await UB.cmd_privacy(upd, MagicMock())
    return upd.message.reply_text.call_args


class TestPrivacyReply:
    async def test_replies_with_a_link_button(self):
        call = await _invoke()
        kb = call.kwargs["reply_markup"]
        button = kb.inline_keyboard[0][0]
        assert button.url == settings.PRIVACY_URL

    async def test_mentions_how_to_delete_your_data(self):
        """The policy's most actionable fact must be in the message itself,
        not hidden behind a link the member may not open."""
        text = (await _invoke()).args[0]
        assert "/stop" in text

    async def test_says_group_conversation_is_not_read(self):
        text = (await _invoke()).args[0]
        assert "group" in text.lower()

    async def test_logged_for_stats(self):
        upd = _update()
        with patch.object(UB, "get_user_prefs", AsyncMock(return_value=(None, "en"))), \
             patch.object(UB.activity, "log_command") as log:
            await UB.cmd_privacy(upd, MagicMock())
        assert log.call_args[0][0] == "privacy"


class TestPrivacyLocalisation:
    @pytest.mark.parametrize("lang", LANGS)
    async def test_every_language_has_a_message_and_button(self, lang):
        call = await _invoke(lang)
        assert call.args[0].strip(), f"{lang}: empty privacy message"
        assert call.kwargs["reply_markup"].inline_keyboard[0][0].text.strip()

    @pytest.mark.parametrize("lang", LANGS)
    async def test_stop_is_mentioned_in_every_language(self, lang):
        """A member who cannot read English still needs to know how to leave."""
        assert "/stop" in (await _invoke(lang)).args[0]

    @pytest.mark.parametrize("lang", LANGS)
    async def test_translations_are_not_just_the_english(self, lang):
        """Guard against a placeholder copy-paste that never got translated."""
        if lang == "en":
            return
        assert t("privacy_message", lang) != t("privacy_message", "en")
        assert t("privacy_button", lang) != t("privacy_button", "en")

    async def test_the_handler_actually_uses_the_users_language(self):
        """Comparing locale strings is not enough — the handler must pass the
        user's language through. A hardcoded "en" would pass a string-level
        comparison while every member still received English."""
        en = (await _invoke("en")).args[0]
        for lang in LANGS:
            if lang == "en":
                continue
            assert (await _invoke(lang)).args[0] != en, (
                f"{lang} reply is identical to English — the handler is probably "
                "not passing lang through"
            )

    @pytest.mark.parametrize("lang,heading", [
        ("en", "Privacy"), ("es", "Privacidad"),
        ("fr", "Confidentialité"), ("zu", "Ubumfihlo"),
    ])
    async def test_heading_is_in_the_right_language(self, lang, heading):
        """Pins the specific wording, so a PARTIAL regression — one line
        reverted to English — is caught, not just a wholesale copy."""
        assert heading in (await _invoke(lang)).args[0]

    @pytest.mark.parametrize("lang", LANGS)
    async def test_command_list_advertises_privacy(self, lang):
        assert "/privacy" in t("user_commands", lang)

    @pytest.mark.parametrize("lang", LANGS)
    async def test_help_topic_exists(self, lang):
        assert t("help_privacy", lang).strip()


class TestPrivacyUrl:
    def test_defaults_to_the_policy_in_this_repository(self):
        assert settings.DEFAULT_PRIVACY_URL.endswith("PRIVACY.md")

    def test_the_policy_file_exists(self):
        """A command linking to a missing document is worse than no command."""
        assert (Path(__file__).resolve().parents[1] / "PRIVACY.md").is_file()

    def test_url_is_configurable(self):
        """So the policy can move to a website without a code change."""
        src = (Path(__file__).resolve().parents[1] / "settings.py").read_text()
        assert '_CFG.get("privacy")' in src


class TestPrivacyIsWired:
    """A localised handler nothing registers is dead code."""

    @staticmethod
    def _registered_commands():
        import bot
        from telegram.ext import CommandHandler, ConversationHandler
        app = MagicMock()
        seen: list = []
        app.add_handler.side_effect = lambda h, group=0: seen.append(h)
        builder = MagicMock()
        builder.token.return_value = builder
        builder.post_init.return_value = builder
        builder.build.return_value = app
        with patch.object(bot, "Application") as App:
            App.builder.return_value = builder
            bot.main()
        cmds = set()
        for h in seen:
            if isinstance(h, CommandHandler):
                cmds |= set(h.commands)
            elif isinstance(h, ConversationHandler):
                for e in h.entry_points:
                    if isinstance(e, CommandHandler):
                        cmds |= set(e.commands)
        return cmds

    def test_privacy_command_is_registered(self):
        assert "privacy" in self._registered_commands()

    def test_privacy_is_in_the_telegram_command_menu(self):
        """post_init publishes the menu users see when they type '/'."""
        src = (Path(__file__).resolve().parents[1] / "bot.py").read_text()
        assert 'BotCommand("privacy"' in src

    def test_help_privacy_topic_is_reachable(self):
        assert UB.HELP_TOPICS.get("privacy") == "help_privacy"
