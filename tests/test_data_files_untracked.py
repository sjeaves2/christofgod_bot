"""Every file the bot writes under data/ must be gitignored.

Written after a near-miss on 2026-10-29. `data/prayer_requests.yaml` — the most
sensitive file the bot holds, carrying health, finances, bereavement and family
crises in members' own words — **had no .gitignore entry**, in a public
repository. It had never been committed, but `git add -A` would have staged it,
and the standing workflow runs exactly that before a commit.

Two things kept the blast radius near zero, and neither is a safeguard:

* handlers/prayer.py deletes a request's text the moment it is answered or
  dismissed and purges the stub after 30 days, so the live file is usually a
  few bytes. A mitigation, not a control.
* the file simply had not existed locally when earlier `git add -A` runs
  happened. Luck.

The per-file guards that DID exist (tests/test_events_config_untracked.py,
tests/test_board_items_untracked.py) were written one at a time, each after its
own incident — so the one file nobody had written a guard for was the one left
exposed. This test is deliberately general instead: it derives the list from
storage.py, so a store added in future is covered the day it is added rather
than the day it leaks.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _data_files() -> list[str]:
    """Every `DATA_DIR / "..."` path storage.py opens a cache on."""
    src = (REPO / "storage.py").read_text(encoding="utf-8")
    return sorted({f"data/{n}" for n in re.findall(r'DATA_DIR / "([^"]+)"', src)})


def _is_ignored(rel: str) -> bool:
    return subprocess.run(["git", "check-ignore", rel], cwd=REPO,
                          capture_output=True).returncode == 0


def _tracked() -> set[str]:
    out = subprocess.run(["git", "ls-files", "data/"], cwd=REPO,
                         capture_output=True, text=True).stdout
    return {line.strip() for line in out.splitlines() if line.strip()}


class TestTheSweepSeesTheStores:
    def test_the_list_was_discovered(self):
        """Guards every test below: an empty list passes vacuously, which is
        precisely how the gap survived — nothing was looking."""
        files = _data_files()
        assert len(files) >= 8, f"only found {files}"

    def test_it_includes_the_one_that_was_missed(self):
        assert "data/prayer_requests.yaml" in _data_files()

    def test_the_discovery_pattern_still_matches_storage(self):
        """If storage.py stops using `DATA_DIR / "name"` the regex silently
        finds nothing, so pin the shape it depends on."""
        src = (REPO / "storage.py").read_text(encoding="utf-8")
        assert src.count("DATA_DIR / ") == len(_data_files())


class TestEveryStoreIsIgnored:
    @pytest.mark.parametrize("rel", _data_files())
    def test_it_is_gitignored(self, rel):
        assert _is_ignored(rel), (
            f"{rel} has no .gitignore entry. Everything the bot writes under "
            "data/ is personal or organisational data and this repository is "
            "public. Add the entry; do not rely on the file happening to be "
            "absent when someone runs `git add -A`."
        )

    @pytest.mark.parametrize("rel", _data_files())
    def test_it_is_not_tracked(self, rel):
        assert rel not in _tracked(), f"{rel} is tracked in git"


class TestTemplatesAreTrackedInstead:
    """A fresh clone needs the shape of the files it does not get."""

    @pytest.mark.parametrize("rel", ["data/events.yaml", "data/board_items.yaml"])
    def test_the_documented_stores_ship_a_template(self, rel):
        assert f"{rel}.example" in _tracked(), f"{rel}.example is not tracked"

    def test_no_template_carries_a_federal_tax_id(self):
        """A template is tracked, so it is published.

        Only the universal rule is checked here. HOST policy deliberately is
        not: it differs per template and the per-file guards know which is
        right. data/events.yaml.example legitimately shows `us06web.zoom.us`,
        because an admin needs the real shape of a join link — what must be a
        placeholder there is the meeting id and passcode, which
        tests/test_events_config_untracked.py enforces. Asserting
        `example.test` everywhere, as this test first did, contradicts that
        guard rather than adding to it. The `example.test` rule applies to test
        FIXTURES, where a realistic Zoom URL is itself a credential.
        """
        for example in sorted((REPO / "data").glob("*.example")):
            text = example.read_text(encoding="utf-8")
            assert not re.findall(r"\b\d{2}-\d{7}\b", text), example.name
