# Privacy Policy — Christ of God bot

**Last updated: 16 September 2026**

This policy explains what [@christofgod_bot](https://t.me/christofgod_bot)
stores about you, why, who can see it, and how to have it removed. The bot is
operated by **Christ of God Ministries** for the use of its congregation.

The bot is open source. Everything described here can be verified in the code
at <https://github.com/sjeaves2/christofgod_bot>.

---

## What the bot stores

### When you send /start

| Data | Why |
|---|---|
| Your Telegram numeric ID | To send you reminders and replies |
| Your Telegram @username | So officials and administrators can recognise you |
| Your display name | Shown to officials when you request a meeting |
| The date you joined | Administration |

### When you choose settings

| Data | Why |
|---|---|
| Your time zone (`/settimezone`) | So service times are shown in your local time |
| Your language (`/language`) | So the bot replies in English, Spanish, French or isiZulu |
| Your reminder preferences (`/notifications`) | So you only receive the reminders you asked for |

### Your phone number — only if you choose to share it

The bot may invite you to share your contact. This is used **solely** to
recognise administrators and church officials by phone number, for people whose
Telegram account has no @username.

**Sharing your contact is optional.** If you do not share it, every part of the
bot still works for you as an ordinary member.

### When you request a meeting (`/appointment`)

The date and time you asked for and that was confirmed, the official concerned,
the meeting length, the status, and **the reason you type for the meeting**.

Please be aware that the reason you type is stored on the server and is visible
to the official you are meeting and to administrators. Share only what you are
comfortable recording. For anything you would rather not have written down,
speak to an official directly.

### When you send a prayer request (`/prayer`)

What you write, your name, and the date you sent it.

A prayer request is shared **only with the members of the ministry's leadership
designated to receive them** — not with all administrators, and with nobody
outside the leadership.

**What you wrote is deleted as soon as your request has been answered or
closed.** Only a short record remains — the reference number, the date, your
name and the fact that it was answered — and that record is deleted after 30
days. The ministry does not keep a lasting copy of what you shared.

Please share what you are comfortable sharing. For anything you would rather
not have written down at all, speak to an official directly.

### Activity records

The bot keeps a log of which commands were used, by whom and when, to
troubleshoot problems and to produce usage summaries for administrators. It
records the command name, your Telegram ID, your @username and your display
name. **It does not record the content of your messages.**

---

## What the bot does NOT do

* It does **not** read ordinary conversation in group chats. Telegram "privacy
  mode" is enabled, so in a group the bot only receives commands addressed to
  it.
* It does **not** process payments. `/donate` is a link to an external page;
  no payment details ever pass through the bot.
* It does **not** sell, rent or share your information with anyone for
  marketing, ever.
* It does **not** use your information for advertising or profiling.

---

## Who can see your information

| Who | What they can see |
|---|---|
| Church administrators | The list of registered members, and all appointments |
| Church officials | Appointments that involve them, including the reason you gave |
| Designated prayer leaders | Prayer requests sent with /prayer |
| Designated operations administrators | A nightly backup copy of the bot's data files, sent to them by direct message |
| Nobody else | — |

Administrators and officials are members of the ministry's leadership. The bot
grants no access to anyone outside it.

---

## How long it is kept

| Data | Kept for |
|---|---|
| Your registration and settings | Until you send `/stop` |
| Appointments | Moved to an archive 90 days after the meeting |
| Archived appointments | Deleted after 2 years |
| Announcements | Deleted 30 days after they expire |
| Prayer requests | The text is deleted as soon as the request is answered or closed; the short record 30 days later |
| Activity records | 90 days, then automatically deleted |
| Technical logs | 45 days, then automatically deleted |

---

## Removing your information

Send **`/stop`** to the bot. Your registration record — your ID, username,
display name, phone number if you shared one, and your settings — is deleted
immediately, and the bot stops sending you anything.

Two honest caveats:

* The activity log will still show that a person with your ID used the bot,
  until those entries age out after 90 days.
* Appointments you have already made remain, because they are also the
  official's record of a meeting. Ask an administrator if you would like one
  removed sooner.

---

## Where it is stored

On a private server rented from Amazon Web Services, located in the United
States, accessible only to the ministry's operations administrators. Data is
held in files on that server; a nightly backup is sent by Telegram direct
message to operations administrators.

---

## Other services involved

* **Telegram** — the platform itself. Your use of the bot is also governed by
  [Telegram's Privacy Policy](https://telegram.org/privacy).
* **Google Translate** — announcement text may be machine-translated for
  members who have chosen another language. Only the announcement text written
  by an administrator is sent; **nothing personal is ever sent to a translation
  service.**
* **Zoom** — service join links point to Zoom. The bot only stores and shares
  the link; it sends no information about you to Zoom.
* **PayPal** — `/donate` links to an external giving page. The bot neither sees
  nor stores anything about a donation.

---

## Children

The bot is intended for members of the congregation. It is not directed at
children under 13, and it asks for no information beyond what is described
above.

---

## Changes to this policy

Changes will be published on this page, and the date at the top updated. The
full history of changes is public in the repository.

---

## Contact

For any question about your information, or to ask for it to be removed, speak
to a church administrator, or write to the ministry through the contact details
published by Christ of God Ministries.
