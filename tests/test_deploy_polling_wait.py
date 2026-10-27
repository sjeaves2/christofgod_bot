"""deploy/update.sh must wait long enough for the bot to reach Telegram.

On 2026-10-27 the v0.22.0 deploy reported failure and the line it was waiting
for arrived ONE SECOND after the loop gave up:

    16:52:10  systemctl restart returns, the 20-second wait begins
    16:52:20  Python finishes importing — half the budget gone
    16:52:30  the wait expires, the script prints a failure and exits 1
    16:52:31  "Long polling started"

Nothing was broken and nothing was rolled back, but a check that cries wolf
teaches the reader to ignore it — and this check exists because `systemctl
is-active` reported "active" for 87 minutes during the 2026-09-04 outage while
the bot reached nobody. A false alarm here is not cosmetic.

These tests RUN the wait loop against a stubbed journalctl rather than reading
the budget out of the file. That is deliberate: two earlier tests on this same
script passed while the code was broken, because the explanatory comment quoted
the very value being asserted, so grepping the file matched the prose instead
of the command. A behavioural test cannot be fooled that way — and the loop is
ten lines of shell, so there is no excuse for testing it by eye.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "deploy" / "update.sh"

#: The old budget, which was one second short in practice. Kept as a named
#: constant so the regression is stated rather than implied.
PREVIOUS_WAIT_SECONDS = 20

#: What the restart actually needed on 2026-10-27, measured from the journal.
MEASURED_SECONDS_NEEDED = 21


def _script_text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


#: Above this, running the loop to exhaustion costs more than the information
#: is worth. The stub makes each attempt cheap but not free — a mutation
#: setting the budget to 100000 took the suite 15 minutes before this guard
#: existed, which turns a failing test into a hung CI run.
MAX_RUNNABLE_BUDGET = 300


def _poll_wait_seconds() -> int:
    """The budget, read from the assignment itself.

    Note what this does NOT do: grep the file for a number. The comment above
    the loop names both the old budget and the new one, so any search for a
    bare "60" matches the prose whatever the code says. This matches the
    assignment and nothing else, and the behavioural tests below are what
    prove the loop actually USES it.
    """
    match = re.search(r"^POLL_WAIT_SECONDS=(\d+)\s*$", _script_text(), re.M)
    assert match, "POLL_WAIT_SECONDS is no longer a plain assignment"
    return int(match.group(1))


def _wait_loop() -> str:
    """The wait loop's own source, from the budget through the loop's `done`.

    Extracted so the test can execute it in isolation: running the whole script
    would need git, pip, sudo and a systemd unit.
    """
    text = _script_text()
    start = text.index("POLL_WAIT_SECONDS=")
    end = text.index("if [ -z \"$polling\" ]", start)
    return text[start:end]


def _run_loop(tmp_path, found_on_attempt: int | None):
    """Execute the real wait loop with journalctl and sleep stubbed out.

    `found_on_attempt` is which journalctl call first reports polling, or None
    for a bot that never comes up. sleep is a no-op so a 60-second budget does
    not cost 60 seconds of test time — which also means this measures ATTEMPTS,
    the thing the budget actually controls.
    """
    if found_on_attempt is None:
        # Running to exhaustion is O(budget). Fail fast rather than hanging:
        # the bound is asserted from the assignment, not by waiting it out.
        budget = _poll_wait_seconds()
        assert budget <= MAX_RUNNABLE_BUDGET, (
            f"POLL_WAIT_SECONDS is {budget}; refusing to run the loop to "
            "exhaustion. See test_the_budget_is_bounded."
        )
    counter = tmp_path / "attempts"
    counter.write_text("0")
    harness = tmp_path / "harness.sh"
    trigger = "" if found_on_attempt is None else str(found_on_attempt)
    harness.write_text(f"""#!/usr/bin/env bash
set -uo pipefail
SERVICE_NAME="christofgod-bot"
restart_at="2026-10-27 16:52:10"
COUNTER="{counter}"
TRIGGER="{trigger}"

# Stubs. Defined as functions so they win inside the loop's pipeline.
journalctl() {{
    n=$(( $(cat "$COUNTER") + 1 ))
    echo "$n" > "$COUNTER"
    if [ -n "$TRIGGER" ] && [ "$n" -ge "$TRIGGER" ]; then
        echo "Long polling started — using getUpdates to check for incoming messages"
    else
        echo "[INFO] settings: ---- Bot process started ----"
    fi
}}
sleep() {{ :; }}

{_wait_loop()}

