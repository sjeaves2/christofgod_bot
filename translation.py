"""Best-effort machine translation for admin-typed runtime content.

Wraps deep-translator's Google backend behind a tiny, failure-safe interface so
the rest of the codebase never imports the library directly — swapping backends
(official Cloud Translate, DeepL, an LLM) only changes this module.

Design contract: translate() NEVER raises. Any failure — network down,
throttling, unsupported language, library breakage — returns None, and callers
fall back to the original text.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Our locale codes happen to match Google's for the languages we offer
# (en, es, fr, zu). Map here if that ever diverges.
_GOOGLE_LANG = {"en": "en", "es": "es", "fr": "fr", "zu": "zu"}


def translate(text: str, source_lang: str, target_lang: str) -> "str | None":
    """Translate *text*, or return None if translation isn't possible.

    Blocking (network call) — invoke via run_in_executor from async code.
    """
    src = _GOOGLE_LANG.get(source_lang)
    dst = _GOOGLE_LANG.get(target_lang)
    if not text or not src or not dst or src == dst:
        return None
    try:
        from deep_translator import GoogleTranslator
        result = GoogleTranslator(source=src, target=dst).translate(text)
        return result or None
    except Exception as exc:  # deliberately broad — see module docstring
        logger.warning("Translation %s→%s failed: %s", source_lang, target_lang, exc)
        return None
