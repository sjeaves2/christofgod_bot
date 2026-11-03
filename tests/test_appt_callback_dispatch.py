"""A snapshot of what every `appt:` tap does, one path per line.

WHY THIS EXISTS, AND WHY IT IS BIGGER THAN THE BOARD ONE

`appt_callback` is not a switch with a guard in front of it. It is a PIPELINE:
four places can intercept a tap before any verb branch runs, and the order
between them is load-bearing — the code says so, because rs_accept/rs_decline
are handled early *specifically* to bypass the terminal-status guard below
them.

    parse ─ appt lookup ─ rs_* ─ terminal status ─ past(counter|decline_counter)
          ─ official-side auth + negotiator claim ─ the seven verb branches

Three of those stages apply to a SUBSET of verbs, and one of them (the
negotiator claim) writes to the store and notifies. So this file pins the
guards as carefully as the verbs: a refactor here can change which guard a
verb passes through without changing any verb's own code.

WHAT EACH FIELD IS FOR

Per path: what was sent, how many times the store was SAVED, the appointment's
status afterwards, and which side effects fired — finalize (calendar invite and
confirmations), notify (a message to the requester), negotiate (the "you are
handling this" notice), reschedule (the rs_* handler), plus any
_counter_propose_state and user_data keys set.

`status` and `saved` are what separate a guard from a verb. A tap stopped by a
guard must leave the record untouched; several of the branches differ from one
another only in which datetime field they read, so the status-and-saved pair is
what tells them apart.

The downstream handlers — _finalize_appointment, _handle_reschedule_response —
are recorded as CALLED rather than run. They have their own tests
(tests/test_reschedule.py); re-pinning their internals here would make this
file fail for reasons that have nothing to do with dispatch.

DATES ARE MASKED

Fixtures are built relative to now, so "past" and "future" stay true; absolute
values are then masked. Pinning an absolute datetime would make this file go
red tomorrow on a date nobody changed.

HOW TO UPDATE IT

    python -m tests.test_appt_callback_dispatch

Only alongside a DELIBERATE change, in the same commit. Regenerating it to
make a refactor pass defeats the only thing it is for.
"""

from __future__ import annotations

import copy
import re
import sys
import warnings
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytz

sys.path.insert(0, str(Path(__file__).parent.parent))

import handlers.appointments as ha                    # noqa: E402
import permissions                                    # noqa: E402
import storage                                        # noqa: E402

SNAPSHOT_FILE = Path(__file__).parent / "appt_callback_dispatch.snapshot"

TZ = pytz.timezone("America/New_York")
OFFICIAL_CHAT_ID = 999
USER_CHAT_ID = 111
STRANGER_CHAT_ID = 777

_ISO_DT = re.compile(r"(?<!\d)\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?"
                     r"(?:[+-]\d{2}:\d{2})?(?!\d)")


def _at(days: int, hour: int = 10) -> str:
    """An ISO datetime `days` from now — negative for the past."""
    base = datetime.now(TZ) + timedelta(days=days)
    return base.replace(hour=hour, minute=0, second=0,
                        microsecond=0).isoformat()


def _officials() -> list:
    return [{"id": "off1", "name": "Pastor Test",
             "chat_id": OFFICIAL_CHAT_ID}]


def _appt(**over) -> dict:
    base = {
        "id": "TARGET", "user_chat_id": USER_CHAT_ID,
        "user_username": "requester", "user_display_name": "Test User",
        "official_id": "off1", "official_name": "Pastor Test",
        "requested_datetime": _at(10), "confirmed_datetime": None,
        "description": "x", "status": "pending", "duration_minutes": 30,
    }
    base.update(over)
    return base


def _clashing(**over) -> dict:
    """A CONFIRMED appointment for the same requester at the same time."""
    base = _appt(id="CLASH", official_id="off2", official_name="Bishop X",
                 status="confirmed")
    base["confirmed_datetime"] = base["requested_datetime"]
    base.update(over)
    return base


def _normalize(text: str) -> str:
    return " ".join(_ISO_DT.sub("<WHEN>", text).split())


