#!/usr/bin/env bash
# Installs Claude meter as a systemd service on a Raspberry Pi (Raspberry Pi OS / Debian).
# Run from the repository folder:  ./install.sh
set -euo pipefail
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
MDNS_NAME="${LED_MDNS_NAME:-claude-meter}"
cd "$APP_DIR"

echo ">>> System packages (Bluetooth, mDNS)"
sudo apt-get update
sudo apt-get install -y python3-venv bluez avahi-daemon avahi-utils
sudo rfkill unblock bluetooth || true
sudo systemctl enable --now bluetooth avahi-daemon
sudo usermod -aG bluetooth "$USER"

echo ">>> Python environment"
python3 -m venv .venv
./.venv/bin/pip install --upgrade pip
./.venv/bin/pip install -r requirements.txt

echo ">>> Configuration"
[ -f .env ] || cp .env.example .env
chmod 600 .env

echo ">>> Services"
sudo tee /etc/systemd/system/claude-meter.service > /dev/null << UNIT
[Unit]
Description=Claude meter (LED usage display)
After=bluetooth.target network-online.target
Wants=network-online.target

[Service]
User=$USER
WorkingDirectory=$APP_DIR
Environment=PYTHONUNBUFFERED=1
Environment=LED_MDNS_NAME=$MDNS_NAME
ExecStart=$APP_DIR/.venv/bin/python $APP_DIR/claude_meter.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT

sudo tee /etc/systemd/system/claude-meter-mdns.service > /dev/null << UNIT
[Unit]
Description=Publish $MDNS_NAME.local on the local network
After=avahi-daemon.service network-online.target
Wants=network-online.target

[Service]
ExecStart=$APP_DIR/mdns-alias.sh $MDNS_NAME
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT

chmod +x mdns-alias.sh
sudo systemctl daemon-reload
sudo systemctl enable --now claude-meter claude-meter-mdns

PORT="$(grep -E '^LED_WEB_PORT=' .env | cut -d= -f2)"; PORT="${PORT:-8080}"
IP="$(hostname -I | awk '{print $1}')"
echo
echo "Done. Open http://$MDNS_NAME.local:$PORT  (or http://$IP:$PORT)"
echo "Then pick your display with 'Find displays' and choose how to sign in."
