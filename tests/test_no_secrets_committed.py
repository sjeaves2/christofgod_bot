"""Fail the build if a credential is committed to this repository.

Written after two real leaks, both of which sat in a PUBLIC repo:

  * the live bot token, in tests/test_log_redaction.py as a constant named
    FAKE_TOKEN — committed 2026-08-24, public for 23 days, and the likely
    starting point of the 2026-09-16 incident in which the bot's name, photo
    and description were changed by someone else;
  * Zoom join links whose URLs embed meeting passcodes, in data/events.yaml —
    tracked from 2026-06-16 until it was untracked in v0.14.0.

Both were written by people being careful. Neither was noticed by review. The
only reliable guard is one that runs on every commit, so these tests scan the
files git actually tracks — not the working tree, which contains ignored
runtime data that is *supposed* to hold real credentials.

Adding a genuinely new kind of secret means adding a check here.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# Telegram bot token: <bot_id>:<35 chars>. Matched loosely so near-misses are
# caught too — a real token that was truncated is still a real token.
TOKEN_RE = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b")

# The ONE permitted token literal: the designated synthetic value used by
# tests/test_log_redaction.py. Anything else is a finding.
ALLOWED_TOKENS = {"111111111:AAAA-NOT-A-REAL-TOKEN-FOR-TESTS-ONLY"}

# Zoom links embed the passcode in the URL, so a link IS a credential.
ZOOM_PWD_RE = re.compile(r"zoom\.us/j/(\d+)[^\s\"']*?pwd=([A-Za-z0-9_.-]+)")
ALLOWED_ZOOM = {("0000000000", "SETMEWITHSETSERVICELINK")}

# A federal Employer Identification Number, 12-3456789. Board compliance items
# (v0.22.0) are the first feature whose free text invites one — "EIN 54-1234567,
# renewal portal login with the treasurer" is exactly what an officer would
# type into /addboarditem. data/board_items.yaml is untracked for that reason;
# this is the guard for the day someone pastes a real item into a test fixture
# or the template, which is how the Zoom links leaked twice.
EIN_RE = re.compile(r"\b\d{2}-\d{7}\b")

# A numeric Telegram chat_id, as the bot auto-fills into officials.yaml when
# someone runs /start. Not a credential, but the one identifier here that
# CANNOT be changed: a username can be swapped and a Zoom link rotated, while
# a chat_id is permanent and is enough to message that person directly.
# config/admins.yaml and config/officials.yaml carried these in a public repo
# from the repository's first commit (40a5f2c, 2026-06-16) until they were
# untracked — the third leak of this shape, after the token and the Zoom links.
# The .example templates omit chat_id entirely: the bot fills it in at runtime,
# so a template never needs one and any value here is a real person's.
# The first version of this check matched only the YAML form, `chat_id: NNN`,
# and CI then found three tracked test files holding the real ops admin's id
# as `User(id=...)` and inside a Python set. A guard aimed at the shape
# the leak happened to take the first time is shape #11 in the project notes.
# So: match any 9-to-11-digit run of digits anywhere in a tracked file, and
# allow the placeholders BY SHAPE rather than keeping a list of the real
# values, which would commit them.
#
# ELEVEN, not ten. The 9-10 version shipped and missed a real phone number
# sitting in a bot.py comment as `"+1..........."`, because a country code
# makes the run 11 digits long and the bound excluded it. Telegram user ids
# are 9-10 digits, so 10 looked like the natural ceiling — but the hazard is
# any identifier of a real person, and a phone number is the one that reaches
# them off Telegram entirely. Third widening of this check in one day; each
# time the previous bound had been drawn around the example in front of me.
LONG_DIGITS_RE = re.compile(r"(?<![\d.])(\d{9,11})(?![\d.])")

# Allowed by prefix: 555… is the test-phone convention (as in 5550001234) and
# 999… is the designated synthetic Telegram-id range (999000001). A leading
# 1 country code is stripped before the check, so "+15550001234" is allowed
# on the same grounds as "5550001234". Anything else needs a reason below.
SYNTHETIC_PREFIXES = ("555", "999")
ALLOWED_LONG_DIGITS = {
    "0000000000",   # events.py PLACEHOLDER_MEETING, and the Zoom example links
    "1234567890",   # the example Zoom meeting id, used across the event tests
    "111111111",    # the bot_id half of the one permitted synthetic token
    "1896455498",   # Apple album id, in apple_music.py's worked example
    "6767277651",   # Apple track id, same example
    "7575550000",   # README's example admin phone (757 area code + 555)
    "7575550001",   # README's example official phone
}


def _is_placeholder(n: str) -> bool:
    """Whether a long run of digits is a designated placeholder.

    A leading 1 is a country code, not part of the pattern: "+15550001234"
    is the same placeholder as "5550001234" and must not need its own entry.
    """
    core = n[1:] if len(n) == 11 and n.startswith("1") else n
    return n in ALLOWED_LONG_DIGITS or core.startswith(SYNTHETIC_PREFIXES)

# Files that legitimately describe the patterns without containing a secret.
EXEMPT = {"tests/test_no_secrets_committed.py",
          "tests/test_board_items_untracked.py"}


def _tracked_text_files(respect_exempt: bool = True) -> list[Path]:
    """Every file git tracks, skipping binaries and this test itself.

    `respect_exempt=False` includes the EXEMPT files. The long-digits check
    uses it, because that check allows values by SHAPE and by a documented
    list rather than by file — so it has no reason to skip anything, and
    skipping is how a real chat_id got into the prose of THIS file on
    2026-11-04 and shipped: the guard exempted itself, so nothing read the
    comment explaining the leak it was written for.
    """
    out = subprocess.run(["git", "ls-files"], cwd=REPO,
                         capture_output=True, text=True).stdout
    files = []
    for name in out.splitlines():
        name = name.strip()
        if not name or (respect_exempt and name in EXEMPT):
            continue
        p = REPO / name
        if not p.is_file():
            continue
        try:
            p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue          # binary or unreadable — nothing to scan
        files.append(p)
    return files


class TestNoCredentialsTracked:
    def test_git_ls_files_works(self):
        """Guard against the scan silently covering nothing."""
        files = _tracked_text_files()
        assert len(files) > 50, (
            f"only {len(files)} tracked text files found — the scan below would "
            "be vacuous, so something is wrong with the file discovery"
        )

    def test_no_bot_token_committed(self):
        findings = []
        for p in _tracked_text_files():
            for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                for m in TOKEN_RE.finditer(line):
                    if m.group(0) not in ALLOWED_TOKENS:
                        rel = p.relative_to(REPO)
                        findings.append(f"{rel}:{i} — {m.group(0)[:12]}…")
        assert not findings, (
            "a Telegram bot token appears in a tracked file:\n  "
            + "\n  ".join(findings)
            + "\n\nRevoke it in BotFather FIRST (a public repo is already scraped; "
              "rewriting history does not help), then replace it with the "
              "designated synthetic token."
        )

    def test_no_real_zoom_passcodes_committed(self):
        findings = []
        for p in _tracked_text_files():
            for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                for m in ZOOM_PWD_RE.finditer(line):
                    if (m.group(1), m.group(2)) not in ALLOWED_ZOOM:
                        rel = p.relative_to(REPO)
                        findings.append(f"{rel}:{i} — meeting {m.group(1)}")
        assert not findings, (
            "a Zoom link with an embedded passcode appears in a tracked file:\n  "
            + "\n  ".join(findings)
            + "\n\nJoin links belong in data/events.yaml (untracked) and are set "
              "with /setservicelink, never committed."
        )

    def test_no_federal_tax_id_committed(self):
        findings = []
        for p in _tracked_text_files():
            for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                for m in EIN_RE.finditer(line):
                    rel = p.relative_to(REPO)
                    findings.append(f"{rel}:{i} — {m.group(0)}")
        assert not findings, (
            "something shaped like a federal tax ID (EIN) appears in a tracked "
            "file:\n  " + "\n  ".join(findings)
            + "\n\nBoard compliance details belong in data/board_items.yaml, "
              "which is untracked. If this is a deliberate placeholder, use a "
              "form that cannot be mistaken for real and exempt the file here."
        )

    def test_live_config_is_not_tracked(self):
        """config/config.yaml holds the live token; only the example ships."""
        tracked = subprocess.run(["git", "ls-files", "config/"], cwd=REPO,
                                 capture_output=True, text=True).stdout.split()
        assert "config/config.yaml" not in tracked
        assert "config/config.yaml.example" in tracked

    def test_member_config_is_not_tracked(self):
        """admins.yaml and officials.yaml name real people; only examples ship.

        Both were tracked from 40a5f2c (2026-06-16) until this change, in a
        repository that is public by choice. admins.yaml also records which
        admins read prayer requests, which members are told stays with the
        ministry's leadership.
        """
        tracked = subprocess.run(["git", "ls-files", "config/"], cwd=REPO,
                                 capture_output=True, text=True).stdout.split()
        for name in ("config/admins.yaml", "config/officials.yaml"):
            assert name not in tracked, (
                f"{name} names real members and must not be tracked; ship "
                f"{name}.example instead"
            )
            assert f"{name}.example" in tracked, (
                f"{name} is untracked, so {name}.example must ship or a fresh "
                "checkout cannot start the bot and CI cannot import it"
            )

    def test_no_telegram_chat_id_committed(self):
        """A chat_id cannot be rotated, unlike a username or a Zoom link."""
        findings = []
        for p in _tracked_text_files(respect_exempt=False):
            for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                for m in LONG_DIGITS_RE.finditer(line):
                    n = m.group(1)
                    if _is_placeholder(n):
                        continue
                    findings.append(f"{p.relative_to(REPO)}:{i} — {n}")
        assert not findings, (
            "a 9-to-11-digit number appears in a tracked file, which is the "
            "shape of a Telegram user id or a phone number:\n  "
            + "\n  ".join(findings)
            + "\n\nA chat_id is PERMANENT — unlike a username or a Zoom link "
              "it cannot be rotated once published, so it must never be "
              "committed. Use the 999… synthetic range in tests (999000001), "
              "and 555… for phone numbers. The bot auto-fills real chat_ids at "
              "runtime, so a config template never needs one: omit the field. "
              "If this really is a harmless constant, add it to "
              "ALLOWED_LONG_DIGITS above WITH a comment saying what it is."
        )

    def test_runtime_data_is_not_tracked(self):
        """data/events.yaml carries join links and is rewritten at runtime."""
        tracked = subprocess.run(["git", "ls-files", "data/"], cwd=REPO,
                                 capture_output=True, text=True).stdout.split()
        assert "data/events.yaml" not in tracked
        for name in ("data/users.yaml", "data/appointments.yaml"):
            assert name not in tracked, f"{name} holds personal data and must not be tracked"

    def test_logs_are_not_tracked(self):
        """bot.log held the token 98 times before redaction was fixed."""
        tracked = subprocess.run(["git", "ls-files", "logs/"], cwd=REPO,
                                 capture_output=True, text=True).stdout.split()
        assert not tracked, f"log files are tracked: {tracked}"
