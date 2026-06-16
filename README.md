# Christ of God Bot

A Telegram bot for the Christ of God Ministries congregation that sends automated reminders for
Hebrew-calendar convocations and special services, manages appointment requests between
congregants and church officials, and provides administrators with tools for calendar
and member management.

---

## Features

### Event Notifications
- Automatically computes upcoming **God's Holy Convocations** from the Hebrew calendar
  and sends reminders to all registered users 90 minutes before each service
- Covers all annual convocations: Passover, Feast of Unleavened Bread, Shavuot,
  Rosh Hashanah, Yom Kippur, and Succoth (Feast of Tabernacles)
- Sends weekly **Sabbath Eve** (Friday 6:00 PM) and **Sabbath Morning** (Saturday 11:00 AM) reminders
- Sends weekly **Sunday Morning Prayer** reminders 12 hours in advance (Saturday 6:00 PM)
- Supports administrator-added **special events** on a one-time or recurring basis
- Urgent announcements (e.g. venue changes) can be attached to any service and are
  included in the outgoing notification

### Appointment Requests
- Congregants can request a meeting with a church official through the bot
- The request includes the user's display name and profile photo (if available)
- Officials receive the request with **Confirm / Suggest different time / Decline** options
- If an official suggests a different time, the user is notified and can accept,
  propose yet another time, or cancel
- Once both parties agree, each receives a confirmation and an **ICS calendar file**
  to import into their personal calendar app
- Every request is assigned a unique ID for tracking

### Calendar Export
- Users can download an **ICS file** containing all upcoming events (convocations,
  special services, and recurring events) for import into Apple Calendar, Google
  Calendar, Outlook, or any standard calendar app

### Admin Tools
- Add, modify, or delete special events through guided bot conversations
- Attach urgent announcements to convocation services without deleting them
- View a formatted list of all events in the next 30 days, including notification times
- Get a count of registered users
- List all registered users by display name and username; automatically generates a
  **PDF file** when the list exceeds 100 users

### Access Control
- Admin commands are restricted to users listed in `config/admins.yaml`
- Admins and officials can be identified by **Telegram username, phone number, or both**
- Users who are phone-number-only (no Telegram username) are identified when they
  share their contact via a one-time prompt on `/start`
- Non-admins who attempt admin commands see an "Unknown command" response

### Activity Logging
- Every command, new user registration, and unsubscription is written to a
  human-readable activity log
- Log entries include timestamp, display name, username, and relevant details
- Log records are retained for **six months** and pruned automatically

---

## Convocation Schedule

Convocation events follow the Hebrew calendar. Service times and notification windows:

| Convocation | Hebrew Date | Service | Time | Notification |
|---|---|---|---|---|
| Passover | Abib 14 | at Even | 3:00 PM | 90 min before |
| Feast of Unleavened Bread | Abib 15 | Opening Day | 11:00 AM | 90 min before |
| Feast of Unleavened Bread | Abib 21 | Closing Eve | 6:00 PM | 90 min before |
| Feast of Unleavened Bread | Abib 21 | Closing Day | 11:00 AM | 90 min before |
| Shavuot | Sivan 6 | Eve | 6:00 PM | 90 min before |
| Shavuot | Sivan 6 | Morning | 11:00 AM | 90 min before |
| Rosh Hashanah | Ethanim 1 | Eve | 6:00 PM | 90 min before |
| Rosh Hashanah | Ethanim 1 | Morning | 11:00 AM | 90 min before |
| Yom Kippur | Ethanim 10 | Eve | 6:00 PM | 90 min before |
| Yom Kippur | Ethanim 10 | Morning | 11:00 AM | 90 min before |
| Succoth | Ethanim 15 | Opening Eve | 6:00 PM | 90 min before |
| Succoth | Ethanim 15 | Opening Day | 11:00 AM | 90 min before |
| Succoth | Ethanim 22 | Closing Eve | 6:00 PM | 90 min before |
| Succoth | Ethanim 22 | Closing Day | 11:00 AM | 90 min before |
| Sabbath | Every Friday | Eve | 6:00 PM | 90 min before |
| Sabbath | Every Saturday | Morning | 11:00 AM | 90 min before |
| Sunday Morning Prayer | Every Sunday | — | 6:00 AM | 12 hours before |

> Biblical month names are used in announcements (Abib, Ethanim) while modern Hebrew
> month names (Nisan, Tishri) are used internally for calendar library compatibility.

---

## Bot Commands