echo "RESULT polling=${{polling:-no}} attempts=$(cat "$COUNTER")"
""")
    proc = subprocess.run(["bash", str(harness)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    match = re.search(r"RESULT polling=(\S+) attempts=(\d+)", proc.stdout)
    assert match, f"harness produced no result line:\n{proc.stdout}\n{proc.stderr}"
    return match.group(1), int(match.group(2))


# ---------------------------------------------------------------------------
# The harness itself
# ---------------------------------------------------------------------------


class TestTheHarnessWorks:
    def test_the_loop_was_actually_extracted(self):
        """Guards every test below: an empty extraction runs nothing and every
        assertion about attempts becomes vacuous."""
        loop = _wait_loop()
        assert "for _ in" in loop
        assert "Long polling started" in loop
        assert loop.count("sleep 1") == 1

    def test_a_bot_that_comes_up_at_once_is_detected(self, tmp_path):
        polling, attempts = _run_loop(tmp_path, found_on_attempt=1)
        assert polling == "yes"
        assert attempts == 1, "it kept polling after finding the line"


# ---------------------------------------------------------------------------
# The budget
# ---------------------------------------------------------------------------


class TestTheWaitIsLongEnough:
    def test_it_waits_longer_than_the_old_budget(self, tmp_path):
        """The regression itself. Mutation check: setting POLL_WAIT_SECONDS
        back to 20 fails here."""
        _polling, attempts = _run_loop(tmp_path, found_on_attempt=None)
        assert attempts > PREVIOUS_WAIT_SECONDS, (
            f"the wait gives up after {attempts} attempts; the deploy that "
            f"prompted this needed {MEASURED_SECONDS_NEEDED}"
        )

    def test_it_covers_the_restart_that_failed(self, tmp_path):
        """The real timeline: polling arrived on what would have been attempt
        21 of a 20-attempt loop."""
        polling, attempts = _run_loop(
            tmp_path, found_on_attempt=MEASURED_SECONDS_NEEDED)
        assert polling == "yes"
        assert attempts == MEASURED_SECONDS_NEEDED

    def test_it_leaves_real_headroom(self, tmp_path):
        """Ten of the original twenty seconds went on interpreter startup
        alone, and that grows with every dependency. Matching the measured
        need exactly would just move the cliff."""
        _polling, attempts = _run_loop(tmp_path, found_on_attempt=None)
        assert attempts >= MEASURED_SECONDS_NEEDED * 2

    def test_a_slow_but_healthy_start_is_not_reported_as_failure(self, tmp_path):
        """Twice as slow as the worst observed start still succeeds."""
        polling, _attempts = _run_loop(tmp_path, found_on_attempt=45)
        assert polling == "yes"

    def test_the_budget_is_bounded(self):
        """A deploy must still terminate. An unbounded wait on a genuinely dead
        bot would hang whoever ran it.

        Read from the assignment rather than measured by running the loop: a
        mutation setting this to 100000 is exactly the case being guarded
        against, and the version of this test that ran it took 15 minutes to
        say so.
        """
        assert _poll_wait_seconds() <= MAX_RUNNABLE_BUDGET


class TestItStillFailsWhenItShould:
    def test_a_bot_that_never_polls_is_reported(self, tmp_path):
        """The check must keep its teeth. During the 2026-09-04 outage
        `systemctl is-active` said "active" for 87 minutes while the bot
        reached nobody; this loop is what distinguishes the two."""
        polling, _attempts = _run_loop(tmp_path, found_on_attempt=None)
        assert polling == "no"

    def test_a_failure_exits_nonzero(self):
        """Read from the file, since the exit lives outside the loop."""
        text = _script_text()
        block = text[text.index('if [ -z "$polling" ]'):]
        block = block[:block.index("\nfi")]
        assert "exit 1" in block

    def test_the_failure_message_still_names_the_likely_causes(self):
        text = _script_text()
        block = text[text.index('if [ -z "$polling" ]'):]
        block = block[:block.index("\nfi")]
        for hint in ("api.telegram.org", "log.level", "NOT been rolled back"):
            assert hint in block, hint

    def test_the_failure_message_says_how_long_it_waited(self):
        """"never reported" invites the reader to think it waited forever."""
        text = _script_text()
        block = text[text.index('if [ -z "$polling" ]'):]
        block = block[:block.index("\nfi")]
        assert "POLL_WAIT_SECONDS" in block


# ---------------------------------------------------------------------------
# The file as a whole
# ---------------------------------------------------------------------------


class TestScriptIntegrity:
    def test_the_script_parses(self):
        proc = subprocess.run(["bash", "-n", str(SCRIPT)],
                              capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr

    def test_the_body_is_still_wrapped_in_main(self):
        """deploy/update.sh checks out new code OVER ITSELF, and bash reads a
        script by byte offset. Unwrapping it makes a mid-deploy release execute
        nonsense. See tests/test_deploy_script_self_rewrite.py.

        The last line must be the CALL and not a comment containing it:
        endswith() alone is satisfied by `# main "$@"`, which deploys nothing.
        """
        last = _script_text().rstrip().rsplit("\n", 1)[-1].strip()
        assert last == 'main "$@"', f"last line is {last!r}"

    @pytest.mark.parametrize("value", ["20", "60"])
    def test_the_budget_is_not_asserted_by_grepping_the_file(self, value):
        """A meta-test, because this exact trap has already caught two tests on
        this script: the comment above the loop names both the old budget and
        the new one, so ANY test that searches the file for a number matches
        the prose whatever the code says."""
        assert value in _script_text(), (
            "the explanatory comment no longer mentions this number — which "
            "means a file-grepping test might now look valid. It still is not; "
            "run the loop instead."
        )
