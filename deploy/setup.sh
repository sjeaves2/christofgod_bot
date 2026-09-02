#!/usr/bin/env bash
#
# First-time provisioning for the bot on a fresh Ubuntu server.
# Run ON THE SERVER, from inside the cloned repository:
#
#   cd ~/christofgod_bot && ./deploy/setup.sh
#
# Idempotent: safe to re-run. It never touches config/config.yaml or data/,
# so re-running cannot clobber live congregation data.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$REPO_DIR/.venv"
SERVICE_NAME="christofgod-bot"

cd "$REPO_DIR"

echo "==> Installing system packages"
sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-venv python3-pip git unattended-upgrades

# Automatic security patches. This machine sits on the internet unattended for
# months at a time, so unapplied kernel/OpenSSL fixes are the realistic risk.
# Idempotent: re-running simply confirms the existing configuration.
echo "==> Enabling automatic security updates"
sudo dpkg-reconfigure -f noninteractive unattended-upgrades

echo "==> Creating virtualenv (if absent)"
if [ ! -d "$VENV" ]; then
    python3 -m venv "$VENV"
fi

echo "==> Installing Python dependencies"
"$VENV/bin/pip" install --upgrade -q pip
"$VENV/bin/pip" install -q -r requirements.txt

echo "==> Checking configuration"
missing=0
if [ ! -f config/config.yaml ]; then
    echo "    MISSING config/config.yaml"
    echo "    Copy it from your laptop (deploy/migrate.sh does this), or:"
    echo "      cp config/config.yaml.example config/config.yaml"
    echo "      # then set bot.token"
    missing=1
fi
if [ ! -f config/admins.yaml ]; then
    echo "    MISSING config/admins.yaml"
    missing=1
fi
if [ "$missing" -eq 1 ]; then
    echo
    echo "Configuration incomplete — fix the above, then re-run this script."
    exit 1
fi

# A placeholder token means the example file was copied but never edited.
if grep -q "YOUR_BOT_TOKEN_HERE" config/config.yaml; then
    echo "    config/config.yaml still contains the placeholder token."
    echo "    Set bot.token to the real value from BotFather, then re-run."
    exit 1
fi

echo "==> Verifying the bot imports cleanly"
"$VENV/bin/python" -c "import bot" > /dev/null

echo "==> Installing the systemd service"
# Rendered from a template so the unit matches wherever this repo actually
# lives and whoever runs it — no editing needed for ~/christofgod_bot,
# ~/repos/christofgod_bot, /opt/christofgod_bot, or a non-"ubuntu" user.
RUN_USER="$(id -un)"
sed -e "s|__REPO_DIR__|${REPO_DIR}|g" -e "s|__RUN_USER__|${RUN_USER}|g" \
    "deploy/${SERVICE_NAME}.service.template" \
    | sudo tee "/etc/systemd/system/${SERVICE_NAME}.service" > /dev/null
echo "    service will run as ${RUN_USER} from ${REPO_DIR}"
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"

echo "==> Starting the bot"
sudo systemctl restart "$SERVICE_NAME"
sleep 3
sudo systemctl --no-pager --lines=15 status "$SERVICE_NAME" || true

cat <<EOF

Setup complete.

  Status : sudo systemctl status $SERVICE_NAME
  Logs   : journalctl -u $SERVICE_NAME -f
  Update : ./deploy/update.sh

Send /start to the bot in Telegram to confirm it is answering.
EOF