### User Commands
| Command | Description |
|---|---|
| `/start` | Register with the bot and display available commands |
| `/help` | Show available commands |
| `/events` | List upcoming events in the next 30 days |
| `/exportcalendar` | Download an ICS file of all upcoming events |
| `/appointment` | Request a meeting with a church official |
| `/stop` | Unsubscribe from notifications |

### Admin-Only Commands
| Command | Description |
|---|---|
| `/addevent` | Add a one-time or recurring special event |
| `/modifyevent` | Modify an existing special event |
| `/deleteevent` | Delete a special event, or add an urgent announcement to a convocation |
| `/listevents` | View all events in the next 30 days with notification times |
| `/usercount` | Display the number of registered users |
| `/userlist` | List all registered users (PDF generated if > 100 users) |
| `/adminhelp` | Show the admin command list |

---

## Project Structure

```
christofgod_bot/
├── bot.py                  # Main bot — all handlers, scheduling, entry point
├── hebrew_calendar.py      # Hebrew-to-Gregorian date conversion and event generation
├── cache.py                # File-backed in-memory cache with 60-second hot-reload
├── activity_logger.py      # Human-readable activity log with 6-month retention
├── ics_generator.py        # ICS calendar file generation for events and appointments
├── pdf_generator.py        # PDF user list generation (for lists > 100 users)
├── requirements.txt        # Python dependencies
├── config/
│   ├── config.yaml         # Bot token, timezone, and path configuration
│   ├── admins.yaml         # Telegram usernames and/or phone numbers of administrators
│   └── officials.yaml      # Church officials available for appointment requests
├── data/
│   ├── events.yaml         # Special events and convocation announcements
│   ├── users.yaml          # Registered users (auto-managed)
│   └── appointments.yaml   # Appointment requests and their status (auto-managed)
├── logs/
│   └── bot_activity.log    # Activity log (auto-managed)
├── generated/              # Temporary ICS and PDF files
└── tests/                  # Unit test suite (149 tests)
```

---

## Setup

### Prerequisites
- Python 3.10 or later
- A Telegram bot token from [@BotFather](https://t.me/BotFather)

### Installation

```bash
git clone git@github.com:sjeaves2/christofgod_bot.git
cd christofgod_bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Configuration

**1. Copy the example config and set your bot token:**
```bash
cp config/config.yaml.example config/config.yaml
```
Then edit `config/config.yaml`:
```yaml
bot:
  token: "YOUR_BOT_TOKEN_HERE"
  timezone: "America/New_York"
```

**2. Add administrators** in `config/admins.yaml`. Each entry may use a Telegram username,
a phone number, or both:
```yaml
admins:
  - username: "your_telegram_username"
    display_name: "Your Name"

  - phone: "7575550000"       # digits only, include country code if outside US
    display_name: "Pastor Name"
```

**3. Add officials** available for appointment requests in `config/officials.yaml`:
```yaml
officials:
  - id: "pastor_example"
    name: "Pastor Jane Smith"
    phone: "7575550001"

  - id: "bishop_example"
    name: "Bishop John Doe"
    telegram_username: "bishopjohn"
```

> Officials must send `/start` to the bot at least once so the bot can capture
> their Telegram chat ID and forward appointment requests to them.

### Running

```bash
source .venv/bin/activate
python bot.py
```

The bot uses long polling — no web server or open port is required.

---

## Data Files

All data is stored as human-readable YAML and can be manually edited. The bot
detects file changes and reloads the cache within 60 seconds automatically.

| File | Purpose | Committed to git |
|---|---|---|
| `config/config.yaml` | Bot configuration (live token) | ❌ (gitignored) |
| `config/config.yaml.example` | Safe template for the repo | ✅ (token placeholder only) |
| `config/admins.yaml` | Admin user list | ✅ |
| `config/officials.yaml` | Officials list | ✅ |
| `data/events.yaml` | Special events and announcements | ✅ |
| `data/users.yaml` | Registered user records | ❌ (personal data) |
| `data/appointments.yaml` | Appointment requests | ❌ (personal data) |

---

## Running the Tests

```bash
source .venv/bin/activate
python -m pytest tests/ -v
```

The test suite covers Hebrew calendar date computation, event name formatting,
file caching, activity logging, ICS generation, PDF generation, and
admin/official phone and username matching (149 tests).

---

## Dependencies

| Package | Purpose |
|---|---|
| `python-telegram-bot` | Telegram Bot API client and job queue |
| `pyluach` | Hebrew calendar date conversion |
| `PyYAML` | YAML configuration and data file handling |
| `icalendar` | ICS calendar file generation |
| `reportlab` | PDF generation for large user lists |
| `pytz` | Timezone handling for service times |
