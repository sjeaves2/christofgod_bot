"""handlers/shared.py must stay a leaf.

It exists so every feature module can assemble its own ConversationHandlers.
All 15 of them need the shared /cancel fallback, and while that fallback lived
in handlers/user_basics.py it was out of reach for handlers/announcements.py:
user_basics imports cmd_announcements from announcements, so an announcements
factory reaching back for the fallback would close the cycle

    announcements -> user_basics -> announcements

The fix only works as long as shared.py imports no feature code. One
`from handlers.something import ...` in there and the cycle it was created to
break comes back, in a form that may not fail until some unrelated import
order changes — the kind of breakage that shows up on the server and not in
CI. So it is asserted rather than left as a comment.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

REPO = Path(__file__).resolve().parents[1]
SHARED = REPO / "handlers" / "shared.py"


def _imported_modules() -> list[str]:
    """Every module handlers/shared.py imports, at any nesting depth.

    Walks the whole tree rather than the top level: a deferred import inside a
    function body would break the cycle at import time and reinstate it at
    call time, which is worse than an honest top-level one.
    """
    tree = ast.parse(SHARED.read_text(encoding="utf-8"))
    mods: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            mods.append(node.module)
        elif isinstance(node, ast.Import):
            mods.extend(a.name for a in node.names)
    return mods


class TestTheSweepSeesTheImports:
    def test_the_file_exists(self):
        assert SHARED.exists(), f"{SHARED} is missing"

    def test_imports_were_found(self):
        """Guards the test below: an empty list passes it vacuously, and would
        pass just as happily if the parse silently failed."""
        assert len(_imported_modules()) >= 4, _imported_modules()


class TestItImportsNoFeatureCode:
    def test_no_handlers_module_is_imported(self):
        offenders = [m for m in _imported_modules()
                     if m == "handlers" or m.startswith("handlers.")]
        assert not offenders, (
            f"handlers/shared.py imports {offenders}. It must stay a leaf: the "
            "feature modules import IT, so importing one of them back "
            "reinstates the announcements -> user_basics -> announcements "
            "cycle this module exists to break."
        )

    def test_it_imports_cleanly_on_its_own(self):
        """The cycle's real symptom is an ImportError under some import order.
        Importing this module first, before anything pulls in a feature
        module, is the order most likely to expose one."""
        import handlers.shared  # noqa: F401


class TestTheFallbackIsHere:
    def test_cancel_conversation_is_exported(self):
        from handlers.shared import cancel_conversation
        assert callable(cancel_conversation)

    def test_nothing_else_defines_a_second_one(self):
        """Two definitions would be worse than one in the wrong place: a
        conversation's fallback would depend on which module it imported from,
        and both would look right.

        Checked against the SOURCE, not with hasattr. Every module that builds
        conversations imports this function, so hasattr is true for all of
        them — which is correct and not what is being guarded against.
        """
        import ast
        offenders = []
        for path in sorted((REPO / "handlers").glob("*.py")):
            if path == SHARED:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and node.name == "cancel_conversation"):
                    offenders.append(f"{path.name}:{node.lineno}")
        assert not offenders, (
            f"a second cancel_conversation is defined at {offenders}. There "
            "must be exactly one, in handlers/shared.py."
        )

    def test_the_importers_all_get_the_same_object(self):
        """The flip side: importing it is fine, but they must all resolve to
        the one definition."""
        import handlers.shared
        import handlers.user_basics as ub
        assert ub.cancel_conversation is handlers.shared.cancel_conversation
