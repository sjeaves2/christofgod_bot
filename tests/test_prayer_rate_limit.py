"""/prayer was the one member-reachable input with no throttle.

Found in the 2026-10-13 security review. A prayer request costs the sender
nothing and DMs every prayer admin, while appointments already had both a
cooldown and a pending cap. With twelve trusted members this was theoretical —
but it was the gap, and the cost of closing it is small.

Limits (chosen by the user): five minutes between submissions, five per day.
Both windows ROLL rather than resetting at midnight: a calendar reset invites
five at 23:59 and five more a minute later, and "you may send another at 3:15"
is a more honest thing to tell someone than "tomorrow".

The pastoral shape matters as much as the numbers. The check runs at /prayer
ENTRY, before the member writes anything — refusing after someone has typed a
page about a crisis, and making them retype it, would be a cruel way to enforce
a limit. The refusal says when they may return and points to a minister for
anything urgent.
"""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch


sys.path.insert(0, str(Path(__file__).parent.parent))

import handlers.prayer as P  # noqa: E402
from common import now_tz  # noqa: E402

MEMBER = 7


def _req(minutes_ago: float, chat_id: int = MEMBER, now=None):
    now = now or now_tz()
    return {"id": "PR-X", "requester_chat_id": chat_id,
            "created": (now - timedelta(minutes=minutes_ago)).isoformat()}


class TestNextAllowedAt:
    """The pure calculation, independent of Telegram."""

    def test_a_first_request_is_always_allowed(self):
        assert P.next_allowed_at([], MEMBER) is None

    def test_inside_the_cooldown_is_refused(self):
        assert P.next_allowed_at([_req(1)], MEMBER) is not None

    def test_past_the_cooldown_is_allowed(self):
        assert P.next_allowed_at([_req(6)], MEMBER) is None

    def test_the_cooldown_boundary(self):
        """Exactly five minutes has elapsed — the wait is over."""
        now = now_tz()
        assert P.next_allowed_at([_req(5, now=now)], MEMBER, now) is None

    def test_the_daily_cap_refuses_a_sixth(self):
        reqs = [_req(m) for m in (6, 60, 120, 180, 240)]
        assert P.next_allowed_at(reqs, MEMBER) is not None

    def test_five_is_allowed_when_one_has_aged_out(self):
        """The window rolls: a slot frees 24h after the oldest of the five."""
        reqs = [_req(m) for m in (6, 60, 120, 180, 25 * 60)]
        assert P.next_allowed_at(reqs, MEMBER) is None

    def test_another_members_requests_do_not_count(self):
        assert P.next_allowed_at([_req(1, chat_id=999)], MEMBER) is None

    def test_the_later_of_the_two_limits_wins(self):
        """Never name a time at which the other limit would still refuse.

        Five requests, the newest a minute ago: the cooldown clears in four
        minutes, but the daily slot only frees 24h after the OLDEST of the
        five (240 minutes ago) — about 20 hours away. The later must win.
        """
        now = now_tz()
        reqs = [_req(m, now=now) for m in (1, 60, 120, 180, 240)]
        retry = P.next_allowed_at(reqs, MEMBER, now)
        cooldown_clears = now + timedelta(minutes=4)
        daily_clears = now + timedelta(hours=24) - timedelta(minutes=240)
        assert retry > cooldown_clears, "the cooldown alone must not decide it"
        assert abs((retry - daily_clears).total_seconds()) < 90, (
            f"expected ~{daily_clears} (when the oldest of five ages out), got {retry}"
        )

    def test_closed_requests_still_count(self):
        """A stub keeps `created`, so answering a request does not reset the
        limit — otherwise a fast admin would hand the sender five more."""
        req = _req(1)
        req["status"] = "answered"
        req.pop("text", None)
        assert P.next_allowed_at([req], MEMBER) is not None


