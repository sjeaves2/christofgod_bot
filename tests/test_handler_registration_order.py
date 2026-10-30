"""A snapshot of every handler main() registers, in registration order.

WHY THIS EXISTS

main() is 320 lines, builds 15 ConversationHandlers and makes 47 add_handler
calls. Splitting it up is the plan; this test is the safety net that makes the
split readable as a pure move.

Registration order in bot.py is load-bearing and invisible. PTB runs only the
FIRST matching handler per group, and group 0 ends with two handlers that match
almost anything — a free-text handler and an unknown-command handler. Anything
registered after them is dead code that still looks registered. /prayer was
silently broken exactly this way once: the member's request was swallowed by
the free-text catch-all and discarded with no error to show for it.

WHAT IT DOES AND DOES NOT CLAIM

It pins the order UNCHANGED. It does not claim the order is CORRECT — that is a
separate question and a much harder one. So this test is worth exactly one
thing: if the snapshot still matches after the code moves, nothing about
dispatch changed. If it stops matching, the move was not pure.

tests/test_board_wiring.py asserts the ordering PROPERTIES that matter for the
board handlers (registered, and before the catch-alls). Those survive a
deliberate reordering, which is right for them and useless for a refactor.
This file is the opposite: deliberately brittle, and expected to be edited in
the same commit as any intentional change to the wiring.

HOW TO UPDATE IT

Intentionally changing the wiring? Run

    python -m tests.test_handler_registration_order

from the repo root. It rewrites tests/handler_registration_order.snapshot in
place; commit that in the SAME commit as the change, so the diff shows the
wiring change and its justification together. Do NOT regenerate it to make a
refactor pass — that defeats the entire point of the file.

The snapshot is a plain text file rather than a list literal in here for two
reasons: the descriptors run to 450 characters, which no sensible line-length
limit accommodates, and a one-descriptor-per-line text file diffs legibly in
git, where a reordering shows as a move instead of a wall of rewritten quotes.
"""

from __future__ import annotations

import sys
import warnings
from pathlib import Path

from telegram.ext import (
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
)

sys.path.insert(0, str(Path(__file__).parent.parent))


def _callback_name(handler) -> str:
    return getattr(getattr(handler, "callback", None), "__name__", "?")


def _pattern(handler) -> str:
    """A CallbackQueryHandler's pattern, as its source regex.

    The patterns are built with re.escape() over the CB_* prefix constants, so
    the string here is what actually gates dispatch. Swapping two routers'
    prefixes is a real bug this catches.
    """
    pat = getattr(handler, "pattern", None)
    if pat is None:
        return "None"
    return getattr(pat, "pattern", str(pat))


def _describe(handler) -> str:
    """One line per handler, enough to detect a swap, a drop or a retype.

    Deliberately NOT repr(): PTB's reprs carry object addresses, so they differ
    every run and would make the snapshot unusable.
    """
    if isinstance(handler, ConversationHandler):
        # State keys appear as the raw ints they are. They are not unique
        # across conversations — AN_CONFIRM and DA_SELECT are both 8 — which is
        # harmless, since a state only ever resolves inside its own
        # conversation. Resolving them back to constant names would need a
        # reverse lookup over bot's globals that those collisions make
        # ambiguous, so the numbers stay.
        entries = ",".join(_describe(e) for e in handler.entry_points)
        states = ";".join(
            f"{key}=[" + ",".join(_describe(h) for h in handlers) + "]"
            for key, handlers in sorted(handler.states.items(),
                                        key=lambda kv: str(kv[0]))
        )
        fallbacks = ",".join(_describe(f) for f in handler.fallbacks)
        return (f"Conversation(entry=[{entries}] states={{{states}}} "
                f"fallbacks=[{fallbacks}] reentry={handler.allow_reentry})")
    if isinstance(handler, CommandHandler):
        cmds = "/" + "|/".join(sorted(handler.commands))
        return f"Command({cmds} -> {_callback_name(handler)})"
    if isinstance(handler, CallbackQueryHandler):
        return f"CallbackQuery({_pattern(handler)} -> {_callback_name(handler)})"
    if isinstance(handler, MessageHandler):
        return f"Message({_callback_name(handler)})"
    if isinstance(handler, ChatMemberHandler):
        return (f"ChatMember(member_types={handler.chat_member_types} -> "
                f"{_callback_name(handler)})")
    return f"{type(handler).__name__}({_callback_name(handler)})"


def _snapshot() -> list[str]:
    """`group:descriptor` for every registration main() makes, in order."""
    from tests.test_board_wiring import _handlers

    return [f"{group}: {_describe(h)}" for group, h in _handlers()]


SNAPSHOT_FILE = Path(__file__).with_name("handler_registration_order.snapshot")


