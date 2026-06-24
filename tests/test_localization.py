"""Tests for the localization catalog and t() lookup."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from localization import AVAILABLE_LANGUAGES, CATALOG, DEFAULT_LANG, t


def _placeholders(s: str) -> set:
    return set(re.findall(r"\{(\w+)\}", s))


class TestTranslate:
    def test_returns_english_by_default(self):
        assert t("events_none") == CATALOG["en"]["events_none"]

    def test_explicit_known_language(self):
        assert t("events_none", "en") == CATALOG["en"]["events_none"]

    def test_unknown_language_falls_back_to_default(self):
        assert t("events_none", "zz") == CATALOG[DEFAULT_LANG]["events_none"]

    def test_none_language_uses_default(self):
        assert t("events_none", None) == CATALOG[DEFAULT_LANG]["events_none"]

    def test_unknown_key_returns_key(self):
        assert t("totally_made_up_key", "en") == "totally_made_up_key"

    def test_placeholder_substitution(self):
        out = t("tz_set", "en", tz="America/New_York", now="Monday")
        assert "America/New_York" in out
        assert "Monday" in out

    def test_missing_placeholder_does_not_raise(self):
        # Catalog string expects {id}; omit it — should return the template unformatted.
        out = t("appt_submitted", "en")
        assert isinstance(out, str)
        assert "{id}" in out  # left intact rather than crashing

    def test_appt_line_has_expected_fields(self):
        out = t("appt_line", "en", counterparty="Pastor", when="Mon", status="confirmed", id="ABC")
        assert "Pastor" in out and "Mon" in out and "confirmed" in out and "ABC" in out
        assert "ID: ABC" in out


class TestCatalogIntegrity:
    def test_default_lang_present(self):
        assert DEFAULT_LANG in CATALOG

    def test_available_languages_have_catalogs(self):
        for code in AVAILABLE_LANGUAGES:
            assert code in CATALOG, f"{code} listed but no catalog"

    def test_english_is_available(self):
        assert "en" in AVAILABLE_LANGUAGES

    def test_spanish_and_french_added(self):
        assert {"en", "es", "fr"} <= set(AVAILABLE_LANGUAGES)
        assert "es" in CATALOG and "fr" in CATALOG


class TestTranslationParity:
    @pytest.mark.parametrize("lang", ["es", "fr"])
    def test_covers_all_english_keys(self, lang):
        missing = set(CATALOG["en"]) - set(CATALOG[lang])
        assert not missing, f"{lang} missing keys: {sorted(missing)}"

    @pytest.mark.parametrize("lang", ["es", "fr"])
    def test_no_extra_keys(self, lang):
        extra = set(CATALOG[lang]) - set(CATALOG["en"])
        assert not extra, f"{lang} has unknown keys: {sorted(extra)}"

    @pytest.mark.parametrize("lang", ["es", "fr"])
    def test_placeholders_match_english(self, lang):
        for key, en_text in CATALOG["en"].items():
            assert _placeholders(CATALOG[lang][key]) == _placeholders(en_text), (
                f"{lang}:{key} placeholders differ from English"
            )

    @pytest.mark.parametrize("lang", ["es", "fr"])
    def test_slash_commands_preserved(self, lang):
        for cmd in ("/help", "/events", "/appointment", "/cancelappointment",
                    "/settimezone", "/language", "/stop"):
            assert cmd in CATALOG[lang]["user_commands"]

    @pytest.mark.parametrize("lang", ["en", "es", "fr"])
    def test_date_token_preserved(self, lang):
        assert "YYYY-MM-DD" in CATALOG[lang]["appt_ask_date"]

    def test_spanish_lookup_distinct_from_english(self):
        assert t("events_none", "es") != t("events_none", "en")

    def test_french_lookup_distinct_from_english(self):
        assert t("events_none", "fr") != t("events_none", "en")


class TestAffirmative:
    @pytest.mark.parametrize("word", ["yes", "Y", " sí ", "si", "oui", "O"])
    def test_accepts_multilingual_yes(self, word):
        import bot
        assert bot._is_affirmative(word)

    @pytest.mark.parametrize("word", ["no", "non", "nope", "", "maybe"])
    def test_rejects_non_affirmative(self, word):
        import bot
        assert not bot._is_affirmative(word)

    def test_none_is_not_affirmative(self):
        import bot
        assert not bot._is_affirmative(None)


class TestLocalizedDatetime:
    def _dt(self):
        import pytz
        from datetime import datetime
        return pytz.timezone("America/New_York").localize(datetime(2026, 6, 18, 10, 15))

    def test_english_month_and_weekday(self):
        from localization import localized_datetime
        out = localized_datetime(self._dt(), "en")
        assert "June" in out and "Thursday" in out and "EDT" in out

    def test_spanish_localized_names(self):
        from localization import localized_datetime
        out = localized_datetime(self._dt(), "es")
        assert "junio" in out and "jueves" in out

    def test_french_localized_names(self):
        from localization import localized_datetime
        out = localized_datetime(self._dt(), "fr")
        assert "juin" in out and "jeudi" in out

    def test_zulu_localized(self):
        from localization import localized_datetime
        out = localized_datetime(self._dt(), "zu")
        # Zulu weekday for Thursday is "ULwesine" (CLDR).
        assert "Lwesine" in out

    def test_unknown_locale_falls_back_to_english(self):
        from localization import localized_datetime
        out = localized_datetime(self._dt(), "de")
        assert "June" in out

    def test_no_narrow_nbsp_in_output(self):
        from localization import localized_datetime
        out = localized_datetime(self._dt(), "en")
        assert " " not in out and " " not in out


class TestStatusLabel:
    def test_known_status_english(self):
        from localization import status_label
        assert status_label("confirmed", "en") == "confirmed"

    def test_known_status_spanish(self):
        from localization import status_label
        assert status_label("confirmed", "es") == "confirmada"

    def test_known_status_french(self):
        from localization import status_label
        assert status_label("cancelled", "fr") == "annulé"

    def test_known_status_zulu(self):
        from localization import status_label
        assert status_label("pending", "zu") == "kulindile"

    def test_unknown_status_returns_raw(self):
        from localization import status_label
        assert status_label("weird_status", "en") == "weird_status"

    def test_empty_status(self):
        from localization import status_label
        assert status_label(None, "en") == ""

    def test_all_active_statuses_have_labels(self):
        from localization import CATALOG
        for st in ("pending", "confirmed", "counter_proposed", "cancelled", "declined"):
            assert f"status_{st}" in CATALOG["en"]
