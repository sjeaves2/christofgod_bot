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
        """A wrapper that is never called deploys nothing at all."""
        assert self._text().rstrip().endswith('main "$@"')

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
