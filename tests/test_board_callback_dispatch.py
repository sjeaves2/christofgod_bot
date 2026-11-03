"""A snapshot of what every `bi:` verb DOES, one verb per line.

WHY THIS EXISTS

`board_callback` is 133 lines of `if verb == ...` and is scheduled to become
dispatch tables. This file is the safety net that makes that rewrite readable
as a pure move, the same role `tests/test_handler_registration_order.py`
played for the `main()` split in v0.24.0.

The existing router tests in `tests/test_board_commands.py` check a handful of
verbs for the behaviour that matters to a user. They are the right shape for
behaviour and the wrong shape for a refactor: they cover some verbs, assert
loosely ("Board Items" appears), and would stay green while another verb
silently changed branch. This file is the opposite — every verb, deliberately
brittle, and expected to be edited only alongside an intentional change.

WHAT IT PINS, AND WHY EACH PART

For each verb: the state returned, whether the store was SAVED, which
`user_data` keys were set, the message text, and the callback_data of every
button offered. The last two matter more than they look. A mistake in this
rewrite does not raise — it routes a tap to the wrong branch, and the cheapest
way to notice is that the wrong text came back or the keyboard offers the
wrong next step. `del_yes` and `done_yes` act on real corporate records.

The store-saved flag is what separates the read-only verbs from the ones with
side effects. A table that accidentally routed `del` (a confirmation prompt)
to `del_yes` (the deletion) would look almost identical in text and differ
here immediately.

DATES ARE MASKED, FOR A REASON THIS PROJECT HAS ALREADY PAID FOR

The fixture's due date is built RELATIVE to today, so the "in N days" text is
stable; absolute dates are then masked to <DATE>. Pinning an absolute date
would make the snapshot churn daily and go red on a date nobody changed —
which is the clock-dependence that took six tests red on 2026-11-03. The
relative text is the part a reader checks anyway.

HOW TO UPDATE IT

Intentionally changing what a verb does? Run

    python -m tests.test_board_callback_dispatch

from the repo root. It rewrites tests/board_callback_dispatch.snapshot in
place; commit that in the SAME commit as the change. Do NOT regenerate it to
make a refactor pass — that is the one thing that would make this file
worthless.
"""

from __future__ import annotations

import asyncio
import copy
import re
import sys
import warnings
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import storage                                       # noqa: E402
import handlers.board as hb                           # noqa: E402
from common import now_tz                             # noqa: E402
from telegram.ext import ConversationHandler          # noqa: E402

SNAPSHOT_FILE = Path(__file__).parent / "board_callback_dispatch.snapshot"

ITEM_ID = "AAA11111"
OFFICER_CHAT_ID = 111

#: Resolved to names so the snapshot reads as states, not as 60/61/62. The
#: numbers are an implementation detail of `range(60, 63)` and would make a
#: renumbering look like a behaviour change.
_STATES = {
    hb.BI_MENU: "BI_MENU",
    hb.BI_EDIT_VALUE: "BI_EDIT_VALUE",
    hb.BI_NEXT_DUE: "BI_NEXT_DUE",
    ConversationHandler.END: "END",
}

# NOT \b at the ends. Markdown wraps some dates in underscores
# ("_Previous cycles: 2026-11-03_"), and `_` is a word character, so \b does
# not match between "3" and "_" — the first draft of this file left exactly
# that date unmasked and would have gone red the next day.
_ISO_DATE = re.compile(r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)")


def _item(**over):
    """One pending item, due a fixed number of days from TODAY.

    Relative, so "in 48 days" is the same string every run. See the module
    docstring on why an absolute date is not used.
    """
    base = {"id": ITEM_ID, "action": "Renew logo registration",
            "due": (now_tz().date() + timedelta(days=48)).isoformat(),
            "notify_days": 60, "status": "pending", "description": "",
            "url": "", "history": []}
    base.update(over)
    return base


def _completed_item():
    return _item(id="BBB22222", action="File annual return",
                 status="completed")


def _cb_update(data: str):
    upd = MagicMock()
    upd.effective_chat.id = OFFICER_CHAT_ID
    upd.effective_user.id = OFFICER_CHAT_ID
    upd.effective_user.username = "officer"
    upd.effective_user.full_name = "Board Officer"
    upd.effective_user.first_name = "Board"
    upd.message = None
    query = MagicMock()
    query.data = data
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.edit_message_reply_markup = AsyncMock()
    upd.callback_query = query
    return upd


