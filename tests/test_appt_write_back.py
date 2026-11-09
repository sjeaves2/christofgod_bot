"""There is one place that writes an appointment back into the list.

The replace-loop

    for i, a in enumerate(appts):
        if a["id"] == appt_id:
            appts[i] = appt

was written out eight times in handlers/appointments.py. Every inline copy was
a no-op: `appt` always arrived from
`next((a for a in appts if a["id"] == appt_id), None)`, so it IS the list
element and the assignment changed nothing. Deleting all six inline copies
left 2303/2303 passing.

They were CONSOLIDATED into _replace_appt rather than deleted, and these tests
exist because of the reason why. **The suite cannot tell this loop live from
dead.** Through the router it is a no-op whether it runs or not, so:

  * no behavioural test can fail if it is removed, and
  * no behavioural test can fail if it is removed WRONGLY — the day some
    caller holds a copy instead of the live object, deleting the write-back
    breaks persistence with nothing going red.

So the thing worth pinning is not behaviour, it is the decision: one tested
place answers the aliasing question, instead of six silent assumptions. These
tests are deliberately structural, which is the honest shape for a guard
against re-duplication.

_replace_appt's own behaviour — the copy case these call sites do not exercise
— is pinned directly in test_appt_callback_dispatch.py
(test_replace_appt_writes_a_copy_back).
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

SOURCE = Path(__file__).parent.parent / "handlers" / "appointments.py"

#: The functions whose inline copies were folded into _replace_appt.
CONSOLIDATED = {
    "_finalize_appointment",
    "handle_counter_propose_message",
    "rs_newtime",
    "_handle_reschedule_response",
}

#: ca_confirm keeps a by-index loop of its own and is NOT a missed site. It
#: sets `appts[i]["status"]` and stamps the action rather than replacing the
#: record, and breaks on the first match. Proven below rather than asserted,
#: so that "consolidate the last one too" fails here instead of quietly
#: changing cancellation.
KEEPS_ITS_OWN_LOOP = {"ca_confirm"}


def _tree() -> ast.Module:
    return ast.parse(SOURCE.read_text(encoding="utf-8"))


def _functions() -> dict[str, ast.AST]:
    found: dict[str, ast.AST] = {}
    for node in ast.walk(_tree()):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            found[node.name] = node
    return found


def _segment(name: str) -> str:
    node = _functions()[name]
    return ast.get_source_segment(SOURCE.read_text(encoding="utf-8"), node) or ""


class TestTheHelperIsTheOnlyWriteBack:
    def test_the_parse_finds_the_functions(self):
        """Vacuity guard: a rename would make every test below trivially
        pass by iterating over nothing."""
        names = _functions().keys()
        missing = (CONSOLIDATED | KEEPS_ITS_OWN_LOOP | {"_replace_appt"}) - names
        assert not missing, f"not found in the module any more: {sorted(missing)}"

    def test_no_function_assigns_into_appts_by_index(self):
        """`appts[i] = appt` must appear only inside _replace_appt.

        Catches a seventh copy being pasted back in, which is how the six
        got there: each was locally reasonable and nobody saw the total.
        """
        pattern = re.compile(r"appts\[\s*\w+\s*\]\s*=\s*appt\b")
        offenders = []
        for name, node in _functions().items():
            if name == "_replace_appt":
                continue
            body = ast.get_source_segment(
                SOURCE.read_text(encoding="utf-8"), node) or ""
            if pattern.search(body):
                offenders.append(name)
        assert not offenders, (
            f"these write an appointment back by index instead of calling "
            f"_replace_appt: {sorted(offenders)}. The loop is a no-op at every "
            f"current call site, so no behavioural test will catch the "
            f"duplication — that is exactly why it is checked here."
        )

    def test_each_consolidated_function_uses_the_helper(self):
        """The move must not have dropped a write-back entirely.

        Deleting one would also pass the whole suite, since every site is a
        no-op today — so the call being present is the only evidence.
        """
        for name in sorted(CONSOLIDATED):
            body = _segment(name)
            assert "_replace_appt(appts, appt)" in body, (
                f"{name} no longer writes the appointment back. Today that "
                f"changes nothing, because `appt` is the list element — but "
                f"it drops the contract the save path assumes, and no test "
                f"will fail the day a caller holds a copy."
            )

    def test_the_helper_is_defined_before_it_is_used(self):
        src = SOURCE.read_text(encoding="utf-8")
        definition = src.index("def _replace_appt(")
        first_call = src.index("_replace_appt(appts, appt)")
        assert definition < first_call


class TestCaConfirmIsDeliberatelyDifferent:
    """The one by-index loop that must NOT become _replace_appt."""

    def test_it_does_not_call_the_helper(self):
        assert "_replace_appt" not in _segment("ca_confirm"), (
            "ca_confirm was consolidated into _replace_appt, but it sets "
            "fields on the existing record rather than replacing it — "
            "see its loop and _replace_appt's docstring"
        )

    def test_it_sets_the_status_in_place(self):
        body = _segment("ca_confirm")
        assert 'appts[i]["status"] = "cancelled"' in body, (
            "ca_confirm no longer cancels by setting the status on the record "
            "in place; if that was intentional, update KEEPS_ITS_OWN_LOOP and "
            "this test together"
        )

    def test_it_stops_at_the_first_match(self):
        """It breaks; _replace_appt deliberately does not. Keeping the
        difference visible stops the two being merged by eye.

        Asked of the AST rather than the source text: _replace_appt's
        docstring now explains that ca_confirm "breaks on the first match",
        so a substring search for "break" matches the prose and passes for
        the wrong reason. It did, on the first run of this test.
        """
        def _breaks(name: str) -> bool:
            return any(isinstance(n, ast.Break)
                       for n in ast.walk(_functions()[name]))

        assert _breaks("ca_confirm"), (
            "ca_confirm no longer stops at the first match"
        )
        assert not _breaks("_replace_appt"), (
            "_replace_appt grew a break; it deliberately scans the whole "
            "list, and the difference from ca_confirm is the point"
        )
