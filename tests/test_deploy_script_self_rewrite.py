"""deploy/update.sh must survive being overwritten while it runs.

The script checks out new code over itself partway through. Bash reads a script
incrementally, by byte offset, so when the file it is executing changes size,
the shell can resume at the wrong place and run whatever bytes now sit there —
mid-deploy, on the production box.

The defence is that the whole body lives in main(), called on the last line:
bash must parse the entire function before executing any of it, so the version
that started is the version that finishes.

The first two tests demonstrate the hazard and the fix on throwaway scripts,
because a claim about bash's behaviour is worth proving rather than asserting.
The rest pin the real script's shape.
"""

from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
UPDATE_SH = REPO / "deploy" / "update.sh"

#: Rewrites itself, then prints. Mirrors what `git checkout` does to update.sh.
UNWRAPPED = """\
#!/usr/bin/env bash
set -euo pipefail
cat > "$0" <<'NEW'
#!/usr/bin/env bash
echo REPLACED
NEW
echo "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
echo FINISHED_OK
"""

WRAPPED = """\
#!/usr/bin/env bash
set -euo pipefail
main() {
cat > "$0" <<'NEW'
#!/usr/bin/env bash
echo REPLACED
NEW
echo "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
echo FINISHED_OK
}
main "$@"
"""


def _run(tmp_path, body):
    script = tmp_path / "s.sh"
    script.write_text(textwrap.dedent(body))
    script.chmod(0o755)
    p = subprocess.run(["bash", str(script)], capture_output=True, text=True, timeout=30)
    return p


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
class TestTheHazardIsReal:
    def test_an_unwrapped_script_silently_skips_the_rest(self, tmp_path):
        """The failure mode is SILENT SUCCESS, which is why it matters.

        Verified identically on macOS and on the production box (GNU bash
        5.2.21): after the rewrite, bash runs none of the remaining commands
        and exits 0. For update.sh that means skipping the dependency install,
        the import check and THE RESTART, while printing nothing — so the
        operator is told the deploy worked and the bot keeps running old code.

        A crash would be kinder. This is why the wrapping is not cosmetic.
        """
        p = _run(tmp_path, UNWRAPPED)
        assert "FINISHED_OK" not in p.stdout, (
            "the commands after the rewrite should not have run; if bash ever "
            "starts handling this safely the wrapping still costs nothing"
        )
        assert p.returncode == 0, (
            "and it exits 0 — the deploy looks successful, which is the danger"
        )

    def test_a_wrapped_script_finishes_intact(self, tmp_path):
        """The fix: bash parses main() fully before running any of it."""
        p = _run(tmp_path, WRAPPED)
        assert p.returncode == 0, p.stderr
        assert "FINISHED_OK" in p.stdout
        assert "REPLACED" not in p.stdout, "the new file must not be executed"


class TestUpdateScriptIsWrapped:
    @staticmethod
    def _text():
        return UPDATE_SH.read_text()

    def test_the_body_is_inside_a_function(self):
        assert "\nmain() {\n" in self._text(), (
            "update.sh rewrites itself with git checkout; without main() bash "
            "can resume at the wrong byte offset mid-deploy"
        )

    def test_main_is_invoked_with_the_arguments(self):
        """A wrapper that is never called deploys nothing at all.

        The last line must be the CALL, not a comment containing it. endswith()
        alone accepted `# main "$@"` — a mutation that comments the call out
        left this test green while the script did nothing, found 2026-10-27.
        """
        last = self._text().rstrip().rsplit("\n", 1)[-1].strip()
        assert last == 'main "$@"', (
            f"the script's last line is {last!r}, not the call to main"
        )

    def test_the_reason_is_recorded_in_the_file(self):
        """Someone will be tempted to unwrap this; the note is the defence."""
        assert "byte offset" in self._text()

    def test_the_script_parses(self):
        p = subprocess.run(["bash", "-n", str(UPDATE_SH)], capture_output=True, text=True)
        assert p.returncode == 0, p.stderr

    def test_the_tag_argument_still_reaches_git(self):
        """main "$@" must forward the release tag, or it deploys main instead."""
        t = self._text()
        assert 'TARGET="${1:-}"' in t and 'git checkout --quiet "$TARGET"' in t