async def _run_one(label: str, suffix: str, appts: list,
                   actor: int = OFFICIAL_CHAT_ID) -> str:
    ha._counter_propose_state.clear()

    ctx = MagicMock()
    ctx.bot = MagicMock()
    ctx.user_data = {}

    query = MagicMock()
    query.data = f"{ha.CB_APPT_PREFIX}{suffix}"
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.message.chat_id = actor
    query.from_user.id = actor
    query.from_user.username = None
    upd = MagicMock()
    upd.callback_query = query

    saves = 0

    async def _get():
        return appts

    async def _save(updated):
        nonlocal saves
        saves += 1

    finalize = AsyncMock()
    notify_user = AsyncMock()
    negotiate = AsyncMock()
    reschedule = AsyncMock()

    with patch.object(storage, "get_appointments", _get), \
         patch.object(storage, "save_appointments", _save), \
         patch.object(permissions, "OFFICIALS", _officials()), \
         patch.object(permissions, "_ops_chat_ids", set()), \
         patch.object(ha, "_finalize_appointment", finalize), \
         patch.object(ha, "send_markdown", notify_user), \
         patch.object(ha, "_notify_negotiation_started", negotiate), \
         patch.object(ha, "_handle_reschedule_response", reschedule):
        await ha.appt_callback(upd, ctx)

    edit = query.edit_message_text
    text = "<nothing sent>"
    if edit.await_args is not None:
        args, kwargs = edit.await_args
        text = args[0] if args else kwargs.get("text", "")

    target = next((a for a in appts if a["id"] == "TARGET"), None)

    # WHICH time was confirmed, named by the field it came from rather than
    # printed. The absolute value cannot go in the snapshot (it would churn
    # daily), but "status=confirmed" alone cannot tell accept_counter from
    # accept_user_counter reading the wrong field — and landing an
    # appointment at the wrong hour is the worst thing this router can do.
    # A mutation swapping counter_datetime for user_counter_datetime survived
    # the first version of this file.
    confirmed_from = "-"
    if target and target.get("confirmed_datetime"):
        confirmed = target["confirmed_datetime"]
        for field in ("counter_datetime", "user_counter_datetime",
                      "requested_datetime"):
            if target.get(field) == confirmed:
                confirmed_from = field
                break
        else:
            confirmed_from = "something-else"
    effects = "".join((
        "F" if finalize.await_count else "-",
        "N" if notify_user.await_count else "-",
        "G" if negotiate.await_count else "-",
        "R" if reschedule.await_count else "-",
    ))

    return (f"{label} [{suffix}] -> saved={saves} "
            f"status={target['status'] if target else '-'} "
            f"confirmed_from={confirmed_from} "
            f"effects={effects} "
            f"cp={sorted(ha._counter_propose_state) or '-'} "
            f"user_data={sorted(ctx.user_data) or '-'} "
            f"text={_normalize(str(text))}")


def _cases() -> list[tuple[str, str, list, int]]:
    """Every path, in the order the pipeline tests them.

    Labelled, because several verbs reach the same guard and the raw callback
    data alone would not say which path a line describes.
    """
    return [
        # --- parse and lookup -------------------------------------------
        ("malformed-no-id", "confirm", [_appt()], OFFICIAL_CHAT_ID),
        ("appt-missing", "confirm:NOPE", [_appt()], OFFICIAL_CHAT_ID),
        # --- rs_* run BEFORE the terminal guard, deliberately ------------
        ("rs_accept-on-confirmed", "rs_accept:TARGET",
         [_appt(status="confirmed")], USER_CHAT_ID),
        ("rs_decline-on-confirmed", "rs_decline:TARGET",
         [_appt(status="confirmed")], USER_CHAT_ID),
        # --- the terminal-status guard -----------------------------------
        ("terminal-confirmed", "confirm:TARGET",
         [_appt(status="confirmed")], OFFICIAL_CHAT_ID),
        ("terminal-declined", "decline:TARGET",
         [_appt(status="declined")], OFFICIAL_CHAT_ID),
        ("terminal-cancelled", "counter:TARGET",
         [_appt(status="cancelled")], OFFICIAL_CHAT_ID),
        # --- the past guard, which applies to two verbs only -------------
        ("past-counter", "counter:TARGET",
         [_appt(requested_datetime=_at(-2))], OFFICIAL_CHAT_ID),
        ("past-decline_counter", "decline_counter:TARGET",
         [_appt(requested_datetime=_at(-2))], USER_CHAT_ID),
        ("past-confirm-is-allowed", "confirm:TARGET",
         [_appt(requested_datetime=_at(-2))], OFFICIAL_CHAT_ID),
        # --- official-side auth and the negotiator claim -----------------
        ("unauthorized-actor", "confirm:TARGET", [_appt()], STRANGER_CHAT_ID),
        ("claim-by-first-actor", "decline:TARGET", [_appt()],
         OFFICIAL_CHAT_ID),
        ("already-claimed-by-other", "confirm:TARGET",
         [_appt(negotiator_chat_id=12345, negotiator_name="Someone Else")],
         OFFICIAL_CHAT_ID),
        ("user-side-verb-skips-auth", "accept_counter:TARGET",
         [_appt(counter_datetime=_at(5))], USER_CHAT_ID),
        # --- the seven verb branches -------------------------------------
        ("confirm", "confirm:TARGET", [_appt()], OFFICIAL_CHAT_ID),
        ("confirm-clash", "confirm:TARGET", [_appt(), _clashing()],
         OFFICIAL_CHAT_ID),
        ("decline", "decline:TARGET", [_appt()], OFFICIAL_CHAT_ID),
        ("counter", "counter:TARGET", [_appt()], OFFICIAL_CHAT_ID),
        ("accept_counter", "accept_counter:TARGET",
         [_appt(counter_datetime=_at(5))], USER_CHAT_ID),
        ("accept_counter-past", "accept_counter:TARGET",
         [_appt(counter_datetime=_at(-5))], USER_CHAT_ID),
        ("accept_counter-clash", "accept_counter:TARGET",
         [_appt(counter_datetime=_at(10)), _clashing()], USER_CHAT_ID),
        ("decline_counter", "decline_counter:TARGET", [_appt()],
         USER_CHAT_ID),
        ("accept_user_counter", "accept_user_counter:TARGET",
         [_appt(user_counter_datetime=_at(5))], OFFICIAL_CHAT_ID),
        ("accept_user_counter-past", "accept_user_counter:TARGET",
         [_appt(user_counter_datetime=_at(-5))], OFFICIAL_CHAT_ID),
        ("accept_user_counter-clash", "accept_user_counter:TARGET",
         [_appt(user_counter_datetime=_at(10)), _clashing()],
         OFFICIAL_CHAT_ID),
        ("decline_user_counter", "decline_user_counter:TARGET", [_appt()],
         OFFICIAL_CHAT_ID),
        # --- the fall-through --------------------------------------------
        ("unknown-verb", "teleport:TARGET", [_appt()], OFFICIAL_CHAT_ID),
    ]


