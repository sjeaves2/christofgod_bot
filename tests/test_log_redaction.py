"""Tests that the bot token never reaches a log file or an admin message.

python-telegram-bot calls api.telegram.org/bot<TOKEN>/<method>, so the token
appears in httpx request logs and inside network-exception text. It leaked into
logs/bot.log for months because the filter was attached to the "httpx" logger,
and a filter on a logger is NOT applied to records propagated from its children
(httpx logs under "httpx._client"). The filters now live on the handlers.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import settings

FAKE_TOKEN = "8655410838:AAHV4z-O7P2bhUnVUdfLzDt5aJdRGJnxP68"
FAKE_URL = f"https://api.telegram.org/bot{FAKE_TOKEN}/getUpdates"


def _capture_log(tmp_path, records, level=logging.INFO) -> str:
    """Emit *records* through handlers configured exactly like the bot's."""
    log_path = tmp_path / "probe.log"
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    handler.setLevel(level)
    handler.addFilter(settings._HttpxApiLogFilter())
    handler.addFilter(settings._SecretRedactingFilter())
    root = logging.getLogger()
    previous_level = root.level
    root.addHandler(handler)
    # pytest's logging plugin adjusts the root level; force it so this test
    # exercises the filters rather than level-based suppression.
    root.setLevel(logging.DEBUG)
    logging.disable(logging.NOTSET)
    try:
        for logger_name, lvl, msg, args in records:
            child = logging.getLogger(logger_name)
            child.setLevel(logging.NOTSET)      # inherit from root
            child.log(lvl, msg, *args)
        handler.flush()
    finally:
        root.removeHandler(handler)
        root.setLevel(previous_level)
        handler.close()
    return log_path.read_text(encoding="utf-8")


class TestRedactSecrets:
    def test_redacts_token_in_url(self):
        with patch.object(settings, "BOT_TOKEN", FAKE_TOKEN):
            out = settings.redact_secrets(FAKE_URL)
        assert FAKE_TOKEN not in out
        assert settings.TOKEN_PLACEHOLDER in out
        assert out.endswith("/getUpdates"), "surrounding text must be preserved"

    def test_redacts_bare_token(self):
        with patch.object(settings, "BOT_TOKEN", FAKE_TOKEN):
            out = settings.redact_secrets(f"configured token is {FAKE_TOKEN} ok")
        assert FAKE_TOKEN not in out
        assert out == f"configured token is {settings.TOKEN_PLACEHOLDER} ok"

    def test_redacts_a_different_bots_token(self):
        """Pattern-based, so a token that isn't ours is caught too."""
        other = "bot999999999:ZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZ"
        out = settings.redact_secrets(f"POST https://api.telegram.org/{other}/sendMessage")
        assert "ZZZZ" not in out
        assert settings.TOKEN_PLACEHOLDER in out

    def test_url_and_bare_forms_render_identically(self):
        with patch.object(settings, "BOT_TOKEN", FAKE_TOKEN):
            from_url = settings.redact_secrets(FAKE_URL)
            from_bare = settings.redact_secrets(FAKE_TOKEN)
        assert settings.TOKEN_PLACEHOLDER in from_url
        assert from_bare == settings.TOKEN_PLACEHOLDER
        assert "bot" + settings.TOKEN_PLACEHOLDER not in from_url

    def test_leaves_ordinary_text_untouched(self):
        assert settings.redact_secrets("nothing sensitive here") == "nothing sensitive here"

    def test_handles_empty_and_none(self):
        assert settings.redact_secrets("") == ""
        assert settings.redact_secrets(None) == ""


