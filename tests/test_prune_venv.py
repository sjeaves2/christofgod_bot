"""The deploy-time pruner must never remove something still needed.

`pip install -r requirements.txt` installs but never removes, so a dependency
dropped from requirements.txt lives in the venv forever and keeps its
advisories. Removing deep-translator on 2026-10-13 would otherwise have left
requests, urllib3 and beautifulsoup4 behind — carrying the four CVEs the
removal was meant to clear.

The asymmetry governs the design: wrongly KEEPING a package wastes a few
megabytes, wrongly REMOVING one stops the bot starting. So the pruner is
conservative, update.sh verifies `import bot` after pruning and before touching
the running service, and a failed verification reinstalls requirements rather
than leaving a half-stripped venv behind.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "deploy"))

import prune_venv  # noqa: E402


class TestNameNormalisation:
    """PyPI treats '-', '_' and case as the same; metadata does not."""

    @pytest.mark.parametrize("a,b", [
        ("deep-translator", "deep_translator"),
        ("PyYAML", "pyyaml"),
        ("ruamel.yaml", "ruamel-yaml"),
        ("python-telegram-bot", "Python_Telegram_Bot"),
    ])
    def test_equivalent_spellings_normalise_together(self, a, b):
        assert prune_venv._norm(a) == prune_venv._norm(b)


class TestRequirementParsing:
    def test_plain_requirement(self):
        assert prune_venv._parse("PyYAML>=6.0") == ("pyyaml", set())

    def test_extras_are_captured(self):
        """The [job-queue] extra is why APScheduler must be kept."""
        assert prune_venv._parse("python-telegram-bot[job-queue]>=21.0,<22.0") == (
            "python-telegram-bot", {"job-queue"})

    def test_comments_and_blanks_are_ignored(self):
        assert prune_venv._parse("# a comment") is None
        assert prune_venv._parse("   ") is None

    def test_flags_are_ignored(self):
        """'-r requirements.txt' is not a package."""
        assert prune_venv._parse("-r requirements.txt") is None

    def test_trailing_comment_is_stripped(self):
        assert prune_venv._parse("pytz>=2023.3  # tz data") == ("pytz", set())


class TestClosureAgainstThisProject:
    """Run against the real requirements.txt and this interpreter's packages."""

    @pytest.fixture(scope="class")
    def keep(self):
        return prune_venv.closure(REPO / "requirements.txt")

    def test_direct_requirements_are_kept(self, keep):
        for name in ("python-telegram-bot", "pyyaml", "pytz", "icalendar",
                     "reportlab", "pyluach", "babel", "ruamel-yaml"):
            assert name in keep, f"{name} is in requirements.txt but not kept"

    def test_the_closure_is_not_trivially_small(self, keep):
        """A closure of just the direct names would prune every transitive dep."""
        assert len(keep) > 8, f"closure looks truncated: {sorted(keep)}"

    def test_transitive_dependencies_are_kept(self, keep):
        """httpx arrives only through python-telegram-bot."""
        assert "httpx" in keep

    def test_the_extra_pulls_its_dependency(self, keep):
        """APScheduler comes from python-telegram-bot[job-queue] and nowhere
        else. If extras were ignored it would be pruned and the JobQueue — every
        reminder the bot sends — would stop existing."""
        assert "apscheduler" in keep

    def test_the_removed_library_is_not_kept(self, keep):
        assert "deep-translator" not in keep
        assert "requests" not in keep, "requests came only via deep-translator"

    def test_packaging_tools_are_declared_untouchable(self):
        assert {"pip", "setuptools", "wheel"} <= prune_venv.ALWAYS_KEEP

    def test_packaging_tools_are_never_orphans(self, monkeypatch, tmp_path):
        """Removing pip would make the NEXT deploy impossible.

        This installs a fake distribution list rather than reading the ambient
        one: the venv the suite runs in has no pip metadata at all (uv omits
        it), so an environment-dependent version of this test passed no matter
        what the code did. It was vacuous until this was noticed.
        """
        class _Dist:
            def __init__(self, name):
                self.metadata = {"Name": name}

        monkeypatch.setattr(prune_venv.md, "distributions",
                            lambda: [_Dist("pip"), _Dist("setuptools"),
                                     _Dist("wheel"), _Dist("left-over-pkg")])
        req = tmp_path / "r.txt"
        req.write_text("PyYAML>=6.0\n")
        found = {prune_venv._norm(n) for n in prune_venv.orphans(req)}
        assert "left-over-pkg" in found, "the sweep must still find real orphans"
        assert not (found & {"pip", "setuptools", "wheel"})


class TestConservatism:
    def test_an_unparseable_requirement_does_not_shrink_the_closure(self, tmp_path):
        req = tmp_path / "r.txt"
        req.write_text("PyYAML>=6.0\n!!!garbage!!!\npytz\n")
        keep = prune_venv.closure(req)
        assert {"pyyaml", "pytz"} <= keep

    def test_an_uninstalled_requirement_is_still_kept(self, tmp_path):
        """A package named in requirements but absent from this venv must not
        make the walk fail — it simply has no metadata to recurse into."""
        req = tmp_path / "r.txt"
        req.write_text("definitely-not-installed-xyz>=1.0\nPyYAML>=6.0\n")
        keep = prune_venv.closure(req)
        assert "definitely-not-installed-xyz" in keep and "pyyaml" in keep


class TestUpdateScriptUsesIt:
    @staticmethod
    def _text():
        return (REPO / "deploy" / "update.sh").read_text()

    def test_update_prunes(self):
        assert "prune_venv.py" in self._text()

    def test_it_prunes_before_verifying_the_import(self):
        """The import check is what proves the prune was safe, so it must come
        after it — and both before the service is restarted."""
        t = self._text()
        assert t.index("prune_venv.py") < t.index('import bot'), \
            "pruning must happen before the verification that validates it"
        assert t.index('import bot') < t.index("Restarting"), \
            "verification must happen before the running bot is touched"

    def test_a_failed_verification_restores_the_virtualenv(self):
        """git reset undoes code, not an uninstall."""
        t = self._text()
        tail = t[t.index('New code fails to import'):]
        assert "pip" in tail and "requirements.txt" in tail, (
            "a bad prune would leave the venv broken after rollback"
        )
