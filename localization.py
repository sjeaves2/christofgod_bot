"""Lightweight localization (i18n) for user-facing strings.

The strings themselves live in one module per language under locales/
(locales/en.py, locales/es.py, …), each exporting STRINGS: dict[str, str].
Adding a language means adding a locales/<code>.py module, importing it below
into CATALOG, and listing it in AVAILABLE_LANGUAGES — no code changes
elsewhere. Any missing key falls back to English. Conventions kept consistent
across languages:
  - Slash-command names (e.g. "/events") are left untranslated.
  - The literal date/time format tokens "YYYY-MM-DD" and "HH:MM" are kept as-is
    so they match what the parser expects.
  - {placeholders} must be preserved exactly.

Note: values substituted at runtime — event names, appointment statuses, and
formatted dates ({when}) — are not themselves translated.

Usage:
    from localization import t
    t("events_none", lang)                      # simple lookup
    t("appt_confirmed_user", lang, id="ABC", when="...")  # with placeholders
"""

from __future__ import annotations

from datetime import datetime

from babel.dates import format_date, format_time

from locales import en, es, fr, zu

DEFAULT_LANG = "en"

# Languages offered to users via /language (code -> display name).
AVAILABLE_LANGUAGES: dict[str, str] = {
    "en": "English",
    "es": "Español",
    "fr": "Français",
    "zu": "isiZulu",
}


def localized_datetime(dt: datetime, lang: str | None = None) -> str:
    """Format an (already tz-aware, already tz-converted) datetime for *lang*.

    Uses Babel/CLDR locale data for correct weekday & month names and ordering,
    then appends the timezone abbreviation (e.g. EDT). Falls back to English on
    any unknown locale.
    """
    locale = lang if lang in AVAILABLE_LANGUAGES else DEFAULT_LANG
    try:
        date_part = format_date(dt, format="full", locale=locale)
        time_part = format_time(dt, format="short", locale=locale)
    except Exception:
        date_part = format_date(dt, format="full", locale=DEFAULT_LANG)
        time_part = format_time(dt, format="short", locale=DEFAULT_LANG)
    tz_abbr = dt.strftime("%Z")
    result = f"{date_part}, {time_part} {tz_abbr}".strip()
    # Babel/CLDR uses narrow/no-break spaces (e.g. before AM/PM); normalize to
    # plain spaces for predictable display and matching.
    return result.translate({0x202F: " ", 0x00A0: " "})


def status_label(status: str | None, lang: str | None = None) -> str:
    """Localized label for an appointment status, falling back to the raw value."""
    if not status:
        return ""
    return t(f"status_{status}", lang) if f"status_{status}" in CATALOG[DEFAULT_LANG] else status

CATALOG: dict[str, dict[str, str]] = {
    "en": en.STRINGS,
    "es": es.STRINGS,
    "fr": fr.STRINGS,
    "zu": zu.STRINGS,
}


def t(key: str, lang: str | None = None, **kwargs) -> str:
    """Translate ``key`` into ``lang`` (falling back to English), then format."""
    if lang not in CATALOG:
        lang = DEFAULT_LANG
    text = CATALOG.get(lang, {}).get(key)
    if text is None:
        text = CATALOG[DEFAULT_LANG].get(key, key)
    if kwargs:
        try:
            return text.format(**kwargs)
        except (KeyError, IndexError):
            return text
    return text
