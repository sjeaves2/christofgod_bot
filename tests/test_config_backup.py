"""The config snapshot must actually hold the three files nothing else covers.

config/config.yaml, admins.yaml and officials.yaml are the files the bot's own
nightly backup deliberately cannot send (live token; real people), and they are
gitignored, so git will replace or delete them without warning. On 2026-11-10 a
hand-run `git checkout main` on the server did exactly that and the only copy
was six days old — losing an ops admin's `phone:` and an official's proxy
`chat_id`. deploy/backup-config.sh exists because of that day.

These tests RUN the script, following the lead of
tests/test_deploy_polling_wait.py and for the same reason stated there: two
earlier tests on a deploy script passed while the code was broken, because the
explanatory comment quoted the very value being asserted and the grep matched
the prose. This file is full of that hazard — backup-config.sh's header
discusses "14 days" and the timer's comment names 02:55 and 03:00-03:45 — so
nothing here greps the file for a number it cares about. The schedule is read
with a real INI parser, which sees `OnCalendar=` and not the comments above it.

What is deliberately NOT tested: that the timer is installed and enabled on the
live server. That is state, not code, and asserting it here would be a test
that passes on a laptop and tells you nothing about the box.
"""

from __future__ import annotations

import configparser
import os
import shutil
import stat
import subprocess
import time
from pathlib import Path

import pytest

DEPLOY = Path(__file__).parent.parent / "deploy"
SCRIPT = DEPLOY / "backup-config.sh"
TIMER = DEPLOY / "christofgod-config-backup.timer"
SERVICE_TEMPLATE = DEPLOY / "christofgod-config-backup.service.template"
SETUP = DEPLOY / "setup.sh"

#: The windows the snapshot must not land in. 02:55 is the unattended-upgrades
#: automatic reboot; 03:00-03:45 is the nightly data backup plus its three
#: 15-minute retries. Both are recorded in the timer, but these values are
#: stated here independently so the test is not reading its own expectation
#: out of the file under test.
FORBIDDEN_MINUTES = {2 * 60 + 55} | set(range(3 * 60, 3 * 60 + 46))


def _fake_repo(tmp_path: Path, *, config_files: dict[str, str]) -> Path:
    """A throwaway repo layout: deploy/backup-config.sh beside a config/.

    The script derives SRC from its own location, so testing the
    empty-config case needs a real directory tree rather than an env var.
    """
    repo = tmp_path / "repo"
    (repo / "deploy").mkdir(parents=True)
    (repo / "config").mkdir()
    shutil.copy2(SCRIPT, repo / "deploy" / SCRIPT.name)
    for name, text in config_files.items():
        (repo / "config" / name).write_text(text, encoding="utf-8")
    return repo


def _run(repo: Path, dest: Path, keep_days: int | None = None):
    env = dict(os.environ, CONFIG_BACKUP_DIR=str(dest), HOME=str(dest.parent))
    if keep_days is not None:
        env["CONFIG_BACKUP_KEEP_DAYS"] = str(keep_days)
    return subprocess.run(
        ["bash", str(repo / "deploy" / SCRIPT.name)],
        capture_output=True, text=True, env=env,
    )


