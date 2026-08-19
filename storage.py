"""File-backed data stores: cache instances and their load/save accessors.

Pure persistence — no domain logic. Domain-level lifecycle (archiving, purges,
derived state) stays with its feature code.
"""

from __future__ import annotations

from typing import Any

from cache import FileCache
from settings import DATA_DIR

# ---------------------------------------------------------------------------
# File caches
# ---------------------------------------------------------------------------

# Round-trip mode: admins hand-edit events.yaml, so comments/formatting must
# survive the bot's own saves (e.g. /setservicelink).
events_cache = FileCache(DATA_DIR / "events.yaml", round_trip=True)
users_cache = FileCache(DATA_DIR / "users.yaml")
appts_cache = FileCache(DATA_DIR / "appointments.yaml")
# Long-term storage for appointments archived out of the live file.
appts_archive_cache = FileCache(DATA_DIR / "appointments_archive.yaml")
# General announcements shown by /announcements (state derived from expires).
ann_cache = FileCache(DATA_DIR / "announcements.yaml")
# Tracks which recipients have already been notified for each event, so a
# missed/partial broadcast can be retried later without duplicate sends.
notif_state_cache = FileCache(DATA_DIR / "notification_state.yaml")
# Groups/channels the bot has been added to (discovered via membership events),
# used to populate the /broadcast target list.
groups_cache = FileCache(DATA_DIR / "known_groups.yaml")

# ---------------------------------------------------------------------------
# Accessors
# ---------------------------------------------------------------------------


async def get_all_users() -> list[dict[str, Any]]:
    data = await users_cache.get()
    return data.get("users") or [] if data else []


async def save_users(users: list[dict[str, Any]]) -> None:
    data = users_cache._data or {}
    data["users"] = users
    await users_cache.save(data)


async def get_all_events_data() -> dict[str, Any]:
    data = await events_cache.get()
    return data or {}


async def save_events_data(data: dict[str, Any]) -> None:
    await events_cache.save(data)


async def get_appointments() -> list[dict[str, Any]]:
    data = await appts_cache.get()
    return data.get("appointments") or [] if data else []


async def save_appointments(appts: list[dict[str, Any]]) -> None:
    data = appts_cache._data or {}
    data["appointments"] = appts
    await appts_cache.save(data)


async def get_announcements() -> list[dict[str, Any]]:
    data = await ann_cache.get()
    return data.get("announcements") or [] if data else []


async def save_announcements(anns: list[dict[str, Any]]) -> None:
    data = ann_cache._data or {}
    data["announcements"] = anns
    await ann_cache.save(data)


async def _load_notif_state() -> dict[str, Any]:
    data = await notif_state_cache.get()
    return (data or {}).get("states") or {}


async def _save_notif_state(states: dict[str, Any]) -> None:
    await notif_state_cache.save({"states": states})


async def _load_known_groups() -> dict[str, Any]:
    data = await groups_cache.get()
    return (data or {}).get("groups") or {}


async def _save_known_groups(groups: dict[str, Any]) -> None:
    await groups_cache.save({"groups": groups})