def _snapshot() -> list[str]:
    import asyncio

    async def _all():
        out = []
        for label, suffix, appts, actor in _cases():
            # deepcopy per case: the branches mutate the records in place, and
            # a shallow copy would leak a status change into later lines.
            out.append(await _run_one(label, suffix, copy.deepcopy(appts),
                                      actor))
        return out

    return asyncio.run(_all())


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------

def test_the_snapshot_file_exists():
    assert SNAPSHOT_FILE.exists(), (
        "missing tests/appt_callback_dispatch.snapshot — regenerate with "
        "python -m tests.test_appt_callback_dispatch")


def test_dispatch_is_unchanged():
    """If this fails after a refactor, the move was not pure. Read the diff;
    each line is labelled with the path it describes."""
    expected = [line for line in SNAPSHOT_FILE.read_text().splitlines()
                if line.strip() and not line.startswith("#")]
    assert _snapshot() == expected


def test_generating_it_twice_gives_the_same_answer():
    """Order-independence. The branches mutate appointments in place, so a
    shallow copy would make each line depend on the ones before it."""
    assert _snapshot() == _snapshot()


def test_no_absolute_datetime_survives_into_the_snapshot():
    raw = re.compile(r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)")
    for line in _snapshot():
        assert not raw.search(line), f"unmasked datetime in: {line}"
    body = [ln for ln in SNAPSHOT_FILE.read_text().splitlines()
            if ln.strip() and not ln.startswith("#")]
    for line in body:
        assert not raw.search(line), f"unmasked datetime in snapshot: {line}"


def test_every_verb_the_router_handles_is_covered():
    """Read from the SOURCE, so adding a branch without a case fails here.

    Replace this with a read of the dispatch table if appt_callback ever grows
    one — the table would be authoritative where this is inference.
    """
    source = (Path(__file__).parent.parent / "handlers"
              / "appointments.py").read_text()
    body = source[source.index("async def appt_callback"):]
    body = body[:body.index("\nasync def _finalize_appointment")]
    verbs = set(re.findall(r'action == "([a-z_]+)"', body))
    verbs |= set(re.findall(r'action in \("([a-z_]+)", "([a-z_]+)"\)', body)
                 and [v for pair in
                      re.findall(r'action in \("([a-z_]+)", "([a-z_]+)"\)',
                                 body) for v in pair])
    covered = {s.partition(":")[0] for _l, s, _a, _c in _cases()}
    missing = verbs - covered
    assert not missing, f"appt_callback verbs with no case pinned: {missing}"


def test_the_source_scan_actually_finds_verbs():
    """Guards the guard: a regex matching nothing would pass forever."""
    source = (Path(__file__).parent.parent / "handlers"
              / "appointments.py").read_text()
    verbs = set(re.findall(r'action == "([a-z_]+)"', source))
    assert {"confirm", "decline", "counter", "accept_counter",
            "decline_user_counter"} <= verbs, verbs


#: Paths stopped by a PIPELINE stage — before any verb body runs. These must
#: be completely inert: nothing written, nothing sent to anyone else.
_PIPELINE_STOPS = (
    "malformed-no-id", "appt-missing", "terminal-confirmed",
    "terminal-declined", "terminal-cancelled", "past-counter",
    "past-decline_counter", "unauthorized-actor",
    "already-claimed-by-other", "unknown-verb",
)