class TestItSnapshotsTheRightFiles:
    def test_it_copies_the_real_configs_and_skips_the_templates(self, tmp_path):
        """Pins the OUTCOME, which is the only honest thing to pin here.

        Two independent mechanisms exclude templates — the `*.yaml` glob
        cannot match `admins.yaml.example`, and the `*.example) continue`
        case skips it if a wider glob ever does. Each is redundant with the
        other, so neither mutation fails this test alone; removing BOTH does
        (measured 2026-11-10). Asserting on either mechanism separately would
        be asserting on an implementation detail.
        """
        repo = _fake_repo(tmp_path, config_files={
            "config.yaml": "bot:\n  token: SECRET\n",
            "admins.yaml": "admins: []\n",
            "officials.yaml": "officials: []\n",
            "admins.yaml.example": "admins: []  # template\n",
            "notes.txt": "not yaml\n",
        })
        dest = tmp_path / "backups"
        result = _run(repo, dest)
        assert result.returncode == 0, result.stderr

        snapshots = sorted(p for p in dest.iterdir() if p.is_dir())
        assert len(snapshots) == 1, f"expected one dated directory, got {snapshots}"
        got = {p.name for p in snapshots[0].iterdir()}
        assert got == {"config.yaml", "admins.yaml", "officials.yaml",
                       "MANIFEST.sha256"}, (
            "the snapshot must hold exactly the three real configs plus the "
            f"manifest — a .example or a stray file crept in, or one of the "
            f"three is missing: {sorted(got)}"
        )

    def test_the_contents_are_copied_not_just_the_names(self, tmp_path):
        """A snapshot of empty files would pass the test above."""
        repo = _fake_repo(tmp_path, config_files={
            "config.yaml": "bot:\n  token: THE-REAL-VALUE\n",
            "admins.yaml": "admins: []\n",
        })
        dest = tmp_path / "backups"
        assert _run(repo, dest).returncode == 0
        snap = next(p for p in dest.iterdir() if p.is_dir())
        assert "THE-REAL-VALUE" in (snap / "config.yaml").read_text()

    def test_the_snapshot_is_not_readable_by_others(self, tmp_path):
        """These files carry the bot token and members' phone numbers."""
        repo = _fake_repo(tmp_path, config_files={"config.yaml": "bot: {}\n"})
        dest = tmp_path / "backups"
        assert _run(repo, dest).returncode == 0
        snap = next(p for p in dest.iterdir() if p.is_dir())

        dir_mode = stat.S_IMODE(snap.stat().st_mode)
        assert dir_mode & (stat.S_IRWXG | stat.S_IRWXO) == 0, (
            f"snapshot directory is {oct(dir_mode)}; group/other must have no "
            f"access to the bot token"
        )
        for f in snap.iterdir():
            mode = stat.S_IMODE(f.stat().st_mode)
            assert mode & (stat.S_IRWXG | stat.S_IRWXO) == 0, (
                f"{f.name} is {oct(mode)}; must be owner-only"
            )

    def test_the_manifest_lists_every_copied_file(self, tmp_path):
        repo = _fake_repo(tmp_path, config_files={
            "config.yaml": "bot: {}\n", "admins.yaml": "admins: []\n",
        })
        dest = tmp_path / "backups"
        assert _run(repo, dest).returncode == 0
        snap = next(p for p in dest.iterdir() if p.is_dir())
        manifest = (snap / "MANIFEST.sha256").read_text()
        for name in ("config.yaml", "admins.yaml"):
            assert name in manifest, f"{name} missing from the manifest"
        assert len(manifest.strip().splitlines()) == 2


class TestItFailsLoudlyRatherThanQuietly:
    def test_an_empty_config_dir_is_an_error_not_an_empty_backup(self, tmp_path):
        """The failure mode this guard exists for.

        An empty dated directory looks exactly like a good backup until the
        day someone needs it, and it stops them looking for the real problem.
        """
        repo = _fake_repo(tmp_path, config_files={})
        dest = tmp_path / "backups"
        result = _run(repo, dest)

        assert result.returncode != 0, (
            "backing up nothing reported success: " + result.stdout
        )
        assert "no config files found" in result.stderr
        leftovers = [p for p in dest.iterdir() if p.is_dir()] if dest.exists() else []
        assert not leftovers, (
            f"an empty snapshot directory was left behind: {leftovers} — it "
            f"will be mistaken for a backup"
        )

    def test_only_templates_present_also_fails(self, tmp_path):
        """`.example` files are skipped, so a config/ holding only templates
        is the same as an empty one — a freshly cloned, unconfigured repo."""
        repo = _fake_repo(tmp_path, config_files={
            "config.yaml.example": "bot: {}\n",
            "admins.yaml.example": "admins: []\n",
        })
        result = _run(repo, tmp_path / "backups")
        assert result.returncode != 0, (
            "a config/ with only templates was reported as backed up"
        )


class TestRetention:
    def test_it_keeps_fourteen_days_and_drops_older(self, tmp_path):
        """Checked at the boundary, because off-by-one here silently shortens
        the window — and `-mtime +N` is strictly greater, so exactly N days
        old is KEPT."""
        repo = _fake_repo(tmp_path, config_files={"config.yaml": "bot: {}\n"})
        dest = tmp_path / "backups"
        dest.mkdir()

        ages = {"2026-10-21": 20, "2026-10-26": 15,
                "2026-10-27": 14, "2026-10-28": 13}
        for name, days in ages.items():
            d = dest / name
            d.mkdir()
            (d / "admins.yaml").write_text("x\n", encoding="utf-8")
            old = time.time() - days * 86400
            os.utime(d, (old, old))

        assert _run(repo, dest, keep_days=14).returncode == 0
        kept = {p.name for p in dest.iterdir() if p.is_dir()}

        assert "2026-10-21" not in kept, "a 20-day-old snapshot survived"
        assert "2026-10-26" not in kept, "a 15-day-old snapshot survived"
        assert "2026-10-27" in kept, "a 14-day-old snapshot was deleted"
        assert "2026-10-28" in kept, "a 13-day-old snapshot was deleted"

    def test_it_does_not_delete_unrelated_directories(self, tmp_path):
        """The one-off ~/config-backup-v0.28.2 from 2026-11-04 is the copy
        that saved most of the data on the day of the incident. A retention
        sweep that eats neighbouring directories would have destroyed it."""
        repo = _fake_repo(tmp_path, config_files={"config.yaml": "bot: {}\n"})
        dest = tmp_path / "backups"
        dest.mkdir()
        keepsake = dest / "config-backup-v0.28.2"
        keepsake.mkdir()
        (keepsake / "admins.yaml").write_text("precious\n", encoding="utf-8")
        old = time.time() - 365 * 86400
        os.utime(keepsake, (old, old))

        assert _run(repo, dest, keep_days=14).returncode == 0
        assert keepsake.is_dir(), (
            "the retention sweep deleted a directory it did not create"
        )


