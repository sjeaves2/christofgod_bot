"""data/events.yaml must stay OUT of git.

Two separate problems came from tracking it:

1. Its join-link URLs embed Zoom passcodes, so they were published in a public
   repository (2026-06-16 to 2026-09-14).
2. The bot rewrites it at runtime (/setservicelink, /addevent, /modifyevent), so
   every admin edit left the server's working tree dirty and deploy/update.sh
   refused to deploy.

These tests exist so a later "why isn't the event config in git?" doesn't
quietly undo it.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LIVE = REPO / "data" / "events.yaml"
TEMPLATE = REPO / "data" / "events.yaml.example"


def _tracked() -> set:
    out = subprocess.run(["git", "ls-files", "data/"], cwd=REPO,
                         capture_output=True, text=True).stdout
    return {line.strip() for line in out.splitlines() if line.strip()}


class TestEventsConfigIsNotTracked:
    def test_live_file_is_not_tracked(self):
        assert "data/events.yaml" not in _tracked(), (
            "data/events.yaml is tracked again — it holds Zoom passcodes and the "
            "bot rewrites it at runtime, which also blocks deploy/update.sh"
        )

    def test_gitignore_covers_it(self):
        ignored = subprocess.run(
            ["git", "check-ignore", "data/events.yaml"], cwd=REPO,
            capture_output=True, text=True)
        assert ignored.returncode == 0, "data/events.yaml is not gitignored"

    def test_template_is_tracked(self):
        assert "data/events.yaml.example" in _tracked(), (
            "the template must be tracked, or a fresh clone has no event config"
        )


class TestTemplateCarriesNoSecrets:
    def test_template_exists(self):
        assert TEMPLATE.is_file()

    def test_no_real_zoom_passcodes(self):
        """Every link in the template must be a placeholder."""
        text = TEMPLATE.read_text(encoding="utf-8")
        real = [m for m in re.findall(r"pwd=([^\s&\"']+)", text) if m != "SETMEWITHSETSERVICELINK"]
        assert not real, f"template contains real Zoom passcodes: {real}"

    def test_no_real_meeting_ids(self):
        text = TEMPLATE.read_text(encoding="utf-8")
        ids = {m for m in re.findall(r"zoom\.us/j/(\d+)", text)}
        assert ids <= {"0000000000"}, f"template contains real meeting ids: {ids}"

    def test_no_real_chat_ids(self):
        """Group chat ids identify the congregation's private groups."""
        text = TEMPLATE.read_text(encoding="utf-8")
        ids = {m for m in re.findall(r"(-100\d{10,})", text)}
        assert ids <= {"-1001234567890"}, f"template contains real chat ids: {ids}"

    def test_template_keeps_the_structure_admins_need(self):
        """A template that omits a section is worse than none — the admin gets a
        bot that silently does nothing for that feature."""
        text = TEMPLATE.read_text(encoding="utf-8")
        for key in ("notification_targets:", "convocation_targets_default:",
                    "convocation_targets:", "special_events:",
                    "convocation_announcements:", "convocation_urls:"):
            assert key in text, f"template is missing the {key!r} section"

    def test_template_is_valid_yaml(self):
        import yaml
        data = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
        assert isinstance(data, dict)
        assert "special_events" in data

    def test_template_explains_how_to_use_it(self):
        text = TEMPLATE.read_text(encoding="utf-8")
        assert "cp data/events.yaml.example data/events.yaml" in text
        assert "/setservicelink" in text


class TestSetupSeedsTheConfig:
    """A fresh clone has no data/events.yaml; setup.sh must create one."""

    def test_setup_copies_the_template_when_absent(self):
        script = (REPO / "deploy" / "setup.sh").read_text(encoding="utf-8")
        assert "cp data/events.yaml.example data/events.yaml" in script

    def test_setup_does_not_clobber_an_existing_config(self):
        """Re-running setup.sh must never overwrite live event configuration."""
        script = (REPO / "deploy" / "setup.sh").read_text(encoding="utf-8")
        seed = script[script.index("Seeding event configuration"):]
        seed = seed[:seed.index("Checking configuration")]
        assert "if [ ! -f data/events.yaml ]" in seed, (
            "the copy must be guarded by an existence check"
        )