class _Store:
    """storage, in memory. Not pytest's monkeypatch: the regenerator below
    runs outside pytest, where fixtures do not exist."""

    def __init__(self, items):
        self.items = items
        self.saves = 0

    @contextmanager
    def installed(self):
        async def get_board_items():
            return self.items

        async def save_board_items(items):
            self.items = items
            self.saves += 1

        async def get_all_users():
            return [{"chat_id": OFFICER_CHAT_ID, "board_officer": True}]

        with patch.object(storage, "get_board_items", get_board_items), \
             patch.object(storage, "save_board_items", save_board_items), \
             patch.object(storage, "get_all_users", get_all_users):
            yield


def _normalize(text: str) -> str:
    """One line, dates masked, so the snapshot is stable and diffs legibly."""
    text = _ISO_DATE.sub("<DATE>", text)
    return " ".join(text.split())


def _buttons(markup) -> str:
    if markup is None:
        return "-"
    data = [b.callback_data for row in markup.inline_keyboard for b in row]
    return ",".join(d if d is not None else "?" for d in data)


async def _run_one(verb: str, items) -> str:
    """Drive board_callback for one verb and describe what it did."""
    store = _Store(items)
    ctx = MagicMock()
    ctx.bot = MagicMock()
    ctx.user_data = {}
    upd = _cb_update(f"{hb.CB_BOARD_PREFIX}{verb}")

    with store.installed():
        state = await hb.board_callback(upd, ctx)

    edit = upd.callback_query.edit_message_text
    if edit.await_args is None:
        text, markup = "<nothing sent>", None
    else:
        args, kwargs = edit.await_args
        text = args[0] if args else kwargs.get("text", "")
        markup = kwargs.get("reply_markup")

    return (f"{verb} -> state={_STATES.get(state, repr(state))} "
            f"saved={store.saves} "
            f"user_data={sorted(ctx.user_data) or '-'} "
            f"buttons=[{_buttons(markup)}] "
            f"text={_normalize(str(text))}")


def _cases() -> list[tuple[str, list]]:
    """Every verb, with the store state that reaches its branch.

    Ordered as the function tests them, so the snapshot reads in the same
    order as the code it pins — a reordering of the branches shows as a move.
    """
    pending = [_item()]
    cases: list[tuple[str, list]] = [
        # Phase 1: handled BEFORE the item lookup, so they need no id.
        ("close", pending),
        ("list", pending),
        ("completed", [_completed_item()]),
        ("completed", pending),          # none completed — the empty branch
        # The guard between the phases.
        (f"show:{ITEM_ID}", []),         # item vanished from another phone
        # Phase 2: every verb that needs the item.
        (f"show:{ITEM_ID}", pending),
        (f"done:{ITEM_ID}", pending),
        (f"done_yes:{ITEM_ID}", pending),
        (f"reopen:{ITEM_ID}", pending),
        (f"del:{ITEM_ID}", pending),
        (f"del_yes:{ITEM_ID}", pending),
        (f"edit:{ITEM_ID}", pending),
    ]
    # ef_<field> is a PREFIX family, not a verb: `verb.startswith("ef_")`.
    # Every legal field is pinned, because a table keyed on exact verbs has to
    # enumerate them and this is the list it must reproduce.
    cases += [(f"ef_{field}:{ITEM_ID}", pending) for field, _ in hb.EDIT_FIELDS]
    cases += [
        (f"ef_bogus:{ITEM_ID}", pending),   # unknown field -> back to the item
        (f"wat:{ITEM_ID}", pending),        # unknown verb -> the fall-through
    ]
    return cases


def _snapshot() -> list[str]:
    async def _all():
        # deepcopy, not [dict(i) for i in items]: a shallow copy shares the
        # nested `history` list, so done_yes appending a cycle to it leaked
        # into every later case. The first draft did exactly that and showed a
        # phantom "Previous cycles" line on ef_bogus — a snapshot whose lines
        # depend on the order they were generated in pins nothing.
        return [await _run_one(verb, copy.deepcopy(items))
                for verb, items in _cases()]

    return asyncio.run(_all())


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------

def test_the_snapshot_file_exists():
    assert SNAPSHOT_FILE.exists(), (
        "missing tests/board_callback_dispatch.snapshot — regenerate with "
        "python -m tests.test_board_callback_dispatch")


def test_dispatch_is_unchanged():
    """The whole point. If this fails after a refactor, the move was not pure.

    Do not regenerate to make it pass; read the diff. Each line names the verb
    it describes, so the failure says which verb changed branch.
    """
    expected = [line for line in SNAPSHOT_FILE.read_text().splitlines()
                if line.strip() and not line.startswith("#")]
    assert _snapshot() == expected


