"""Report packages installed in this venv that requirements.txt no longer needs.

`pip install -r requirements.txt` installs; it never removes. So a dependency
dropped from requirements.txt stays in the venv indefinitely, and keeps its
advisories with it — on 2026-10-13, removing deep-translator left requests,
urllib3 and beautifulsoup4 behind, carrying the four CVEs the removal was meant
to clear.

This computes the dependency closure of requirements.txt from the metadata of
what is actually installed, and prints anything outside it, one name per line.
It REMOVES NOTHING: update.sh decides, so the uninstall and the verification
that follows live in one place.

Conservative by construction. A dependency whose marker it cannot evaluate is
KEPT, because wrongly keeping a package wastes a few megabytes while wrongly
removing one breaks the bot.
"""

from __future__ import annotations

import importlib.metadata as md
import re
import sys
from pathlib import Path

#: Packaging machinery that is never a project dependency but must survive.
ALWAYS_KEEP = {"pip", "setuptools", "wheel", "pkg-resources", "distribute"}

_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[([^\]]*)\])?")
_EXTRA_MARKER = re.compile(r"""extra\s*==\s*['"]([^'"]+)['"]""")


def _norm(name: str) -> str:
    """PyPI treats '-', '_' and case as equivalent; metadata is inconsistent."""
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def _parse(line: str) -> tuple[str, set[str]] | None:
    """('python-telegram-bot[job-queue]>=21', ...) -> ('python-telegram-bot', {'job-queue'})"""
    line = line.split("#", 1)[0].strip()
    if not line or line.startswith("-"):
        return None
    m = _NAME.match(line)
    if not m:
        return None
    extras = {_norm(e) for e in (m.group(2) or "").split(",") if e.strip()}
    return _norm(m.group(1)), extras


def closure(requirements: Path) -> set[str]:
    """Every package requirements.txt needs, directly or transitively."""
    wanted: dict[str, set[str]] = {}
    for line in requirements.read_text().splitlines():
        parsed = _parse(line)
        if parsed:
            name, extras = parsed
            wanted.setdefault(name, set()).update(extras)

    keep: set[str] = set()
    queue = [(n, e) for n, e in wanted.items()]
    while queue:
        name, extras = queue.pop()
        if name in keep and not extras:
            continue
        keep.add(name)
        try:
            requires = md.requires(name) or []
        except md.PackageNotFoundError:
            continue        # not installed; nothing to walk
        for req in requires:
            dep = _parse(req)
            if not dep:
                continue
            marker = req.split(";", 1)[1] if ";" in req else ""
            needed_extras = {_norm(x) for x in _EXTRA_MARKER.findall(marker)}
            # An extra-gated dependency counts only if we asked for that extra.
            if needed_extras and not (needed_extras & extras):
                continue
            if dep[0] not in keep:
                queue.append((dep[0], dep[1]))
    return keep


def orphans(requirements: Path) -> list[str]:
    keep = closure(requirements) | {_norm(n) for n in ALWAYS_KEEP}
    out = []
    for dist in md.distributions():
        name = dist.metadata["Name"]
        if name and _norm(name) not in keep:
            out.append(name)
    return sorted(set(out))


if __name__ == "__main__":
    req = Path(sys.argv[1] if len(sys.argv) > 1 else "requirements.txt")
    for name in orphans(req):
        print(name)
