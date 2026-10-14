"""CI must audit the bot's dependencies, and must fail when one is vulnerable.

The 2026-10-13 review found four advisories by hand. Nothing would have found
them otherwise, and nothing would find the next one: an advisory is published
against code that has not changed, so there is no commit to trigger a check.
This repository goes weeks between pushes.

These tests pin the workflow's shape. They cannot run GitHub Actions, so they
guard the decisions that were verified by hand on 2026-10-14 and are easy to
undo later without noticing.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github" / "workflows" / "tests.yml"


@pytest.fixture(scope="module")
def wf():
    return yaml.safe_load(WORKFLOW.read_text())


@pytest.fixture(scope="module")
def audit(wf):
    assert "audit" in wf["jobs"], (
        "the dependency audit job is gone; a new advisory would go unnoticed "
        "until someone thought to run pip-audit by hand"
    )
    return wf["jobs"]["audit"]


def _run_script(job) -> str:
    return "\n".join(s.get("run", "") for s in job["steps"])


class TestTheJobExists:
    def test_it_runs_pip_audit(self, audit):
        assert "pip-audit" in _run_script(audit)

    def test_it_installs_production_requirements(self, audit):
        script = _run_script(audit)
        assert "requirements.txt" in script
        assert "requirements-dev.txt" not in script, (
            "dev tooling never reaches the server; auditing it would block "
            "merges over advisories in pytest"
        )


class TestItFailsTheBuild:
    def test_strict_is_set(self, audit):
        """Without --strict, pip-audit exits 0 even when it finds something."""
        assert "--strict" in _run_script(audit)

    def test_the_step_is_not_allowed_to_fail(self, audit):
        for step in audit["steps"]:
            assert not step.get("continue-on-error"), (
                f"step {step.get('name')!r} cannot fail the build, so an "
                "advisory would be reported and then ignored"
            )
        assert not audit.get("continue-on-error")


class TestTheTwoMistakesThatWereMade:
    """Both of these were tried on 2026-10-14 and silently gave wrong answers."""

    def test_it_audits_a_freeze_not_the_requirements_file(self, audit):
        """`pip-audit -r requirements.txt` does NOT resolve transitive
        dependencies. Given only deep-translator it reports that package and
        never looks at urllib3 — where three of October's four advisories
        were. A freeze names every installed package."""
        script = _run_script(audit)
        assert "pip freeze" in script, "auditing requirements.txt misses transitive deps"

    def test_the_audit_tool_is_installed_somewhere_separate(self, audit):
        """pip-audit drags in its own requests/urllib3. Installed into the
        audited environment, the job reports advisories in the audit tool."""
        script = _run_script(audit)
        assert "venv" in script and "audit-tool" in script, (
            "pip-audit must not be installed into the environment it audits"
        )

    def test_the_interpreter_matches_the_server(self, audit):
        """The resolver's choice depends on the interpreter: on 3.9 it picks a
        pillow with advisories, on 3.12 one without. Auditing under the wrong
        version reports vulnerabilities nobody is running."""
        versions = [s.get("with", {}).get("python-version")
                    for s in audit["steps"] if s.get("with")]
        assert "3.12" in [str(v) for v in versions if v]


class TestItRunsWithoutACommit:
    def test_there_is_a_schedule(self, wf):
        """An advisory lands against unchanged code. Waiting for the next push
        means waiting indefinitely."""
        triggers = wf[True] if True in wf else wf["on"]
        assert "schedule" in triggers, (
            "without a schedule this only runs when someone happens to commit"
        )

    def test_the_schedule_is_at_least_weekly(self, wf):
        triggers = wf[True] if True in wf else wf["on"]
        cron = triggers["schedule"][0]["cron"]
        day_of_month, month, _day_of_week = cron.split()[2:5]
        # day-of-month and month must both be wildcards: pinning either makes
        # the run monthly or yearly. With both free, the day-of-week field
        # gives weekly at worst and daily at best.
        assert day_of_month == "*" and month == "*", (
            f"{cron} runs less often than weekly"
        )


class TestTheWorkflowStillParses:
    def test_it_is_valid_yaml(self):
        assert yaml.safe_load(WORKFLOW.read_text())

    def test_the_test_job_survived(self, wf):
        """Adding the audit must not have displaced the suite."""
        assert "test" in wf["jobs"]
        assert "pytest" in "\n".join(s.get("run", "")
                                     for s in wf["jobs"]["test"]["steps"])
