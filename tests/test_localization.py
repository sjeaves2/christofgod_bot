"""Tests for the localization catalog and t() lookup."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from localization import AVAILABLE_LANGUAGES, CATALOG, DEFAULT_LANG, t


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