class TestMalformedRecords:
    """Never let a bad record stop someone asking for prayer."""

    def test_an_unparseable_timestamp_is_skipped(self):
        bad = {"requester_chat_id": MEMBER, "created": "not a date"}
        assert P.next_allowed_at([bad], MEMBER) is None

    def test_a_missing_timestamp_is_skipped(self):
        assert P.next_allowed_at([{"requester_chat_id": MEMBER}], MEMBER) is None

    def test_a_naive_timestamp_is_tolerated(self):
        """Legacy records predate tz-aware storage."""
        from datetime import datetime
        naive = datetime.now().replace(microsecond=0).isoformat()
        reqs = [{"requester_chat_id": MEMBER, "created": naive}]
        assert P.next_allowed_at(reqs, MEMBER) is not None

    def test_a_bad_record_does_not_hide_a_good_one(self):
        reqs = [{"requester_chat_id": MEMBER, "created": "rubbish"}, _req(1)]
        assert P.next_allowed_at(reqs, MEMBER) is not None


class TestTheLimitsAreWhatWasAsked:
    def test_five_minutes_between_submissions(self):
        assert P.PRAYER_COOLDOWN == timedelta(minutes=5)

    def test_five_per_day(self):
        assert P.PRAYER_DAILY_LIMIT == 5

    def test_the_day_is_a_rolling_24_hours(self):
        assert P.PRAYER_DAILY_WINDOW == timedelta(hours=24)


class TestTheCommandEnforcesIt:
    """Correct arithmetic is worthless if /prayer never consults it."""

    @staticmethod
    async def _prayer(stored, is_prayer_admin=False):
        sent = []

        async def reply(text, **kwargs):
            sent.append(text)

        upd = MagicMock()
        upd.effective_user.id = MEMBER
        upd.effective_user.username = "member"
        upd.effective_user.full_name = "A Member"
        upd.message.reply_text = AsyncMock(side_effect=reply)
        ctx = MagicMock()
        ctx.user_data = {}

        with patch.object(P.storage, "get_prayer_requests",
                          AsyncMock(return_value=stored)), \
             patch.object(P, "get_user_prefs", AsyncMock(return_value=(None, "en"))), \
             patch.object(P.permissions, "is_prayer_admin",
                          lambda _u: is_prayer_admin), \
             patch.object(P.activity, "log_command", lambda *a, **k: None), \
             patch.object(P, "format_dt", lambda dt, tz, lang: "3:15 PM"):
            state = await P.cmd_prayer(upd, ctx)
        return state, sent

    async def test_a_limited_member_is_refused_and_the_conversation_ends(self):
        from telegram.ext import ConversationHandler
        state, sent = await self._prayer([_req(1)])
        assert state == ConversationHandler.END
        assert sent and "3:15 PM" in sent[0]

    async def test_a_limited_member_is_never_asked_to_write(self):
        """The whole point of checking at entry: do not make someone type out a
        crisis and then refuse it."""
        _, sent = await self._prayer([_req(1)])
        assert len(sent) == 1
        assert "2048" not in sent[0], "that is the prompt to write a request"

    async def test_an_unlimited_member_gets_the_prompt(self):
        state, sent = await self._prayer([])
        assert state == P.PR_TEXT
        assert sent and "2048" in sent[0]

    async def test_a_member_past_the_cooldown_gets_the_prompt(self):
        state, _ = await self._prayer([_req(6)])
        assert state == P.PR_TEXT

    async def test_prayer_admins_are_exempt(self):
        """They may be submitting for someone who phoned in or has no Telegram."""
        state, _ = await self._prayer([_req(1)], is_prayer_admin=True)
        assert state == P.PR_TEXT

    async def test_the_refusal_points_somewhere_for_an_emergency(self):
        """A limit must never be a dead end for someone in crisis."""
        _, sent = await self._prayer([_req(1)])
        assert "urgent" in sent[0].lower()

    async def test_the_refusal_is_localized(self):
        from localization import t
        for lang in ("en", "es", "fr", "zu"):
            msg = t("prayer_rate_limited", lang, when="3:15 PM")
            assert msg and msg != "prayer_rate_limited", f"missing {lang}"
            assert "3:15 PM" in msg, f"{lang} drops the time"
