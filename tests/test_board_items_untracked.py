"""data/board_items.yaml must stay OUT of git.

Item descriptions hold what a corporate renewal needs: trademark serial
numbers, EIN and filing references, portal logins' URLs. This repository is
public by choice, and the same mistake has already been made twice here with
Zoom join links — once for three months. This test is the thing that would have
caught it the first time.

It also guards the template, by the same rule the events template is guarded
by: every value in a tracked example file must be a placeholder.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TEMPLATE = REPO / "data" / "board_items.yaml.example"


def _tracked() -> set:
    out = subprocess.run(["git", "ls-files", "data/"], cwd=REPO,
                         capture_output=True, text=True).stdout
    return {line.strip() for line in out.splitlines() if line.strip()}


class TestLiveFileIsNotTracked:
    def test_not_tracked(self):
        assert "data/board_items.yaml" not in _tracked(), (
            "data/board_items.yaml is tracked — it holds registration and filing "
            "references, and this repository is public"
        )

    def test_gitignore_covers_it(self):
        result = subprocess.run(["git", "check-ignore", "data/board_items.yaml"],
                                cwd=REPO, capture_output=True, text=True)
        assert result.returncode == 0, "data/board_items.yaml is not gitignored"

    def test_template_is_tracked(self):
        assert "data/board_items.yaml.example" in _tracked()


class TestTemplateCarriesNoRealData:
    def test_template_exists(self):
        assert TEMPLATE.is_file()

    def test_template_is_valid_yaml(self):
        import yaml
        data = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
        assert isinstance(data, dict)
        assert "items" in data

    def test_every_url_is_a_placeholder_host(self):
        """example.test is reserved by RFC 6761 and resolves nowhere. A real
        host in a fixture or template is how the Zoom links leaked."""
        text = TEMPLATE.read_text(encoding="utf-8")
        hosts = set(re.findall(r"https?://([^/\s\"']+)", text))
        assert hosts <= {"example.test"}, f"template names real hosts: {hosts}"

    def test_no_federal_identifier_shaped_values(self):
        """An EIN (12-3456789) in a tracked file is a real disclosure."""
        text = TEMPLATE.read_text(encoding="utf-8")
        assert not re.findall(r"\b\d{2}-\d{7}\b", text)

    def test_example_due_dates_are_obviously_fictional(self):
        """A plausible date invites someone to treat the template as live."""
        text = TEMPLATE.read_text(encoding="utf-8")
        for due in re.findall(r"due:\s*(\d{4})-\d{2}-\d{2}", text):
            assert int(due) >= 2099, f"template has a plausible due date: {due}"

    def test_template_documents_the_fields_a_human_needs(self):
        text = TEMPLATE.read_text(encoding="utf-8")
        for key in ("action:", "due:", "notify_days:", "description:", "url:",
                    "status:"):
            assert key in text, f"template is missing {key!r}"

    def test_template_explains_how_to_use_it(self):
        text = TEMPLATE.read_text(encoding="utf-8")
        assert "cp data/board_items.yaml.example data/board_items.yaml" in text

    def test_template_says_the_live_file_is_untracked(self):
        """Whoever next edits this file should learn why from the file."""
        assert "NOT tracked" in TEMPLATE.read_text(encoding="utf-8")