#: Paths where the VERB ran and refused. Not inert, and that is not a bug:
#: for an official-side verb the negotiator claim has already fired, so the
#: record was written and the "you are handling this" notice sent before the
#: verb validated anything. See test_a_refused_verb_leaves_the_appointment
#: _unconfirmed for what must still hold.
_VERB_REFUSALS = (
    "confirm-clash", "accept_counter-past", "accept_counter-clash",
    "accept_user_counter-past", "accept_user_counter-clash",
)


def test_a_pipeline_stop_is_completely_inert():
    """The property that separates a guard stage from a verb.

    A tap stopped before any verb body runs must write nothing and notify
    nobody. A refactor that moved a stage to AFTER the negotiator claim would
    still return the same text, and only this would notice.
    """
    lines = {line.split(" [")[0]: line for line in _snapshot()}
    for label in _PIPELINE_STOPS:
        line = lines[label]
        assert "saved=0" in line, f"pipeline stop wrote to the store: {line}"
        assert "effects=----" in line, f"pipeline stop had effects: {line}"


def test_a_refused_verb_leaves_the_appointment_unconfirmed():
    """Verb-level refusals are NOT inert, deliberately.

    `confirm` on a clashing time writes once and sends the negotiation notice
    before refusing, because the official-side claim runs first — they are
    now the one handling this appointment even though their first action was
    rejected. What must never happen is the appointment ending up confirmed,
    or the calendar invite going out.
    """
    lines = {line.split(" [")[0]: line for line in _snapshot()}
    for label in _VERB_REFUSALS:
        line = lines[label]
        assert "status=pending" in line, f"refused verb changed status: {line}"
        assert "effects=F" not in line, f"refused verb finalized: {line}"
        assert "-N" not in line.split("effects=")[1][:4], (
            f"refused verb notified the requester: {line}")


def test_the_two_categories_between_them_cover_every_refusal():
    """So a new refusal path cannot be added without choosing a category.

    Any line that neither confirms nor progresses the appointment is a
    refusal, and every refusal has to be classified as a pipeline stop or a
    verb refusal — otherwise the two tests above quietly stop covering it.
    """
    classified = set(_PIPELINE_STOPS) | set(_VERB_REFUSALS)
    for line in _snapshot():
        label = line.split(" [")[0]
        refused = ("⚠️" in line or "already been" in line
                   or "already being handled" in line
                   or "no longer works" in line
                   or "not found" in line)
        if refused:
            assert label in classified, (
                f"unclassified refusal path {label!r}: add it to "
                "_PIPELINE_STOPS or _VERB_REFUSALS")


def test_the_reschedule_pair_bypasses_the_terminal_guard():
    """The one ordering the code comments on, and the reason rs_* sits above
    the terminal guard: a reschedule acts on an ALREADY-confirmed appointment,
    so a terminal check would reject every one of them."""
    lines = {line.split(" [")[0]: line for line in _snapshot()}
    for label in ("rs_accept-on-confirmed", "rs_decline-on-confirmed"):
        assert "effects=---R" in lines[label], lines[label]
        assert "already been" not in lines[label], lines[label]


HEADER = """\
# What every `appt:` tap does, one path per line. GENERATED — do not hand-edit.
#
# Regenerate with:
#     python -m tests.test_appt_callback_dispatch
#
# Only alongside a DELIBERATE change, in the same commit. appt_callback is a
# pipeline: four stages can intercept a tap before any verb runs, and the
# order between them is load-bearing. effects= is F)inalize N)otify-requester
# G)negotiation-started R)eschedule-handler. Datetimes are masked to <WHEN>;
# see tests/test_appt_callback_dispatch.py.
#
# Two numbers here read oddly and are correct:
#
#   saved=2 on `decline` — the negotiator claim writes, then the verb writes
#   again. Two round trips to disk for one tap.
#
#   saved=0 on `accept_counter` despite status=confirmed — that branch
#   delegates persistence to _finalize_appointment, which this file records as
#   called rather than running. So saved= counts the router's OWN writes, not
#   every write a tap causes.
"""


def _write_snapshot() -> None:
    lines = _snapshot()
    if len(lines) < 20:
        raise SystemExit(
            f"refusing to write a {len(lines)}-line snapshot: the sweep "
            "almost certainly failed rather than the router shrinking.")
    SNAPSHOT_FILE.write_text(HEADER + "\n".join(lines) + "\n",
                             encoding="utf-8")
    print(f"wrote {len(lines)} path descriptions to {SNAPSHOT_FILE}")


if __name__ == "__main__":
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        _write_snapshot()
