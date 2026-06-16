# Christ of God Ministries Bot — Setup Guide

## 1. Prerequisites

- Python 3.10+
- A Telegram bot token from [@BotFather](https://t.me/BotFather)

## 2. Install dependencies

```bash
cd christofgod_bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 3. Configure the bot

**`config.yaml`** — set your bot token and timezone:
```yaml
bot:
  token: "12345678:AABBCCaabbcc..."
  timezone: "America/New_York"   # IANA tz name
```

**`admins.yaml`** — add admin Telegram usernames (without @):
```yaml
admins:
  - username: "sjeaves2"
    display_name: "Bishop Samuel Eaves, II"
```

**`officials.yaml`** — officials available for appointments.  
Each official must send `/start` to the bot at least once so their chat_id is captured.

## 4. Run

```bash
python bot.py
```

## 5. Bot commands

### All users
| Command | Description |
|---------|-------------|
| `/start` | Register and display welcome + commands |
| `/help` | Show available commands |
| `/events` | Upcoming events in the next 30 days |
| `/exportcalendar` | Download ICS file for all upcoming events |
| `/appointment` | Request a meeting with an official |
| `/stop` | Unsubscribe from notifications |

### Admins only (error shown to non-admins)
| Command | Description |
|---------|-------------|
| `/addevent` | Add a one-time or recurring special event |
| `/modifyevent` | Modify an existing special event |
| `/deleteevent` | Delete a special event or add an urgent announcement to a convocation |
| `/listevents` | Admin view of events in the next 30 days (includes notification times) |
| `/usercount` | Number of registered users |
| `/userlist` | List all users (PDF if > 100) |
| `/adminhelp` | Show admin command list |

## 6. Data files

All data is stored as human-readable YAML in `data/`.  
The bot hot-reloads these files within 60 seconds of a manual edit.

| File | Purpose |
|------|---------|
| `data/events.yaml` | Special events + convocation announcements |
| `data/users.yaml` | Registered users (chat_id, username, display_name) |
| `data/appointments.yaml` | Appointment requests and their status |

## 7. Logs

Activity is logged to `logs/bot_activity.log` in human-readable format.  
Records older than 180 days (6 months) are pruned automatically.

## 8. Notifications

- Convocation events are computed from the Hebrew calendar (`pyluach`) for the current and next Hebrew year.
- Notifications go out 90 minutes before service time by default.
- Sunday Morning Prayer notification goes out 12 hours before (6 pm Saturday).
- A weekly reschedule job keeps the notification window current.
- To add an urgent announcement to a Sabbath/convocation service (e.g. venue change), use `/deleteevent` and select the event.
