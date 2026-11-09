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

import fnmatch
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github" / "workflows" / "tests.yml"


@pytest.fixture(scope="module")
def wf():
    return yaml.safe_load(WORKFLOW.read_text())


@pytest.fixture(scope="module")
def triggers(wf):
    """The `on:` block.

    PyYAML resolves the bare key `on` to the boolean True (YAML 1.1), so it
    cannot simply be looked up by name.
    """
    return wf[True] if True in wf else wf["on"]


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
    def test_there_is_a_schedule(self, triggers):
        """An advisory lands against unchanged code. Waiting for the next push
        means waiting indefinitely."""
        assert "schedule" in triggers, (
            "without a schedule this only runs when someone happens to commit"
        )

    def test_the_schedule_is_at_least_weekly(self, triggers):
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


def _matches(triggers, event: str, branch: str) -> bool:
    """Would *event* fire, given the branch its `branches` filter examines?

    An absent filter matches every branch, and fnmatch's `*` crosses `/`,
    which is what Actions' `**` means.

    The caller supplies the right branch for the event, because the two are
    NOT the same thing — see _push_fires and _pr_fires below.
    """
    if event not in triggers:
        return False
    patterns = (triggers[event] or {}).get("branches")
    if patterns is None:
        return True
    return any(fnmatch.fnmatch(branch, p) for p in patterns)


def _push_fires(triggers, pushed_branch: str) -> bool:
    """`push.branches` filters the branch being PUSHED."""
    return _matches(triggers, "push", pushed_branch)


def _pr_fires(triggers, base: str = "main") -> bool:
    """`pull_request.branches` filters the BASE branch — the PR's target — not
    the branch the commits are on.

    Worth spelling out: an earlier version of this helper passed a feature
    branch to both events, which made `pull_request: branches: [main]` look
    like "PRs get no CI" when in fact it covers every PR in this repository,
    since they all target main. A control mutation that was expected to pass
    failed, which is how the conflation surfaced.
    """
    return _matches(triggers, "pull_request", base)


class TestOneCommitDoesNotRunTheWorkflowTwice:
    """`push: ["**"]` alongside `pull_request` ran everything TWICE for every
    commit on a PR branch — six jobs per commit instead of three, identical
    SHAs. Measured 2026-11-02: 444de36b produced a push run at 20:46:33 and a
    pull_request run at 20:46:55, both green and both complete.

    Asked as "which events fire?" rather than "what does the YAML say?", so
    these fail for any trigger shape that reintroduces the duplication, not
    only for the exact one that caused it.
    """

    def test_a_pr_branch_commit_fires_exactly_one_event(self, triggers):
        """A commit on a feature branch with a PR open against main: the push
        must NOT start a run, because the pull_request event already does."""
        fired = []
        if _push_fires(triggers, "some-feature-branch"):
            fired.append("push")
        if _pr_fires(triggers, base="main"):
            fired.append("pull_request")
        assert fired == ["pull_request"], (
            f"a commit on a PR branch fires {fired or 'nothing'}; two events "
            "means the whole workflow runs twice for one commit")

    def test_pull_requests_are_still_checked(self, triggers):
        """The dangerous over-correction: dropping pull_request would leave
        PRs with no CI at all, and a failure would only surface after merge."""
        assert _pr_fires(triggers, base="main")

    def test_main_is_still_verified_after_a_merge(self, triggers):
        """main is what deploy/update.sh fast-forwards to, so it is the one
        branch that must be checked on push as well as in the PR."""
        assert _push_fires(triggers, "main")

    def test_pushing_a_tag_does_not_start_a_run(self, triggers):
        """Releases are tagged by hand several times a day; a `tags:` filter
        would spend a full CI run on each one, re-testing a commit that was
        already green on main."""
        assert "tags" not in (triggers.get("push") or {})

    def test_a_manual_trigger_does_not_add_an_automatic_event(self, triggers):
        """workflow_dispatch must not reintroduce the duplication above.

        It fires only when somebody asks for it, so it cannot double up with
        push or pull_request — but assert it rather than assume it, because
        the obvious wrong fix for "I cannot re-run CI" is a broader `push:`.
        """
        assert "workflow_dispatch" in triggers
        assert not _push_fires(triggers, "some-feature-branch")


class TestCIRunsCanBeRepeatedOnDemand:
    """A release gate you cannot satisfy is a release gate that gets skipped.

    On 2026-11-09 GitHub dropped one matrix leg from two consecutive runs of
    the same code: the pull_request run for #87 scheduled 3.13 but never
    3.12, the push run for its merge scheduled 3.12 but never 3.13. Each run
    reported `failure` while every job it did schedule passed. Both legs
    passed, but across two runs, so `main` had no green run.

    Recovery did not exist. GitHub refused the retry outright, and the only
    other triggers were a push to main and a weekly cron that had already
    fired. The remaining choices were to tag against the standing "green on
    main first" rule, or wait a week — for code that was fine.
    """

    def test_a_run_can_be_started_by_hand(self, triggers):
        assert "workflow_dispatch" in triggers, (
            "without workflow_dispatch the only ways to get a green run on "
            "main are another push or the weekly cron, so a dropped matrix "
            "leg blocks tagging for up to a week"
        )

    def test_it_covers_the_test_job_and_not_a_separate_stub(self, wf):
        """The trigger is worthless if the dispatched run skips the matrix.

        A dispatch must exercise the same job, on the same interpreters, as a
        push to main — otherwise a green dispatched run would not be evidence
        of anything.
        """
        job = wf["jobs"]["test"]
        versions = job["strategy"]["matrix"]["python-version"]
        assert [str(v) for v in versions] == ["3.12", "3.13"], (
            f"dispatched runs would cover {versions}, which is not what a "
            f"push to main covers"
        )
        conditioned = job.get("if")
        assert conditioned is None, (
            f"the test job is gated on `if: {conditioned}` — a manually "
            f"dispatched run could skip it entirely and still report green"
        )
