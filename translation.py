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
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Locale codes we offer. A new backend maps these to whatever it expects.
SUPPORTED = ("en", "es", "fr", "zu")


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
