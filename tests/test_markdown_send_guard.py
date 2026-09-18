"""Every Markdown send must go through a helper that can fall back to plain text.

Four bugs in two days shared one root cause: a value interpolated into a
Markdown message without escaping. Telegram rejects the WHOLE message over one
unpaired marker, so the message is not degraded — it is never delivered, and
usually nobody sees an error:

  * /events, 2026-09-17 — a placeholder join link containing "REPLACE_ME";
  * event reminders — the same link, where a rejected message means the
    congregation is simply not told about a service;
  * /setservicelink — broken BY the placeholders it exists to repair;
  * an appointment's purpose — the one such path a MEMBER could trigger.

Escaping with md() is the fix. This guard is about the fix being forgotten,
which is what actually happened, repeatedly. Routing sends through a helper
means a missed escape costs the formatting instead of the message.

The guard is the durable part. Converting the call sites once is a cleanup that
decays as new code is written; a failing build does not.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

REPO = Path(__file__).resolve().parents[1]

#: Modules that send messages to people. error_reporting sends tracebacks as
#: plain text on purpose (a traceback is not valid Markdown), so it has no
#: Markdown sends to guard.
MODULES = sorted((REPO / "handlers").glob("*.py")) + [REPO / "bot.py"]

#: The send methods that accept parse_mode and therefore can be rejected.
RAW_SENDS = {"reply_text", "send_message", "edit_message_text"}

#: Call sites that stay raw, each for a stated reason. Anything not listed here
#: must use reply_markdown / send_markdown / edit_markdown from common.
#:
#: Keep this SHORT. Every entry is a place a message can still be lost.
ALLOWED_RAW: dict[str, str] = {
    "announcements.py:cmd_announcements":
        "has a BETTER fallback than plain text: it re-renders the list with "
        "everything escaped, keeping the structure. A generic retry would "
        "swallow the BadRequest and lose that.",
}

#: Entry-time VALIDATION lives in common.validate_markdown, which is not in
#: MODULES, so those sites need no exemption: /addannouncement and the
#: /deleteevent notice both call it. That is deliberate — a validator and a
#: fallback are opposites, and wrapping a validator in a fallback would accept
#: every input. Both were briefly broken that way while this pass was written.


def _markdown_sends(path: Path):
    """Yield (function_name, lineno) for each raw Markdown send in *path*."""
    tree = ast.parse(path.read_text())
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    def enclosing_func(node):
        while node in parents:
            node = parents[node]
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return node.name
        return "<module>"

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not isinstance(fn, ast.Attribute) or fn.attr not in RAW_SENDS:
            continue
        # Only sends that ask for Markdown can be rejected for it.
        for kw in node.keywords:
            if kw.arg == "parse_mode" and "MARKDOWN" in ast.dump(kw.value):
                yield enclosing_func(node), node.lineno


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_no_unguarded_markdown_sends(path: Path):
    offenders = [
        f"{path.name}:{func} (line {line})"
        for func, line in _markdown_sends(path)
        if f"{path.name}:{func}" not in ALLOWED_RAW
    ]
    assert not offenders, (
        "these send Markdown without a plain-text fallback, so one unescaped "
        "value makes the message vanish instead of losing its formatting:\n  "
        + "\n  ".join(offenders)
        + "\n\nUse reply_markdown / send_markdown / edit_markdown from common, "
          "or add the site to ALLOWED_RAW with a reason."
    )


class TestTheDetectorWorks:
    """Guard the guard.

    Once the codebase is clean this sweep finds nothing, and a detector that
    silently stopped detecting would look exactly the same. So prove it still
    recognises a raw send, and still ignores a guarded one.
    """

    @staticmethod
    def _find(src: str, tmp_path):
        f = tmp_path / "sample.py"
        f.write_text(src)
        return list(_markdown_sends(f))

    def test_it_catches_a_raw_markdown_send(self, tmp_path):
        found = self._find(
            "async def f(u):\n"
            "    await u.message.reply_text('hi', parse_mode=ParseMode.MARKDOWN)\n",
            tmp_path)
        assert found == [("f", 2)]

    def test_it_ignores_a_guarded_send(self, tmp_path):
        assert self._find("async def f(u):\n    await reply_markdown(u.message, 'hi')\n",
                          tmp_path) == []

    def test_it_ignores_a_plain_text_send(self, tmp_path):
        """Plain sends cannot be rejected for Markdown; they are not the target."""
        assert self._find("async def f(u):\n    await u.message.reply_text('hi')\n",
                          tmp_path) == []

    def test_it_names_the_enclosing_function(self, tmp_path):
        found = self._find(
            "class C:\n"
            "    async def handler(self, q):\n"
            "        await q.edit_message_text('x', parse_mode=ParseMode.MARKDOWN)\n",
            tmp_path)
        assert found and found[0][0] == "handler"

    def test_the_module_list_is_complete(self, tmp_path):
        assert len(MODULES) >= 8, "expected the full handler set"


def test_allowed_raw_entries_still_exist():
    """A stale exemption silently widens the guard."""
    stale = []
    for entry in ALLOWED_RAW:
        fname, func = entry.split(":")
        path = REPO / fname if (REPO / fname).exists() else REPO / "handlers" / fname
        if not path.exists():
            stale.append(f"{entry} (no such file)")
            continue
        if func not in {f for f, _ in _markdown_sends(path)}:
            stale.append(f"{entry} (no raw Markdown send there any more)")
    assert not stale, "remove these from ALLOWED_RAW:\n  " + "\n  ".join(stale)