class TestTheSchedule:
    """Read with an INI parser, never grepped.

    The timer's own comment names 02:55 and 03:00-03:45 in prose, so a
    substring search for a time would match the explanation rather than the
    setting — the trap documented in test_deploy_polling_wait.py.
    """

    @staticmethod
    def _timer() -> configparser.ConfigParser:
        cp = configparser.ConfigParser(strict=False)
        cp.read_string(TIMER.read_text(encoding="utf-8"))
        return cp

    def test_it_runs_clear_of_the_reboot_and_the_data_backup(self):
        on_calendar = self._timer()["Timer"]["OnCalendar"]
        hh, mm = on_calendar.split()[-1].split(":")[:2]
        minute_of_day = int(hh) * 60 + int(mm)

        assert minute_of_day not in FORBIDDEN_MINUTES, (
            f"the config backup is scheduled at {hh}:{mm}, inside the 02:55 "
            f"auto-reboot or the 03:00-03:45 data-backup retry window. A "
            f"reboot mid-snapshot, or contention with the backup the "
            f"congregation depends on, is the cost."
        )

    def test_a_missed_run_catches_up(self):
        """Without Persistent, the 02:55 auto-reboot landing badly leaves a
        silent hole in the retention window."""
        assert self._timer()["Timer"].getboolean("Persistent") is True

    def test_the_forbidden_window_is_not_vacuous(self):
        """Guard on the guard: an empty FORBIDDEN_MINUTES would make the
        schedule test pass for any time at all."""
        assert 3 * 60 + 15 in FORBIDDEN_MINUTES
        assert 2 * 60 + 55 in FORBIDDEN_MINUTES
        assert 4 * 60 + 15 not in FORBIDDEN_MINUTES


class TestItIsInstalledOnARebuiltBox:
    """setup.sh must install the timer, or a rebuilt server comes up with no
    config backup and nothing to say so — which is the state that made
    2026-11-10 unrecoverable past the 4th.
    """

    @staticmethod
    def _setup_code() -> str:
        """setup.sh with comment lines stripped.

        The block that installs this timer is introduced by a comment
        explaining it, and that comment names the unit. Matching on it would
        pass with the install itself deleted.
        """
        return "\n".join(
            line for line in SETUP.read_text(encoding="utf-8").splitlines()
            if not line.lstrip().startswith("#")
        )

    def test_setup_renders_the_service_and_installs_the_timer(self):
        code = self._setup_code()
        assert "${BACKUP_NAME}.service.template" in code, (
            "setup.sh no longer renders the config-backup service template"
        )
        assert "${BACKUP_NAME}.timer" in code, (
            "setup.sh no longer installs the config-backup timer"
        )

    def test_setup_enables_the_timer(self):
        code = self._setup_code()
        assert 'systemctl enable --now "${BACKUP_NAME}.timer"' in code, (
            "the timer is installed but never enabled, so it never fires"
        )

    def test_the_template_is_substitutable_like_the_bot_service(self):
        """Same placeholders as christofgod-bot.service.template, so setup.sh's
        existing sed works and the unit follows a non-default clone path."""
        text = SERVICE_TEMPLATE.read_text(encoding="utf-8")
        for placeholder in ("__REPO_DIR__", "__RUN_USER__"):
            assert placeholder in text, f"{placeholder} missing from the template"
        assert "/home/ubuntu" not in text, (
            "the template hardcodes /home/ubuntu instead of __REPO_DIR__"
        )

    def test_the_script_does_not_hardcode_a_home_directory(self):
        assert "/home/ubuntu" not in SCRIPT.read_text(encoding="utf-8")


class TestTheScriptIsRunnable:
    def test_it_is_executable(self):
        assert os.access(SCRIPT, os.X_OK), (
            "deploy/backup-config.sh is not executable; systemd ExecStart "
            "will fail with 203/EXEC"
        )

    def test_it_parses(self):
        assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0

    @pytest.mark.parametrize("script", [SCRIPT, SETUP])
    def test_the_shell_is_strict(self, script):
        """Without -e, a failed cp is a partial backup reported as success."""
        assert "set -euo pipefail" in script.read_text(encoding="utf-8")
