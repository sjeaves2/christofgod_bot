# Deploying to a 24/7 server (AWS Lightsail)

The bot is a long-running process that polls Telegram and keeps its state in
YAML files on local disk. That shapes every choice below:

* **No inbound ports, no domain, no TLS.** It dials out to `api.telegram.org`;
  nothing connects *to* it. Leave the firewall closed except for SSH.
* **It must never sleep**, and its disk must persist across restarts — which is
  why a small VM suits it and most "free web app" tiers do not.
* It is tiny: ~52 MB of RAM at full import, ~36 KB of config and data.

Written for Lightsail, but only steps 1–2 are provider-specific; everything
after works on any Ubuntu host.

---

## 1. Create the instance

Lightsail console → **Create instance**:

| Setting | Value |
|---|---|
| Region | Any near you (irrelevant to polling; pick for console latency) |
| Platform | Linux/Unix |
| Blueprint | **OS Only → Ubuntu 22.04 LTS** (or newer LTS) |
| Plan | The **$5/month** tier (512 MB–1 GB RAM) is ample |
| Name | `christofgod-bot` |

Then, still in the console:

* **Networking → IPv4 Firewall** — keep SSH (22) only. Delete HTTP/HTTPS rules
  if present; the bot needs no inbound traffic.
* **Snapshots → Enable automatic snapshots.** This is your backup for `data/`
  until we add application-level backups. Do not skip it: `users.yaml` and
  `appointments.yaml` will exist only on this machine.
* Optionally attach a **static IP** so the address survives a stop/start.

Connect with the browser SSH client, or download the key and:

```bash
ssh -i ~/Downloads/LightsailKey.pem ubuntu@<your-ip>
```

## 2. Set the server clock to the church timezone (optional)

The bot converts times internally using `bot.timezone` from `config.yaml`, so
this only affects how *system* logs read. It makes `journalctl` easier to match
against the bot's own logs:

```bash
sudo timedatectl set-timezone America/New_York
```

## 3. Give the server read-only access to GitHub

Generate a key **on the server** and register it as a *deploy key*. The server
can then pull, but cannot push, and your personal SSH key never leaves your
laptop.

```bash
ssh-keygen -t ed25519 -C "christofgod-bot deploy key" -f ~/.ssh/id_ed25519 -N ""
cat ~/.ssh/id_ed25519.pub
```

Copy that public key, then in GitHub: **repo → Settings → Deploy keys → Add
deploy key**. Paste it, name it `lightsail`, and leave *Allow write access*
**unchecked**. Verify:

```bash
ssh -T git@github.com    # "Hi sjeaves2/christofgod_bot! You've successfully authenticated"
```

## 4. Clone and provision

```bash
git clone git@github.com:sjeaves2/christofgod_bot.git ~/christofgod_bot
cd ~/christofgod_bot
```

`config/config.yaml` and `data/` are gitignored, so copy them from your laptop.
**On your laptop**, from the repository root:

```bash
./deploy/migrate.sh ubuntu@<your-ip>
```

That copies `config/*.yaml` and `data/`, and deliberately **skips `logs/`** —
see "Before you migrate" below. Then back **on the server**:

```bash
cd ~/christofgod_bot && ./deploy/setup.sh
```

`setup.sh` installs Python and dependencies, refuses to continue if the config
is missing or still holds the placeholder token, verifies the bot imports, then
installs and starts the systemd service.

## 5. Confirm it works

```bash
sudo systemctl status christofgod-bot     # should say active (running)
journalctl -u christofgod-bot -f          # live log
```

In Telegram, send `/start`, then `/stats` as an admin. Reboot once
(`sudo reboot`) and confirm it comes back on its own.

**Stop the copy on your laptop.** Two instances polling the same token produce
Telegram `Conflict` errors; the bot now alerts ops admins about those on the
first occurrence.

---

## Updating

```bash
cd ~/christofgod_bot && ./deploy/update.sh          # latest main
cd ~/christofgod_bot && ./deploy/update.sh v0.9.0   # a specific release
```

`update.sh` refuses to run with local modifications, verifies the new code
imports **before** restarting, and rolls back if it does not — so a bad pull
leaves the running bot alone instead of taking the congregation's bot offline.

## Before you migrate — two housekeeping items

1. **Do not copy `logs/bot.log`.** It has historically contained the live bot
   token (98 occurrences as of 2026-08-24). Redaction now prevents new leaks,
   but old lines are not rewritten. `migrate.sh` skips `logs/` entirely. If that
   file has ever been shared, rotate the token via BotFather.
2. **Leave `logs/bot_activity.log` behind.** Years of test runs wrote real
   COMMAND entries into it, which is why `/stats` reported ~10,600 commands for
   a twelve-member congregation. Starting empty makes `/stats` meaningful from
   day one. (Test isolation now prevents this; the old data cannot be cleaned
   reliably.)

## Operating notes

* **Logs.** `logs/bot.log` (runtime, pruned at 45 days), `logs/bot_activity.log`
  (commands/notifications, 90 days), `logs/errors.log` (full tracebacks, cleared
  at each restart). systemd also keeps a copy in journald.
* **Errors.** Unhandled exceptions DM admins with `ops: true`, carrying an
  `ERR-XXXXXX` id that matches an entry in `logs/errors.log`.
* **Backups.** Lightsail snapshots cover the whole disk. `data/` is what matters
  — it is the only copy of registrations and appointments.
* **Resources.** If you ever want to check: `free -h`, `df -h`,
  `systemctl status christofgod-bot`.

## Troubleshooting

| Symptom | Check |
|---|---|
| Service won't start | `journalctl -u christofgod-bot -n 50` — usually a config or dependency problem |
| Bot silent, service "running" | Look for `Conflict` in the log: another instance is polling the same token |
| `git pull` asks for a password | The deploy key is missing or wrong; re-run `ssh -T git@github.com` |
| Restart loop | systemd gives up after 5 tries in 5 minutes; `systemctl status` shows the reason |
