"""No test may build a fixture from the machine's clock.

WHAT WENT WRONG
---------------
On 2026-11-03 at 01:12 UTC — 20:12 the previous evening in church time — six
tests in tests/test_board_notices.py failed on CI:

    assert '10 days' in '... Due 2026-11-13 — in 11 days ...'

The bot was right. The tests were wrong, and had been from the day they were
written. Every date decision in the bot goes through `now_tz().date()`
(America/New_York); those fixtures used `date.today()`, which follows the
RUNNER's timezone. On a UTC runner the two disagree for the five hours between
19:00 church time and midnight, so the fixture said "due in 10 days" while the
code computed 11.

A second instance failed in the other direction: a prayer test built a naive
"just now" timestamp with `datetime.now()`, which production localizes to
church time, so on a machine BEHIND New York the record read as hours old and
a cooldown looked expired. That one needed TZ=America/Los_Angeles to show.

Neither was caught by review, by 2000 passing tests, or by any CI run, because
CI had never happened to run inside the window. A test whose result depends on
what time of day — or in which country — it runs is not a passing test; it is
an unexploded one.

WHAT THIS ENFORCES
------------------
Fixtures use `now_tz()`, the same clock as the code under test. `date.today()`
and a bare `datetime.now()` are banned outright rather than allow-listed: an
allowlist is where the next one would hide.

The scan matches source TEXT, so neither call may be spelled out anywhere in a
test file — prose and docstrings included. The patterns themselves live in this
file, which exempts itself. (The first draft of the fix tripped this rule with
a docstring that quoted the banned call while explaining it.)
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

TESTS = Path(__file__).parent
#: `date.today()`, and `datetime.now()` with no timezone argument.
BANNED = (
    (re.compile(r"\bdate\.today\(\)"), "date.today()", "now_tz().date()"),
    (re.compile(r"\bdatetime\.now\(\s*\)"), "datetime.now()", "now_tz()"),
)


def _test_sources() -> list[Path]:
    return sorted(p for p in TESTS.glob("test_*.py") if p.name != Path(__file__).name)


class TestTheSweepIsReal:
    """A scan over nothing passes vacuously, and would keep passing after
    someone moved the tests."""

    def test_it_finds_the_test_files(self):
        assert len(_test_sources()) >= 40

    def test_it_would_catch_a_banned_call(self):
        """The patterns are checked against known-bad text, so a typo in a
        regex cannot silently disable this file."""
        for pattern, label, _ in BANNED:
            sample = f"due = {label}"
            assert pattern.search(sample), label

    def test_it_does_not_flag_the_approved_form(self):
        for pattern, _label, replacement in BANNED:
            assert not pattern.search(f"due = {replacement}")
        # nor a tz-aware now(), which is explicit about its clock
        assert not BANNED[1][0].search("datetime.now(TZ)")
        assert not BANNED[1][0].search("datetime.now(tz=TZ)")


class TestNoTestUsesTheMachineClock:
    def test_no_banned_clock_calls(self):
        offenders = []
        for path in _test_sources():
            for n, line in enumerate(path.read_text().splitlines(), 1):
                if line.lstrip().startswith("#"):
                    continue
                for pattern, label, replacement in BANNED:
                    if pattern.search(line):
                        offenders.append(
                            f"{path.name}:{n} uses {label} — use {replacement}")
        assert not offenders, (
            "a fixture built on the machine's clock contradicts code that "
            "uses church time, and fails only in some timezones or at some "
            "hours:\n  " + "\n  ".join(offenders))
