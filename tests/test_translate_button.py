"""A reader whose language differs from an announcement's gets a Translate button.

Chosen on 2026-11-17 instead of a translation backend: a button carrying a
Google Translate link means the request to the translator leaves from the
READER'S device when they tap it. The bot never contacts a translation service,
holds no key, and is not exposed to the AWS-IP blocking that made the old
scraped endpoint return nothing from the server.

The rules these tests hold:

* The button appears only where ONE reader sees the message — a DM, or
  /announcements in a private chat. In a group every member sees the same
  buttons, so one member's language must never choose them.
* It appears only when the reader's language differs from the announcement's.
* The broadcast shares one markup across every recipient, so adding a row
  must build a new markup, never edit the shared one.
* Telegram rejects the WHOLE message over a button it dislikes, so a link too
  long for the cap is dropped (and logged) rather than sent.
"""

from __future__ import annotations

import ast
import logging
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlsplit

import pytest
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatType
from telegram.error import BadRequest

sys.path.insert(0, str(Path(__file__).parent.parent))

import common  # noqa: E402
import handlers.announcements as A  # noqa: E402
import handlers.broadcast as B  # noqa: E402
import translation  # noqa: E402
from localization import CATALOG, t  # noqa: E402

ROOT = Path(__file__).parent.parent
LINK = "https://example.test/signup"


def _query(url: str) -> dict[str, str]:
    parts = urlsplit(url)
    return {k: v[0] for k, v in parse_qs(parts.query, keep_blank_values=True).items()}


def _buttons(markup) -> list[InlineKeyboardButton]:
    if markup is None:
        return []
    return [b for row in markup.inline_keyboard for b in row]


def _translate_buttons(markup) -> list[InlineKeyboardButton]:
    return [b for b in _buttons(markup)
            if b.url and b.url.startswith(translation.TRANSLATE_PAGE)]


# ---------------------------------------------------------------------------
# The link itself
# ---------------------------------------------------------------------------