class TestHandlerFiltering:
    """The regression that mattered: records from httpx CHILD loggers."""

    def test_child_logger_record_is_redacted(self, tmp_path):
        with patch.object(settings, "BOT_TOKEN", FAKE_TOKEN):
            text = _capture_log(tmp_path, [
                ("httpx._client", logging.INFO,
                 'HTTP Request: POST %s "HTTP/1.1 500 Server Error"', (FAKE_URL,)),
            ])
        assert FAKE_TOKEN not in text, "token must never reach the log file"
        assert settings.TOKEN_PLACEHOLDER in text

    def test_token_redacted_regardless_of_logger(self, tmp_path):
        with patch.object(settings, "BOT_TOKEN", FAKE_TOKEN):
            text = _capture_log(tmp_path, [
                ("telegram.ext", logging.ERROR, "Failed calling %s", (FAKE_URL,)),
                ("bot", logging.ERROR, "boom while polling %s", (FAKE_URL,)),
            ])
        assert FAKE_TOKEN not in text
        assert text.count(settings.TOKEN_PLACEHOLDER) == 2

    def test_successful_httpx_calls_demoted_below_info(self, tmp_path):
        text = _capture_log(tmp_path, [
            ("httpx._client", logging.INFO, 'HTTP Request: POST %s "HTTP/1.1 200 OK"', (FAKE_URL,)),
            ("httpx._client", logging.INFO, 'HTTP Request: POST %s "HTTP/1.1 200 OK"', (FAKE_URL,)),
        ])
        # The first is replaced by the friendly notice; neither shows the URL line.
        assert "HTTP/1.1 200 OK" not in text

    def test_api_errors_are_still_surfaced(self, tmp_path):
        text = _capture_log(tmp_path, [
            ("httpx._client", logging.INFO,
             'HTTP Request: POST %s "HTTP/1.1 500 Server Error"', (FAKE_URL,)),
        ])
        assert "500" in text, "4xx/5xx responses must remain visible"

    def test_non_httpx_errors_are_not_demoted(self, tmp_path):
        """Regression: the demotion filter runs on every handler record now, so
        it must not silence a real error merely for mentioning a getUpdates URL."""
        text = _capture_log(tmp_path, [
            ("telegram.ext", logging.ERROR, "Failed calling %s", (FAKE_URL,)),
        ])
        assert "ERROR telegram.ext" in text

    def test_plain_records_pass_through(self, tmp_path):
        text = _capture_log(tmp_path, [("bot", logging.INFO, "ordinary message", ())])
        assert "ordinary message" in text


class TestErrorReportRedaction:
    def test_error_log_is_redacted(self, tmp_path):
        import error_reporting as er
        try:
            raise RuntimeError(f"connection failed for {FAKE_URL}")
        except RuntimeError as exc:
            with patch.object(settings, "BOT_TOKEN", FAKE_TOKEN), \
                 patch.object(er, "ERRORS_LOG", tmp_path / "errors.log"):
                er.write_error_log("ERR-TOK1", exc)
                written = (tmp_path / "errors.log").read_text()
        assert FAKE_TOKEN not in written
        assert settings.TOKEN_PLACEHOLDER in written

    def test_admin_alert_is_redacted(self, tmp_path):
        import error_reporting as er
        try:
            raise RuntimeError(f"connection failed for {FAKE_URL}")
        except RuntimeError as exc:
            with patch.object(settings, "BOT_TOKEN", FAKE_TOKEN):
                text = er.build_alert("ERR-TOK2", exc)
        assert FAKE_TOKEN not in text, "an admin DM must not carry the token"
        assert settings.TOKEN_PLACEHOLDER in text


class TestRuntimeLogRetention:
    def test_runtime_log_pruned_at_45_days(self):
        assert settings.RUNTIME_LOG_RETENTION == 45

    def test_fallback_default_is_45(self):
        """Used when a deployment's config.yaml predates the setting."""
        assert settings.DEFAULT_RUNTIME_LOG_RETENTION == 45

    def test_shipped_example_config_sets_45(self):
        text = (Path(__file__).parent.parent / "config" / "config.yaml.example").read_text()
        assert "runtime_retention_days: 45" in text

    def test_activity_log_keeps_its_own_longer_window(self):
        assert settings.LOG_RETENTION == 90
        assert settings.RUNTIME_LOG_RETENTION < settings.LOG_RETENTION


class TestFiltersAreInstalled:
    """The filters are only useful if they are actually attached to the real
    handlers — attaching them to the 'httpx' logger is what failed before."""

    def _filter_types(self, handler):
        return {type(f).__name__ for f in handler.filters}

    def test_file_handler_has_both_filters(self):
        installed = self._filter_types(settings._file_handler)
        assert "_SecretRedactingFilter" in installed
        assert "_HttpxApiLogFilter" in installed

    def test_console_handler_has_both_filters(self):
        installed = self._filter_types(settings._console_handler)
        assert "_SecretRedactingFilter" in installed
        assert "_HttpxApiLogFilter" in installed

    def test_redaction_reaches_the_real_file_handler(self):
        """Drive a record through the bot's own file handler filters."""
        record = logging.LogRecord("httpx._client", logging.INFO, __file__, 1,
                                   "HTTP Request: POST %s", (FAKE_URL,), None)
        with patch.object(settings, "BOT_TOKEN", FAKE_TOKEN):
            for f in settings._file_handler.filters:
                f.filter(record)
        assert FAKE_TOKEN not in record.getMessage()