def _expected() -> list[str]:
    """The pinned snapshot. See the module docstring before regenerating it."""
    assert SNAPSHOT_FILE.exists(), (
        f"{SNAPSHOT_FILE.name} is missing. Without it this file cannot pin "
        "anything; restore it from git rather than regenerating it."
    )
    return [line for line in
            SNAPSHOT_FILE.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")]


class TestTheSnapshotIsReal:
    """Guards every assertion below. A snapshot test whose snapshot is empty,
    or whose sweep sees nothing, passes vacuously — and would pass just as
    happily against a main() that registered nothing at all.
    """

    def test_the_pinned_snapshot_is_populated(self):
        assert len(_expected()) >= 40, (
            f"the snapshot holds {len(_expected())} entries. An empty or "
            "truncated one makes this whole file vacuous."
        )

    def test_the_sweep_collects_handlers(self):
        assert len(_snapshot()) >= 40

    def test_the_descriptors_are_not_placeholders(self):
        """`?` is what _callback_name returns when it cannot find a callback.
        A snapshot full of those would compare equal to itself and mean
        nothing."""
        unknown = [line for line in _snapshot() if "-> ?" in line]
        assert not unknown, unknown

    def test_descriptors_are_stable_across_runs(self):
        """If a descriptor carried an object address or a set's iteration
        order, the snapshot could never match and someone would 'fix' it by
        deleting this file."""
        assert _snapshot() == _snapshot()

    def test_a_reorder_is_actually_visible(self):
        """The failure mode this file exists to catch, forced by hand: if
        swapping two registrations compared equal, the snapshot would be
        pinning nothing."""
        live = _snapshot()
        swapped = list(live)
        swapped[1], swapped[2] = swapped[2], swapped[1]
        assert swapped != live

    def test_a_dropped_handler_is_actually_visible(self):
        live = _snapshot()
        assert live[:-1] != live


class TestRegistrationOrderIsUnchanged:
    def test_the_count_is_unchanged(self):
        """Checked first and reported on its own: a length mismatch makes the
        per-line diff below a wall of shifted text that hides the real cause.
        """
        live = _snapshot()
        assert len(live) == len(_expected()), (
            f"main() now registers {len(live)} handlers, snapshot has "
            f"{len(_expected())}. If that was intended, regenerate the "
            "snapshot in "
            "the same commit (see the module docstring)."
        )

    def test_every_handler_is_in_the_same_place(self):
        live = _snapshot()
        for i, (got, want) in enumerate(zip(live, _expected())):
            assert got == want, (
                f"registration #{i} changed.\n  was: {want}\n  now: {got}\n\n"
                "PTB runs only the first matching handler per group, so "
                "reordering silently changes which handler receives an update. "
                "If this change was intended, regenerate the snapshot in the same "
                "commit."
            )

    def test_the_whole_sequence_matches(self):
        """The per-line check above stops at the first difference, which is
        the useful message. This one fails on any difference at all, including
        a reordering that the zip above happens to walk past."""
        assert _snapshot() == _expected()


class TestTheCatchAllsAreStillLast:
    """Not a snapshot assertion — a property one, and the property that the
    snapshot cannot express. If someone regenerates the snapshot to silence a
    failure, this still objects.
    """

    def test_nothing_in_group_0_follows_the_unknown_command_handler(self):
        live = [(g, d) for g, d in zip(
            [int(line.split(":", 1)[0]) for line in _snapshot()],
            _snapshot())]
        group0 = [d for g, d in live if g == 0]
        assert group0, "no group-0 handlers found; this test sees nothing"
        assert "cmd_unknown" in group0[-1], (
            "the unknown-command handler must be LAST in group 0, otherwise it "
            f"fires on valid commands. Last is: {group0[-1]}"
        )

    def test_the_free_text_catch_all_is_second_to_last(self):
        live = _snapshot()
        group0 = [line for line in live if line.startswith("0: ")]
        assert len(group0) >= 2
        assert "handle_counter_propose_message" in group0[-2], (
            "the free-text catch-all must sit just before the unknown-command "
            f"handler. Second-to-last is: {group0[-2]}"
        )


HEADER = """\
# Every handler bot.main() registers, in registration order, one per line as
# `group: descriptor`.
#
# GENERATED — do not hand-edit. Regenerate with:
#     python -m tests.test_handler_registration_order
#
# Regenerate it only alongside a DELIBERATE change to the wiring, in the same
# commit. Registration order decides which handler receives an update (PTB
# runs only the first match per group), so a diff here is a behaviour change,
# never a cosmetic one. See tests/test_handler_registration_order.py.
"""


def _write_snapshot() -> None:
    lines = _snapshot()
    if len(lines) < 40:
        raise SystemExit(
            f"refusing to write a {len(lines)}-entry snapshot: the sweep "
            "almost certainly failed rather than main() shrinking. "
            "Nothing written."
        )
    SNAPSHOT_FILE.write_text(HEADER + "\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {len(lines)} entries to {SNAPSHOT_FILE}")


if __name__ == "__main__":
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        _write_snapshot()
