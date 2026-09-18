"""Event aggregation: convocations (from hebrew_calendar) merged with the
special events defined in events.yaml, with announcements, join links, media
and broadcast targets attached.

Depends on settings/storage/common only — no feature modules.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from common import now_tz
from hebrew_calendar import all_upcoming_events
from settings import DEFAULT_NOTIF_MIN, TZ
import storage


def _merge_special_events(
    special_defs: list[dict[str, Any]],
    announcements_map: dict[str, list[str]],
    days_ahead: int = 90,
) -> list[dict[str, Any]]:
    """Expand special_events definitions into concrete upcoming event dicts.

    All special events (including the weekly Sunday Morning Prayer) are driven
    from events.yaml here; convocations/Sabbath come from hebrew_calendar.py.
    """
    now = now_tz()
    cutoff = now + timedelta(days=days_ahead)
    results: list[dict[str, Any]] = []
    for defn in special_defs:
        if not defn.get("active", True):
            continue
        etype = defn.get("type", "once")
        if etype == "weekly":
            wd_target = int(defn["weekday"])
            current = now.date()
            end = cutoff.date()
            while current <= end:
                if current.weekday() == wd_target:
                    h, m = [int(x) for x in defn["time"].split(":")]
                    svc_dt = TZ.localize(datetime(current.year, current.month, current.day, h, m))
                    notif_dt = svc_dt - timedelta(
                        minutes=int(defn.get("notification_minutes", DEFAULT_NOTIF_MIN)))
                    if svc_dt > now:
                        key = f"{defn['id']}_{current.isoformat()}"
                        results.append({
                            "key": key,
                            "name": defn["name"],
                            "type": "special",
                            "service_time": svc_dt,
                            "notification_time": notif_dt,
                            "duration_minutes": defn.get("duration_minutes", 60),
                            "description": defn.get("description", ""),
                            "url": defn.get("url", ""),
                            "image": defn.get("image", ""),
                            "document": defn.get("document", ""),
                            "targets": defn.get("targets", []),
                            "announcements": announcements_map.get(key, []),
                            # ICS revision counter, bumped by /modifyevent when
                            # the service is moved in time.
                            "sequence": int(defn.get("sequence", 0)),
                        })
                current += timedelta(days=1)
        elif etype == "once":
            date_str = defn.get("date")
            if not date_str:
                continue
            h, m = [int(x) for x in defn["time"].split(":")]
            parts = [int(x) for x in date_str.split("-")]
            svc_dt = TZ.localize(datetime(parts[0], parts[1], parts[2], h, m))
            notif_dt = svc_dt - timedelta(
                minutes=int(defn.get("notification_minutes", DEFAULT_NOTIF_MIN)))
            if now <= svc_dt <= cutoff:
                results.append({
                    "key": defn["id"],
                    "name": defn["name"],
                    "type": "special",
                    "service_time": svc_dt,
                    "notification_time": notif_dt,
                    "duration_minutes": defn.get("duration_minutes", 60),
                    "description": defn.get("description", ""),
                    "url": defn.get("url", ""),
                    "image": defn.get("image", ""),
                    "document": defn.get("document", ""),
                    "targets": defn.get("targets", []),
                    "announcements": announcements_map.get(defn["id"], []),
                    "sequence": int(defn.get("sequence", 0)),
                })
    return results


def _resolve_targets(names: list, registry: dict) -> list[int]:
    """Map target names to chat_ids via the registry; pass through raw ids.

    Accepts a list of registry names and/or literal chat ids (int, or a string
    like a "@channelusername" / numeric id). Unknown names are dropped.
    """
    resolved: list = []
    for n in names or []:
        if isinstance(n, int):
            resolved.append(n)
        elif n in registry:
            resolved.append(registry[n])
        elif isinstance(n, str) and (n.startswith("@") or n.lstrip("-").isdigit()):
            resolved.append(int(n) if n.lstrip("-").isdigit() else n)
        # else: unknown name with no registry entry — skip
    # De-duplicate, preserving order
    seen = set()
    out = []
    for c in resolved:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


async def all_upcoming(days_ahead: int = 90) -> list[dict[str, Any]]:
    """Return all events (convocations + special) sorted by service_time."""
    evdata = await storage.get_all_events_data()
    announcements_map: dict[str, list[str]] = evdata.get("convocation_announcements", {})
    urls_map: dict[str, str] = evdata.get("convocation_urls", {})
    images_map: dict[str, str] = evdata.get("convocation_images", {}) or {}
    documents_map: dict[str, str] = evdata.get("convocation_documents", {}) or {}
    special_defs: list[dict[str, Any]] = evdata.get("special_events", [])

    # Notification target registry + convocation target assignments
    registry: dict = evdata.get("notification_targets", {}) or {}
    convo_targets: dict = evdata.get("convocation_targets", {}) or {}
    convo_targets_default: list = evdata.get("convocation_targets_default", []) or []

    convocations = all_upcoming_events(TZ, days_ahead)
    # Attach announcements, per-service join link, and notification target chats.
    for ev in convocations:
        # A combined Sabbath+festival service carries several identities: the
        # join link, media and announcements may be configured against either
        # the festival or the Sabbath, so try each in turn (festival first).
        phase_keys = [k for k in (ev.get("phase_keys") or [ev.get("phase_key")]) if k]
        event_keys = ev.get("component_keys") or [ev["key"]]

        # Union of announcements across every identity, order preserved, no dupes.
        announcements: list[str] = []
        for k in event_keys:
            for ann in announcements_map.get(k, []):
                if ann not in announcements:
                    announcements.append(ann)
        ev["announcements"] = announcements

        for field, source in (("url", urls_map), ("image", images_map),
                              ("document", documents_map)):
            for k in phase_keys:
                if source.get(k):
                    ev[field] = source[k]
                    break

        names = None
        for k in phase_keys:
            if k in convo_targets:
                names = convo_targets[k]
                break
        if names is None:
            names = convo_targets_default
        ev["target_chat_ids"] = _resolve_targets(names, registry)

    specials = _merge_special_events(special_defs, announcements_map, days_ahead)
    for ev in specials:
        ev["target_chat_ids"] = _resolve_targets(ev.get("targets", []), registry)

    merged = convocations + specials
    merged.sort(key=lambda e: e["service_time"])
    return merged


# ---------------------------------------------------------------------------
# Service-link health check
# ---------------------------------------------------------------------------

#: Markers that identify a link copied from data/events.yaml.example and never
#: filled in. SETMEWITHSETSERVICELINK is current; REPLACE_ME is what the
#: template said before v0.17.3, and it is what the LIVE config re-seeded before
#: 2026-09-16 actually contains — a check that knew only the new spelling was
#: blind to the very file that motivated it. Retiring a placeholder means adding
#: it here, not replacing the list.
LINK_PLACEHOLDERS = ("SETMEWITHSETSERVICELINK", "REPLACE_ME")

#: The template's dummy meeting id. Catches a link whose placeholder passcode was
#: replaced but whose meeting was not.
PLACEHOLDER_MEETING = "/j/0000000000"


def _is_placeholder_link(url: str) -> bool:
    return any(m in url for m in LINK_PLACEHOLDERS) or PLACEHOLDER_MEETING in url


async def check_service_links(days_ahead: int = 30) -> list[tuple[str, str]]:
    """Report upcoming services whose join link is unusable.

    Returns (label, reason) pairs, soonest first; empty when all is well. The
    label carries the date because the same service name recurs weekly, and an
    alert naming only "Sabbath Eve" would not say which one to fix.

    Why this exists: nothing in the bot reads data/events.yaml until someone
    asks it to, so on 2026-09-17 the file was found re-seeded from the template
    with ten placeholder links — and the bot had been running normally for a day
    with no reminder due. The next reminder would have carried a dead link to
    the congregation. This looks at the links the way a reminder will, so a
    silent re-seed is noticed at startup rather than by a member.

    Only convocations are reported as *missing* a link: they are the Zoom
    services and always have one. A special event may legitimately be in person.
    A placeholder is reported wherever it appears — it can only have come from
    the template.
    """
    problems: list[tuple[str, str]] = []
    for ev in await all_upcoming(days_ahead):
        url = (ev.get("url") or "").strip()
        label = f"{ev['name']} ({ev['service_time']:%a %d %b})"
        if _is_placeholder_link(url):
            problems.append((label, "still set to the template placeholder"))
        elif not url and ev.get("type") == "convocation":
            problems.append((label, "has no join link"))
    return problems