def test_no_absolute_date_survives_into_the_snapshot():
    """Otherwise the file goes red tomorrow, on a date nobody changed.

    This caught the real thing: a markdown-italicised date
    ("_Previous cycles: 2026-11-03_") escaped a `\\b`-anchored mask, because
    `_` is a word character. Checked against the FILE as well as the live
    sweep, since the file is what CI compares.
    """
    raw = re.compile(r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)")
    for line in _snapshot():
        assert not raw.search(line), f"unmasked date in: {line}"
    body = [ln for ln in SNAPSHOT_FILE.read_text().splitlines()
            if ln.strip() and not ln.startswith("#")]
    for line in body:
        assert not raw.search(line), f"unmasked date in the snapshot: {line}"


def test_generating_it_twice_gives_the_same_answer():
    """Order-independence. The first draft shared one `history` list across
    every case, so done_yes appending to it changed later lines — a snapshot
    that depends on the order it was built in pins nothing at all."""
    assert _snapshot() == _snapshot()


def test_every_verb_in_the_router_is_covered():
    """A pin that misses a verb is worse than no pin: it reads as complete.

    The verbs are read from the dispatch TABLES, which are authoritative.
    Until board_callback became tables this had to regex `if verb == "..."`
    out of the source; the tables are the better source and the reason the
    regex version is gone.
    """
    in_tables = set(hb._QUEUE_VERBS) | set(hb._ITEM_VERBS)
    covered = {v.partition(":")[0] for v, _ in _cases()}
    missing = in_tables - covered
    assert not missing, f"board_callback verbs with no case pinned: {missing}"


def test_the_tables_are_populated():
    """Guards the guard: two empty dicts would make the check above vacuous."""
    assert {"close", "list", "completed"} <= set(hb._QUEUE_VERBS)
    assert {"show", "done", "done_yes", "reopen", "del", "del_yes",
            "edit"} <= set(hb._ITEM_VERBS)


def test_the_two_tables_are_disjoint():
    """A verb in both would be answered by the queue table and the item one
    would never run — silently, since the queue table is consulted first."""
    overlap = set(hb._QUEUE_VERBS) & set(hb._ITEM_VERBS)
    assert not overlap, f"verb in both tables: {overlap}"


def test_every_editable_field_has_a_handler():
    """EDIT_FIELDS drives the buttons AND the table. A field with a button but
    no handler falls back to the item view, which looks like a dead button."""
    for field, _label in hb.EDIT_FIELDS:
        assert f"{hb.EDIT_FIELD_PREFIX}{field}" in hb._ITEM_VERBS, field


def test_no_queue_verb_needs_an_item():
    """The split is only sound if the queue handlers never touch an item.

    Their signature says so — they take (tap) and not (tap, item) — and this
    asserts it, because the signature is what keeps the item-is-None guard in
    one place instead of eleven.
    """
    import inspect
    for verb, handler in hb._QUEUE_VERBS.items():
        params = list(inspect.signature(handler).parameters)
        assert params == ["tap"], f"{verb}: {params}"
    for verb, handler in hb._ITEM_VERBS.items():
        params = list(inspect.signature(handler).parameters)
        assert params == ["tap", "item"], f"{verb}: {params}"


def test_side_effecting_verbs_are_distinguishable_from_their_prompts():
    """The confirmation/execution pairs are the dangerous ones.

    `del` prompts and `del_yes` deletes; `done` prompts and `done_yes`
    completes. Routing one to the other is the mistake this rewrite could make
    without raising, so the snapshot must record a DIFFERENT saved= count for
    each pair — otherwise it could not tell them apart.
    """
    lines = {line.split(" -> ")[0]: line for line in _snapshot()}
    for prompt, execute in (("del", "del_yes"), ("done", "done_yes")):
        p = lines[f"{prompt}:{ITEM_ID}"]
        e = lines[f"{execute}:{ITEM_ID}"]
        assert "saved=0" in p, f"{prompt} should not write: {p}"
        assert "saved=0" not in e, f"{execute} must write: {e}"


HEADER = """\
# What every `bi:` verb does, one verb per line. GENERATED — do not hand-edit.
#
# Regenerate with:
#     python -m tests.test_board_callback_dispatch
#
# Only alongside a DELIBERATE change to what a verb does, in the same commit.
# This file exists to prove the dispatch-table rewrite of board_callback is a
# pure move, so regenerating it to make a refactor pass defeats its only
# purpose. Absolute dates are masked to <DATE>; see
# tests/test_board_callback_dispatch.py.
"""


def _write_snapshot() -> None:
    lines = _snapshot()
    if len(lines) < 15:
        raise SystemExit(
            f"refusing to write a {len(lines)}-line snapshot: the sweep "
            "almost certainly failed rather than the router shrinking. "
            "Nothing written.")
    SNAPSHOT_FILE.write_text(HEADER + "\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {len(lines)} verb descriptions to {SNAPSHOT_FILE}")


if __name__ == "__main__":
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        _write_snapshot()
