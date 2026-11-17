"""Best-effort machine translation for admin-typed runtime content.

**There is currently no backend.** translate() returns None, so callers fall
back to the original text — which is exactly what happened in production
anyway: deep-translator's free Google endpoint refuses requests from AWS IPs,
so every call from the server had already been returning None.

The library was removed on 2026-10-13 because it was the sole source of four
security advisories (its own PYSEC-2022-252, and three in urllib3 2.7.0 reached
through requests), none of which bought anything: in the live data not one
announcement had ever been translated, and none had a cached translation.

This module stays because the contract is worth keeping. The rest of the
codebase never imports a translation library directly, so adding a backend that
works — Google Cloud Translate with a key, DeepL, a self-hosted
LibreTranslate — is a change to this one function and nothing else.

Design contract: translate() NEVER raises. Any failure — network down,
throttling, unsupported language, no backend at all — returns None, and callers
fall back to the original text. Every caller already relies on this.

translate_url() is the translation we CAN offer without a backend: a link the
reader taps, so the request to the translator leaves from THEIR device, not the
bot's. That was chosen on 2026-11-17 over adding an account and a key while the
congregation is all English; translate-by-default needs the bot to make the
request, so it waits for translate() to have a backend.
"""

from __future__ import annotations

import logging
from urllib.parse import quote, urlencode

logger = logging.getLogger(__name__)

# Locale codes we offer. A new backend maps these to whatever it expects.
# Google Translate uses the same four codes, so translate_url needs no mapping.
SUPPORTED = ("en", "es", "fr", "zu")

TRANSLATE_PAGE = "https://translate.google.com/"

# The longest link we put on a button. Telegram does not document a limit for
# an inline button's url, and rejects the WHOLE message over one it dislikes,
# so this is deliberately conservative and NOT a measured figure. An
# announcement is at most 64 + 1024 characters; ASCII prose encodes to a bit
# over its own length, so ordinary announcements fit, and text heavy in
# emoji or accented letters (several bytes each, ×3 once percent-encoded) is
# what loses its button. Raise it only after measuring Telegram's real limit.
MAX_TRANSLATE_URL = 2000


def translate_url(text: str, source_lang: str, target_lang: str) -> str | None:
    """A Google Translate link for *text*, or None if no button should be offered.

    None when there is nothing to translate, a language is not one we offer
    (which includes a legacy record with no source language at all), the two
    languages are the same, or the link would exceed MAX_TRANSLATE_URL. The
    last is logged, because it is the one case where a reader who needed a
    translation silently gets none.

    The bot never fetches this url. It is only ever put on a button.
    """
    if not text or not text.strip():
        return None
    if source_lang not in SUPPORTED or target_lang not in SUPPORTED:
        return None
    if source_lang == target_lang:
        return None
    # quote, not the default quote_plus: %20 for a space is unambiguous in any
    # url parser, where '+' depends on the reader treating it as form-encoded.
    query = urlencode({"sl": source_lang, "tl": target_lang, "text": text,
                       "op": "translate"}, quote_via=quote)
    url = f"{TRANSLATE_PAGE}?{query}"
    if len(url) > MAX_TRANSLATE_URL:
        logger.info("No Translate button (%s→%s): the link would be %d characters, "
                    "over the %d cap.", source_lang, target_lang, len(url),
                    MAX_TRANSLATE_URL)
        return None
    return url


def translate(text: str, source_lang: str, target_lang: str) -> str | None:
    """Translate *text*, or return None if translation isn't possible.

    Returns None for everything while no backend is configured. Kept blocking
    in signature (callers use asyncio.to_thread) so wiring a real backend in
    does not ripple outwards.
    """
    if not text or source_lang not in SUPPORTED or target_lang not in SUPPORTED:
        return None
    if source_lang == target_lang:
        return None
    logger.debug("No translation backend configured; %s→%s returning None.",
                 source_lang, target_lang)
    return None
