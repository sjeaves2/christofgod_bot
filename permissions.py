"""Identity and authorization: admins, church officials, and their proxies.

Admins and officials are loaded once at startup from config/. Callers should
reference module attributes (permissions.OFFICIALS, permissions.is_admin) so a
single patch point covers every module.
"""

from __future__ import annotations

import re
from typing import Any

import yaml
from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler

from cache import make_rt_yaml
from settings import CONFIG_DIR

# Admins are loaded once at startup and kept in memory.
# Each entry may carry a `username`, a `phone`, or both.
_admins_raw = yaml.safe_load((CONFIG_DIR / "admins.yaml").read_text()) or {}
ADMIN_USERNAMES: set[str] = {
    a["username"].lstrip("@").lower()
    for a in _admins_raw.get("admins", [])
    if a.get("username")
}
ADMIN_PHONES: set[str] = {
    re.sub(r"\D", "", a["phone"])
    for a in _admins_raw.get("admins", [])
    if a.get("phone")
}
# chat_id → True for admins identified by phone after they /start the bot
_admin_chat_ids: set[int] = set()

# Officials. Loaded in ruamel round-trip mode because the bot rewrites this
# file (auto-filled chat_ids, /enable_appt_proxies) and admins hand-edit it —
# round-trip keeps their comments and formatting intact. OFFICIALS is the live
# node from the loaded document, so mutating it and calling _save_officials()
# writes the change back without disturbing anything else in the file.
_rt_yaml = make_rt_yaml()
_officials_path = CONFIG_DIR / "officials.yaml"
try:
    with open(_officials_path, encoding="utf-8") as _fh:
        _officials_doc = _rt_yaml.load(_fh) or {}
except FileNotFoundError:
    _officials_doc = {}
OFFICIALS: list[dict[str, Any]] = _officials_doc.get("officials") or []


def user_info(update: Update) -> tuple[int, str | None, str]:
    u = update.effective_user
    return u.id, u.username, u.full_name or u.first_name or str(u.id)

def is_admin(update: Update) -> bool:
    """Return True if the user is listed in admins.yaml by username or phone."""
    u = update.effective_user
    if (u.username or "").lower() in ADMIN_USERNAMES:
        return True
    if u.id in _admin_chat_ids:
        return True
    return False


async def _register_admin_by_phone(user_id: int, phone: str | None) -> None:
    """Cache chat_id when a phone-number-only admin shares their contact."""
    if not phone:
        return
    normalized = re.sub(r"\D", "", phone)
    if normalized in ADMIN_PHONES:
        _admin_chat_ids.add(user_id)


async def _register_admin_by_username(user_id: int, username: str | None) -> None:
    """Cache chat_id for username-based admins (no-op if already in set)."""
    if (username or "").lower() in ADMIN_USERNAMES:
        _admin_chat_ids.add(user_id)


def _is_known_official(user_id: int, username: str | None) -> bool:
    """Return True if this user is already linked to an official entry."""
    uname_lower = (username or "").lstrip("@").lower()
    for off in OFFICIALS:
        if off.get("chat_id") == user_id:
            return True
        if uname_lower:
            oname = (off.get("telegram_username") or "").lstrip("@").lower()
            if oname and oname == uname_lower:
                return True
    return False



def _save_officials() -> None:
    """Persist the OFFICIALS list (with any auto-populated chat_ids / flags).

    Writes through the round-trip document so the file's comments and layout
    survive. OFFICIALS is re-attached first, so this is still correct if a
    caller (or a test) replaced the module-level list wholesale.
    """
    _officials_doc["officials"] = OFFICIALS
    with open(_officials_path, "w", encoding="utf-8") as fh:
        _rt_yaml.dump(_officials_doc, fh)


def _official_by_id(official_id: "str | None") -> "dict | None":
    return next((o for o in OFFICIALS if o.get("id") == official_id), None)


def _person_matches(rec: dict, user_id: int, uname_lower: str) -> bool:
    """Match a person record (official or proxy) by chat_id or telegram_username."""
    if rec.get("chat_id") == user_id:
        return True
    rname = (rec.get("telegram_username") or "").lstrip("@").lower()
    return bool(uname_lower) and rname == uname_lower


def _enabled_proxies(off: "dict | None") -> list[dict]:
    """Proxy records for an official, only if proxies are enabled."""
    if not off or not off.get("proxies_enabled"):
        return []
    return off.get("proxies") or []


def _user_can_act_for_official(off: "dict | None", user_id: int, username: "str | None") -> bool:
    """True if the user is the official, or an enabled proxy for that official."""
    if not off:
        return False
    uname_lower = (username or "").lstrip("@").lower()
    if _person_matches(off, user_id, uname_lower):
        return True
    return any(_person_matches(p, user_id, uname_lower) for p in _enabled_proxies(off))


def _user_can_act_for_appt(appt: dict, user_id: int, username: "str | None") -> bool:
    return _user_can_act_for_official(_official_by_id(appt.get("official_id")), user_id, username)


def _acting_identity(off: dict, user_id: int, username: "str | None") -> "tuple[str | None, bool]":
    """Return (display_name, is_proxy) for the acting official/proxy, else (None, False)."""
    uname_lower = (username or "").lstrip("@").lower()
    if _person_matches(off, user_id, uname_lower):
        return off.get("name"), False
    for p in _enabled_proxies(off):
        if _person_matches(p, user_id, uname_lower):
            return (p.get("name") or "A proxy"), True
    return None, False


def _official_side_recipients(off: dict) -> list[dict]:
    """Chats that should receive an appointment request: the official + enabled
    proxies that have started the bot. Each: {chat_id, is_proxy, name}."""
    out: list[dict] = []
    if off.get("chat_id"):
        out.append({"chat_id": off["chat_id"], "is_proxy": False, "name": off.get("name")})
    for p in _enabled_proxies(off):
        if p.get("chat_id"):
            out.append({"chat_id": p["chat_id"], "is_proxy": True, "name": p.get("name") or "Proxy"})
    return out



def admin_only(handler):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_admin(update):
            await update.message.reply_text("⛔ Unknown command.")
            return ConversationHandler.END
        return await handler(update, context)
    wrapper.__name__ = handler.__name__
    return wrapper




def official_username_drift(user_id: int, username: "str | None") -> "dict | None":
    """Detect a known official/proxy whose configured telegram_username is stale.

    Matching is by chat_id — which the bot auto-filled when they ran /start — so
    a mismatch means this same person changed their Telegram @username since
    officials.yaml was written. Their username-based authorization will fail
    until the file is updated, so callers surface this to the admins.

    Returns {kind, name, official_id, configured, current} or None.
    """
    current = (username or "").lstrip("@").lower()
    for off in OFFICIALS:
        candidates = [(off, "official")] + [(p, "proxy") for p in (off.get("proxies") or [])]
        for rec, kind in candidates:
            if rec.get("chat_id") != user_id:
                continue
            configured = (rec.get("telegram_username") or "").lstrip("@").lower()
            if configured and configured != current:
                return {
                    "kind": kind,
                    "name": rec.get("name") or "(unnamed)",
                    "official_id": off.get("id"),
                    "configured": rec.get("telegram_username"),
                    "current": username,
                }
    return None
