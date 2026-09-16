# BotFather settings — the bot's public identity

Everything on this page lives **only in BotFather**, not in the running code.
There is no API to read a previous value and BotFather keeps no history, so if
one of these is changed or lost, this file is the only copy.

That is not hypothetical. On **2026-09-16** someone with access to the owning
Telegram account changed the bot's name, avatar and description. The name and
avatar were restored from memory; the description could not be, because nobody
had written it down. The text below is a reconstruction, not the original.

Keep this file updated whenever any of it changes in BotFather.

---

## Identity

| Field | Value |
|---|---|
| Username | `@christofgod_bot` |
| Name | `Christ of God bot` |
| Avatar ("botpic", `/setuserpic`) | `deploy/botfather-avatar.png` (commit the image alongside this file) |
| Description picture | Set, and confirmed legitimate on 2026-09-16 (not attacker residue). Export and commit it as `deploy/botfather-description-pic.png` so it is restorable. |
| Privacy policy URL | `https://github.com/sjeaves2/christofgod_bot/blob/main/PRIVACY.md` |

**Avatar vs description picture.** The *avatar* is the round image shown beside
every message, everywhere. The *description picture* is a photo or short video
shown above the description text on the "What can this bot do?" screen, which a
person sees only BEFORE pressing Start. Neither is readable through the Bot API
— `getMyDescription` returns the text only — so BotFather is the only way to
see or set them, and this file is the only record.

### Short description

Shown on the bot's profile, 120 characters maximum.

```
Automated Telegram bot for Christ of God Ministries
```

### Description

Shown in the "What can this bot do?" panel before a user has interacted,
512 characters maximum.

```
Automated assistant for Christ of God Ministries.

I send reminders for God's Holy Convocations, special services and events, post announcements, and help you arrange a meeting with a church official.

Send /start to begin, or /help to see what I can do.
```

---

## Settings that matter for safety

| Setting | Required value | Why |
|---|---|---|
| Privacy **mode** | **ON** (`/setprivacy` → Enable) | With it off the bot receives EVERY message in every group it belongs to. It only needs commands addressed to it. Not to be confused with the privacy *policy* below. |
| Privacy **policy** | URL set, pointing at `PRIVACY.md` | A link shown to users describing what the bot stores. Required in spirit by Telegram for bots handling personal data — and this one stores phone numbers and appointment reasons. |
| Inline mode | OFF | Unused. Enabling it widens the surface for no benefit. |
| Allow groups | ON | The bot posts reminders into congregation groups. |
| Payments | Not configured | See the standing decision against payment-gateway work. |

Verify these after any suspected compromise. An attacker turning privacy mode
OFF would silently start feeding the congregation's group messages to the bot.

---

## Commands

**Do not set these by hand in BotFather.** `post_init` in `bot.py` calls
`set_my_commands` on every start, so the code is the source of truth and any
manual edit is overwritten at the next restart. There are currently 14.

---

## Restoring after a compromise

Order matters. Securing the account first is what makes the rest stick — an
attacker still holding a session can simply read the new token out of BotFather.

1. **Telegram → Settings → Devices → Terminate all other sessions.** Changing
   the password does NOT evict an existing session.
2. **Settings → Privacy and Security → Two-Step Verification** — set a password
   and a recovery email. Without both, an SMS code is the only barrier to
   logging in as you.
3. **Revoke the bot token**: `/mybots` → the bot → API Token → Revoke. Then
   update `config/config.yaml` on the server and
   `sudo systemctl restart christofgod-bot`.
4. **Restore the identity above** — name, avatar, description, short
   description — and re-check the safety settings table.
5. **Verify** what the API now reports, which catches anything missed:

```bash
ssh ubuntu@<server> 'cd ~/repos/christofgod_bot && .venv/bin/python - <<PY
import asyncio, yaml
from telegram import Bot
token = yaml.safe_load(open("config/config.yaml"))["bot"]["token"]
async def main():
    b = Bot(token)
    me = await b.get_me()
    print("name       :", me.first_name)
    print("privacy ON :", not me.can_read_all_group_messages)
    print("inline off :", not me.supports_inline_queries)
    print("descr      :", (await b.get_my_description()).description)
    print("short      :", (await b.get_my_short_description()).short_description)
    print("commands   :", len(await b.get_my_commands()))
asyncio.run(main())
PY'
```

Step 5 is what found the planted invite link in the description on 2026-09-16,
after the visible fields had already been corrected by hand.

---

## Optional hardening, not yet implemented

`post_init` could call `set_my_name`, `set_my_description` and
`set_my_short_description` the same way it already calls `set_my_commands`.
The identity above would then be declarative and **self-restoring on every
restart** — an attacker's edits would survive only until the next deploy.

The trade-off is that changing the description would require a code change and
a restart rather than a BotFather edit. Worth doing if this happens twice.
