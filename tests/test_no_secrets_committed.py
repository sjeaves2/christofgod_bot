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
ALLOWED_ZOOM = {("0000000000", "REPLACE_ME")}

# Files that legitimately describe the patterns without containing a secret.
EXEMPT = {"tests/test_no_secrets_committed.py"}


def _tracked_text_files() -> list[Path]:
    """Every file git tracks, skipping binaries and this test itself."""
    out = subprocess.run(["git", "ls-files"], cwd=REPO,
                         capture_output=True, text=True).stdout
    files = []
    for name in out.splitlines():
        name = name.strip()
        if not name or name in EXEMPT:
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

    def test_live_config_is_not_tracked(self):
        """config/config.yaml holds the live token; only the example ships."""
        tracked = subprocess.run(["git", "ls-files", "config/"], cwd=REPO,
                                 capture_output=True, text=True).stdout.split()
        assert "config/config.yaml" not in tracked
        assert "config/config.yaml.example" in tracked

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
