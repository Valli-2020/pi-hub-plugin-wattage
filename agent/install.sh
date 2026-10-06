#!/bin/sh
# Install wattage-agent as a systemd service. Usage: sudo ./install.sh <bind-ip>
set -eu
BIND="${1:?usage: install.sh <bind-ip>}"
cd "$(dirname "$0")"
install -m 0755 wattage-agent.py /usr/local/bin/wattage-agent.py
sed "s/@BIND@/$BIND/" wattage-agent.service > /etc/systemd/system/wattage-agent.service
systemctl daemon-reload
systemctl enable --now wattage-agent.service