class TestTheTranslateLink:
    def test_it_points_at_google_translate_over_https(self):
        url = translation.translate_url("Hello", "en", "es")
        parts = urlsplit(url)
        assert (parts.scheme, parts.hostname) == ("https", "translate.google.com")

    def test_the_direction_is_source_to_reader(self):
        """Swapped, the reader is shown a 'translation' into the language they
        could not read in the first place."""
        q = _query(translation.translate_url("Hello", "en", "zu"))
        assert (q["sl"], q["tl"]) == ("en", "zu")

    @pytest.mark.parametrize("text", [
        "Bring your Bible",
        "Fish & chips #3 at 50% off?",
        "Línea uno\n\nLínea dos — ¿sí?",
        "🙏 Prayer *tonight* at 7_30",
    ])
    def test_the_text_survives_the_round_trip_exactly(self, text):
        """Anything lost here is lost silently: the reader sees a translation
        of a different message. &, #, % and newlines are the characters that
        break a hand-built query string."""
        assert _query(translation.translate_url(text, "en", "fr"))["text"] == text

    def test_a_space_is_percent_encoded_not_a_plus(self):
        url = translation.translate_url("a b", "en", "es")
        assert "a%20b" in url and "a+b" not in url

    def test_it_is_a_url_a_button_will_accept(self):
        url = translation.translate_url("Line one\nLine two", "en", "es")
        assert common.is_button_url(url) == url

    @pytest.mark.parametrize("src,dst", [("en", "en"), ("es", "es")])
    def test_no_link_when_the_languages_match(self, src, dst):
        assert translation.translate_url("Hello", src, dst) is None

    @pytest.mark.parametrize("src,dst", [
        ("", "es"),      # a legacy record with no source language
        (None, "es"),
        ("de", "es"),    # a language we do not offer
        ("en", "xx"),
    ])
    def test_no_link_for_an_unknown_language(self, src, dst):
        assert translation.translate_url("Hello", src, dst) is None

    @pytest.mark.parametrize("text", ["", "   ", "\n\n", None])
    def test_no_link_when_there_is_nothing_to_translate(self, text):
        assert translation.translate_url(text, "en", "es") is None

    def test_the_cap_is_inclusive_and_exact(self):
        """Measured at the boundary, so an off-by-one in either direction
        fails: a link of exactly the cap is offered, one character more is not."""
        base = len(translation.translate_url("x", "en", "es")) - 1
        at_cap = "x" * (translation.MAX_TRANSLATE_URL - base)
        assert len(translation.translate_url(at_cap, "en", "es")) == \
            translation.MAX_TRANSLATE_URL
        assert translation.translate_url(at_cap + "x", "en", "es") is None

    def test_a_dropped_button_is_logged(self, caplog):
        """The one case where a reader who needed a translation gets none."""
        with caplog.at_level(logging.INFO, logger="translation"):
            assert translation.translate_url(
                "x" * translation.MAX_TRANSLATE_URL, "en", "es") is None
        assert any("No Translate button" in r.getMessage() for r in caplog.records)

    def test_a_longest_plain_announcement_fits(self):
        """The cap must not quietly cost every real announcement its button:
        a maximum-length ASCII title and body still fit."""
        text = "T" * A.ANN_TITLE_MAX + "\n\n" + ("word " * (A.ANN_BODY_MAX // 5))
        assert translation.translate_url(text, "en", "es") is not None

    def test_the_translation_module_has_no_http_client(self):
        """The whole design: the BOT never fetches a translation. A backend may
        return here one day, deliberately; this makes that a visible decision
        rather than an import slipped in beside the link builder."""
        tree = ast.parse((ROOT / "translation.py").read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert imported, "found no imports at all — the scan is not reading the file"
        assert not imported & {"httpx", "requests", "urllib3", "aiohttp",
                               "http", "socket", "deep_translator"}, imported


# ---------------------------------------------------------------------------
# Adding the button to a markup
# ---------------------------------------------------------------------------

def _link_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔗 Open link", url=LINK)]])


class TestAddingTheButton:
    def test_same_language_returns_the_markup_itself(self):
        markup = _link_markup()
        assert common.with_translate_button(markup, "Hi", "en", "en") is markup

    def test_same_language_and_no_markup_stays_none(self):
        assert common.with_translate_button(None, "Hi", "en", "en") is None

    def test_a_different_language_adds_one_row_below_the_link(self):
        out = common.with_translate_button(_link_markup(), "Hi", "en", "es")
        rows = out.inline_keyboard
        assert len(rows) == 2
        assert rows[0][0].url == LINK, "the link button must keep its place"
        assert _query(rows[1][0].url)["tl"] == "es"

    def test_the_shared_markup_is_not_changed(self):
        """The broadcast hands ONE markup to every recipient. Editing it in
        place would give the next recipient — a group, say — this reader's
        Translate button."""
        shared = _link_markup()
        common.with_translate_button(shared, "Hi", "en", "es")
        assert len(shared.inline_keyboard) == 1

    def test_no_markup_becomes_a_markup_with_just_the_button(self):
        out = common.with_translate_button(None, "Hi", "en", "fr")
        assert len(_buttons(out)) == 1 and _translate_buttons(out)

    @pytest.mark.parametrize("lang,label", [
        ("en", "🌐 Translate"), ("es", "🌐 Traducir"),
        ("fr", "🌐 Traduire"), ("zu", "🌐 Humusha"),
    ])
    def test_the_label_is_in_the_reader_s_language(self, lang, label):
        """The reader's, not the announcement's: they may not read the source."""
        src = "es" if lang == "en" else "en"
        out = common.with_translate_button(None, "Hi", src, lang)
        assert _buttons(out)[0].text == label

    def test_a_given_label_is_used(self):
        out = common.with_translate_button(None, "Hi", "en", "es", label="X")
        assert _buttons(out)[0].text == "X"


class TestTheLabelsExistEverywhere:
    @pytest.mark.parametrize("key", ["translate_button", "translate_button_titled"])
    def test_every_locale_has_its_own(self, key):
        """A missing key falls back to English: a Spanish reader would be
        offered a translation by a button they may not understand."""
        values = {lang: CATALOG[lang].get(key) for lang in CATALOG}
        assert all(values.values()), values
        assert len(set(values.values())) == len(values), values

    def test_the_titled_label_names_the_announcement(self):
        for lang in CATALOG:
            assert "Roof" in t("translate_button_titled", lang, title="Roof"), lang


# ---------------------------------------------------------------------------
# What the translator is handed
# ---------------------------------------------------------------------------

class TestTheTranslatableText:
    def test_title_and_body(self):
        ann = {"title": "Roof fund", "body": "We are raising funds.", "link": LINK}
        assert A.translatable_text(ann) == "Roof fund\n\nWe are raising funds."

    def test_not_the_link(self):
        assert "example.test" not in A.translatable_text(
            {"title": "T", "body": "B", "link": LINK})

    def test_a_missing_part_leaves_no_stray_separator(self):
        assert A.translatable_text({"title": "Only a title"}) == "Only a title"


# ---------------------------------------------------------------------------
# The broadcast
# ---------------------------------------------------------------------------

def _dm(chat_id, lang):
    return {"kind": "user", "chat_id": chat_id, "label": f"u{chat_id}", "lang": lang}


def _group(chat_id=-100):
    return {"kind": "group", "chat_id": chat_id, "label": "G"}


async def _push(recipients, *, translatable=True, markup=None, media=None):
    """Run the real send loop; return {chat_id: reply_markup sent}."""
    sent: dict = {}

    async def send_message(chat_id, text, **kwargs):
        sent[chat_id] = kwargs.get("reply_markup")

    async def send_media(bot, chat_id, kind, file_id, **kwargs):
        sent[chat_id] = kwargs.get("reply_markup")

    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=send_message)
    ctx = MagicMock()
    ctx.user_data = {"bc_message": "📢 *Roof fund*", "bc_markup": markup,
                     "bc_recipients": recipients, "bc_done": set()}
    if media is not None:
        ctx.user_data["bc_media"] = media
    if translatable:
        ctx.user_data["bc_translate"] = {"lang": "en", "text": "Roof fund\n\nBody"}
    with patch.object(B, "_send_media", AsyncMock(side_effect=send_media)):
        await B._bc_send_pending(bot, ctx)
    return sent


class TestTheBroadcast:
    async def test_a_dm_in_another_language_gets_the_button(self):
        sent = await _push([_dm(1, "es")])
        (button,) = _translate_buttons(sent[1])
        assert _query(button.url)["tl"] == "es"
        assert _query(button.url)["text"] == "Roof fund\n\nBody"

    async def test_a_dm_in_the_same_language_does_not(self):
        sent = await _push([_dm(1, "en")])
        assert sent[1] is None

    async def test_a_group_never_does(self):
        sent = await _push([_group()])
        assert sent[-100] is None

    async def test_each_dm_gets_its_own_language(self):
        sent = await _push([_dm(1, "es"), _dm(2, "zu"), _dm(3, "en")])
        assert _query(_translate_buttons(sent[1])[0].url)["tl"] == "es"
        assert _query(_translate_buttons(sent[2])[0].url)["tl"] == "zu"
        assert sent[3] is None

    async def test_a_group_sent_after_a_dm_still_gets_the_shared_markup(self):
        """The order that would expose an in-place edit of the shared markup."""
        shared = _link_markup()
        sent = await _push([_dm(1, "es"), _group()], markup=shared)
        assert sent[-100] is shared
        assert len(sent[-100].inline_keyboard) == 1
        assert len(sent[1].inline_keyboard) == 2

    async def test_the_link_button_is_kept_alongside(self):
        sent = await _push([_dm(1, "fr")], markup=_link_markup())
        urls = [b.url for b in _buttons(sent[1])]
        assert urls[0] == LINK and urls[1].startswith(translation.TRANSLATE_PAGE)

    async def test_nothing_translatable_means_no_button(self):
        """A push that did not set bc_translate — the plain path."""
        sent = await _push([_dm(1, "es")], translatable=False)
        assert sent[1] is None

    async def test_a_media_caption_carries_the_button(self):
        media = {"kind": "photo", "file_id": "F", "caption": "📢 *Roof fund*"}
        sent = await _push([_dm(1, "es")], media=media)
        assert _translate_buttons(sent[1])

    async def test_media_without_a_caption_carries_no_buttons(self):
        """The existing rule: with no caption the text went nowhere, so a
        button has nothing to belong to."""
        media = {"kind": "photo", "file_id": "F", "caption": None}
        sent = await _push([_dm(1, "es")], media=media)
        assert sent[1] is None


class TestRecipientsCarryTheirLanguage:
    async def _expand(self, users):
        opts = [{"key": "all", "kind": "all", "chat_id": None, "label": "All"}]
        with patch("storage.get_all_users", AsyncMock(return_value=users)):
            return await B._bc_expand_recipients(opts, {"all"})

    async def test_a_member_s_language_is_read_from_their_record(self):
        (r,) = await self._expand([{"chat_id": 1, "language": "zu"}])
        assert r["lang"] == "zu"

    async def test_a_member_who_never_chose_defaults(self):
        """Twelve of fourteen members, on the day this was written."""
        (r,) = await self._expand([{"chat_id": 1}])
        assert r["lang"] == "en"

    async def test_a_group_has_no_language(self):
        opts = [{"key": "g", "kind": "group", "chat_id": -100, "label": "G"}]
        with patch("storage.get_all_users", AsyncMock(return_value=[])):
            (r,) = await B._bc_expand_recipients(opts, {"g"})
        assert "lang" not in r


class TestTheAnnouncementHandsItOver:
    @staticmethod
    async def _confirm(admin_lang="en", stale=None):
        upd = MagicMock()
        upd.effective_user.id = 1
        upd.effective_user.username = "plainadmin"
        upd.effective_user.full_name = "First Official"
        upd.message.text = "yes"
        upd.message.reply_text = AsyncMock()
        ctx = MagicMock()
        ctx.user_data = {"an_title": "Roof fund", "an_body": "We are raising funds.",
                         "an_expires": "2099-01-01", "an_link": LINK}
        if stale is not None:
            ctx.user_data["bc_translate"] = stale
        ctx.bot = MagicMock()
        with patch.object(A.storage, "get_announcements", AsyncMock(return_value=[])), \
             patch.object(A.storage, "save_announcements", AsyncMock()), \
             patch.object(A, "get_user_prefs", AsyncMock(return_value=(None, admin_lang))), \
             patch.object(A, "_broadcast_target_options", AsyncMock(return_value=[])), \
             patch.object(A.activity, "log_command", lambda *a, **k: None):
            await A.an_confirm(upd, ctx)
        return ctx.user_data["bc_translate"]

    async def test_the_source_language_is_the_author_s(self):
        assert (await self._confirm("fr"))["lang"] == "fr"

    async def test_the_text_is_title_and_body_without_link_or_sender(self):
        text = (await self._confirm())["text"]
        assert text == "Roof fund\n\nWe are raising funds."

    async def test_a_previous_push_s_text_is_replaced(self):
        out = await self._confirm(stale={"lang": "es", "text": "OLD"})
        assert out["text"] != "OLD" and out["lang"] == "en"


# ---------------------------------------------------------------------------
# /announcements
# ---------------------------------------------------------------------------

def _ann(ann_id, title, lang="en"):
    return {"id": ann_id, "title": title, "body": f"About {title}.", "lang": lang,
            "expires": "2099-01-01", "created": f"2026-11-1{ann_id[-1]}"}


async def _view(anns, reader_lang, chat_type=ChatType.PRIVATE, for_lang=None,
                fail_first=False):
    """Run the real /announcements; return the reply_markup of the last send."""
    upd = MagicMock()
    upd.effective_user.id = 1
    upd.effective_user.username = "anofficial"
    upd.effective_user.full_name = "Second Official"
    upd.effective_chat.type = chat_type
    calls = []

    async def reply(text, **kwargs):
        calls.append(kwargs.get("reply_markup"))
        if fail_first and len(calls) == 1:
            raise BadRequest("can't parse entities")

    upd.message.reply_text = AsyncMock(side_effect=reply)
    patches = [
        patch.object(A.storage, "get_announcements", AsyncMock(return_value=anns)),
        patch.object(A, "get_user_prefs", AsyncMock(return_value=(None, reader_lang))),
        patch.object(A.activity, "log_command", lambda *a, **k: None),
    ]
    if for_lang is not None:
        patches.append(patch.object(A, "_announcement_for_lang", for_lang))
    for p in patches:
        p.start()
    try:
        await A.cmd_announcements(upd, MagicMock())
    finally:
        for p in patches:
            p.stop()
    return calls[-1], calls


class TestTheAnnouncementsCommand:
    async def test_one_button_per_announcement_named_for_it(self):
        markup, _ = await _view([_ann("A1", "Roof fund"), _ann("A2", "Picnic")], "es")
        buttons = _translate_buttons(markup)
        assert sorted(b.text for b in buttons) == [
            "🌐 Traducir: Picnic", "🌐 Traducir: Roof fund"]
        texts = {_query(b.url)["text"] for b in buttons}
        assert texts == {"Roof fund\n\nAbout Roof fund.", "Picnic\n\nAbout Picnic."}

    async def test_through_the_real_translation_path(self):
        """No substitute for _announcement_for_lang: with no backend it returns
        the record itself, and that identity is what decides the button."""
        markup, _ = await _view([_ann("A1", "Roof fund")], "zu")
        assert _query(_translate_buttons(markup)[0].url)["tl"] == "zu"

    async def test_none_for_a_reader_in_the_same_language(self):
        markup, _ = await _view([_ann("A1", "Roof fund")], "en")
        assert markup is None

    async def test_only_the_announcements_in_another_language(self):
        markup, _ = await _view(
            [_ann("A1", "Roof fund", lang="en"), _ann("A2", "Picnic", lang="es")], "es")
        assert [b.text for b in _translate_buttons(markup)] == ["🌐 Traducir: Roof fund"]

    @pytest.mark.parametrize("chat_type", [ChatType.GROUP, ChatType.SUPERGROUP,
                                           ChatType.CHANNEL])
    async def test_none_outside_a_private_chat(self, chat_type):
        markup, _ = await _view([_ann("A1", "Roof fund")], "es", chat_type=chat_type)
        assert markup is None

    async def test_none_for_one_already_translated(self):
        """If a backend ever translates it, the reader already has their
        language; a button would offer what they are reading."""
        async def translated(a, lang):
            return {**a, "body": "Traducido"}

        markup, _ = await _view([_ann("A1", "Roof fund")], "es", for_lang=translated)
        assert markup is None

    async def test_the_escaped_retry_keeps_the_buttons(self):
        """The fallback send is the one a member gets when markup fails; it
        must not quietly lose the translation."""
        markup, calls = await _view([_ann("A1", "Roof fund")], "es", fail_first=True)
        assert len(calls) == 2
        assert _translate_buttons(calls[0]) and _translate_buttons(markup)

    async def test_a_long_title_is_clipped_on_the_button(self):
        title = "W" * 60
        markup, _ = await _view([_ann("A1", title)], "fr")
        (button,) = _translate_buttons(markup)
        assert len(button.text) <= len("🌐 Traduire : ") + A.TRANSLATE_LABEL_TITLE_MAX
        assert button.text.endswith("…")
        assert _query(button.url)["text"].startswith(title), \
            "the clip is for the label only; the translator gets the full title"