class TestSetupTightensPermissions:
    """data/ and logs/ hold congregational data; config/ holds the token.

    Found 2026-10-13 during a security review: data/ and logs/ were 0775 and
    admins.yaml 0664 on the server. Nothing was actually exposed — /home/ubuntu
    is 0750 and it is a single-user box — but the modes were inherited rather
    than chosen, and the next box might not be single-user.

    setup.sh sets them on every run, so a file created loosely at some point is
    corrected rather than inherited forever.
    """

    @staticmethod
    def _text():
        return (REPO / "deploy" / "setup.sh").read_text()

    def test_data_and_log_directories_are_not_world_readable(self):
        assert "chmod 750 data logs" in self._text()

    def test_the_token_file_is_owner_only(self):
        t = self._text()
        assert "chmod 600 config/config.yaml" in t, (
            "config.yaml holds the live bot token; group-readable is not enough"
        )

    def test_the_admin_list_is_not_world_readable(self):
        assert "config/admins.yaml" in self._text()

    def test_no_global_umask_is_left_set(self):
        """setup.sh writes the systemd unit with `sudo tee` further down; a
        lingering restrictive umask would create that unit 0600."""
        body = self._text()
        assert "\numask " not in body, (
            "a global umask here would also apply to the systemd unit written "
            "later in this script"
        )

    def test_the_script_still_parses(self):
        import subprocess
        p = subprocess.run(["bash", "-n", str(REPO / "deploy" / "setup.sh")],
                           capture_output=True, text=True)
        assert p.returncode == 0, p.stderr


class TestUpdateKeepsDependenciesCurrent:
    """A deploy must leave the server with what CI tested.

    `pip install -r requirements.txt` never upgrades a package that already
    satisfies the spec, so a TRANSITIVE dependency freezes at whatever was
    current when it was first installed. On 2026-10-20 that had left the server
    on certifi from July — the CA bundle httpx verifies Telegram with — while
    CI, installing fresh every run, had been testing against October's. Eight
    packages had drifted, including the timezone rules this bot schedules
    services by.

    The subtlety that made it worse: plain `--upgrade` does NOT fix it. pip's
    default --upgrade-strategy is only-if-needed, so it moves the packages
    NAMED in the file and leaves their dependencies alone. Tried on the server:
    it upgraded pytz and nothing else.
    """

    @staticmethod
    def _text():
        return (REPO / "deploy" / "update.sh").read_text()

    @staticmethod
    def _install_command() -> str:
        """The pip install line that provisions the venv, comments excluded.

        Searching the whole file would match the comment ABOVE this line, which
        quotes the very flags being asserted on — two mutations passed that way
        before this was noticed.
        """
        lines = [ln.strip() for ln in (REPO / "deploy" / "update.sh").read_text().splitlines()
                 if not ln.lstrip().startswith("#")]
        installs = [ln for ln in lines if "pip" in ln and "install" in ln
                    and "requirements.txt" in ln]
        assert installs, "update.sh no longer installs requirements at all"
        return installs[0]          # the provisioning one; later is the rollback

    def test_the_install_upgrades(self):
        assert "--upgrade" in self._install_command(), (
            "without this the server's transitive dependencies never move again"
        )

    def test_it_upgrades_eagerly(self):
        """only-if-needed is the default and does not reach transitive deps."""
        assert "--upgrade-strategy eager" in self._install_command(), (
            "plain --upgrade moves only the packages named in requirements.txt; "
            "certifi, pillow and tzdata are not named there"
        )

    def test_the_upgrade_happens_before_the_import_check(self):
        """The import check is what catches a bad upgrade, so it must run
        after one — and both before the service is restarted."""
        t = self._text()
        assert t.index("--upgrade-strategy eager") < t.index("import bot")
        assert t.index("import bot") < t.index("Restarting")

    def test_the_rollback_limit_is_written_down(self):
        """pip is never told the previous versions, so a rollback cannot
        downgrade. Someone will eventually need to know that."""
        t = self._text()
        tail = t[t.index("New code fails to import"):]
        assert "DOWNGRADE" in tail or "downgrade" in tail
