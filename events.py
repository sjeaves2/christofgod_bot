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
                    notif_dt = svc_dt - timedelta(minutes=int(defn.get("notification_minutes", DEFAULT_NOTIF_MIN)))
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
                        })
                current += timedelta(days=1)
        elif etype == "once":
            date_str = defn.get("date")
            if not date_str:
                continue
            h, m = [int(x) for x in defn["time"].split(":")]
            parts = [int(x) for x in date_str.split("-")]
            svc_dt = TZ.localize(datetime(parts[0], parts[1], parts[2], h, m))
            notif_dt = svc_dt - timedelta(minutes=int(defn.get("notification_minutes", DEFAULT_NOTIF_MIN)))
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
                })
    return results


def _resolve_targets(names: "list", registry: dict) -> list[int]:
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
        ev["announcements"] = announcements_map.get(ev["key"], [])
        phase_key = ev.get("phase_key")
        if phase_key and urls_map.get(phase_key):
            ev["url"] = urls_map[phase_key]
        if phase_key and images_map.get(phase_key):
            ev["image"] = images_map[phase_key]
        if phase_key and documents_map.get(phase_key):
            ev["document"] = documents_map[phase_key]
        names = convo_targets.get(phase_key) if phase_key else None
        if names is None:
            names = convo_targets_default
        ev["target_chat_ids"] = _resolve_targets(names, registry)

    specials = _merge_special_events(special_defs, announcements_map, days_ahead)
    for ev in specials:
        ev["target_chat_ids"] = _resolve_targets(ev.get("targets", []), registry)

    merged = convocations + specials
    merged.sort(key=lambda e: e["service_time"])
    return merged
