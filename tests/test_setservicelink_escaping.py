"""/setservicelink must survive the very data it exists to repair.

On 2026-09-17 the picker died with

    BadRequest: Can't parse entities: can't find end of the entity
                starting at byte offset 1151

It lists each service with its CURRENT link, and nine of those links still
read "...?pwd=REPLACE_ME". Nine underscores is odd, so one had no partner and
Telegram rejected the whole menu.

The parity detail is the reason this went unnoticed. With all TEN links
placeholders the underscores paired up and the menu rendered — as italics, but
it worked. Setting one real link left nine, and the command needed to fix the
remaining links became unusable. The bug was latent until it was half fixed.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from telegram.error import BadRequest

sys.path.insert(0, str(Path(__file__).parent.parent))

import handlers.events_admin as EA  # noqa: E402

PLACEHOLDER = "https://example.test/j/0000000000?pwd=REPLACE_ME"
REAL = "https://example.test/j/1234567890?pwd=e3BzF98vLF2t2biXwRGbCVaRUUOrVN.1"


def _phases(n):
    return [{"phase_key": f"p{i}::Eve", "display": f"Service {i}"} for i in range(n)]


async def _picker(urls, phases=None, markdown_ok=True):
    phases = phases or _phases(len(urls))
    upd = MagicMock()
    upd.effective_user.id = 1
    upd.effective_user.username = "adm"
    upd.effective_user.full_name = "An Admin"

    async def reply(text, **kwargs):
        if kwargs.get("parse_mode") is not None and not markdown_ok:
            raise BadRequest("can't parse entities: can't find end of the entity")

    upd.message.reply_text = AsyncMock(side_effect=reply)
    ctx = MagicMock()
    ctx.user_data = {}
    with patch.object(EA.storage, "get_all_events_data",
                      AsyncMock(return_value={"convocation_urls": urls})), \
         patch.object(EA, "service_phases", lambda: phases), \
         patch.object(EA.activity, "log_command", lambda *a, **k: None), \
         patch("permissions.is_admin", lambda _u: True):
        await EA.cmd_setservicelink(upd, ctx)
    return upd


class TestPickerEscaping:
    async def test_nine_placeholders_still_render(self):
        """The exact production failure: odd number of unescaped underscores."""
        urls = {f"p{i}::Eve": PLACEHOLDER for i in range(9)}
        sent = (await _picker(urls)).message.reply_text.call_args[0][0]
        assert sent.count("REPLACE\\_ME") == 9
        assert "pwd=REPLACE_ME" not in sent

    async def test_the_half_fixed_config_renders(self):
        """One real link among nine placeholders — what broke it."""
        urls = {f"p{i}::Eve": PLACEHOLDER for i in range(9)}
        urls["p9::Eve"] = REAL
        sent = (await _picker(urls, _phases(10))).message.reply_text.call_args[0][0]
        assert sent.count("_") == sent.count("\\_"), "every underscore must be escaped"

    async def test_a_password_with_an_underscore_is_escaped(self):
        sent = (await _picker({"p0::Eve": "https://example.test/j/1?pwd=aB_cD"})) \
            .message.reply_text.call_args[0][0]
        assert "aB\\_cD" in sent

    async def test_links_are_still_readable(self):
        url = "https://example.test/j/1234567890"
        sent = (await _picker({"p0::Eve": url})).message.reply_text.call_args[0][0]
        assert url in sent

    async def test_services_without_a_link_are_listed(self):
        sent = (await _picker({}, _phases(3))).message.reply_text.call_args[0][0]
        assert "Service 0" in sent and "Service 2" in sent


class TestPickerNeverGoesSilent:
    async def test_falls_back_to_plain_text(self):
        """/setservicelink is the repair tool. It must never be unusable."""
        upd = await _picker({"p0::Eve": PLACEHOLDER}, markdown_ok=False)
        calls = upd.message.reply_text.call_args_list
        assert len(calls) == 2
        assert calls[1].kwargs.get("parse_mode") is None
        assert "Service 0" in calls[1].args[0]

    async def test_no_fallback_needed_when_markdown_is_fine(self):
        upd = await _picker({"p0::Eve": PLACEHOLDER}, markdown_ok=True)
        assert len(upd.message.reply_text.call_args_list) == 1
