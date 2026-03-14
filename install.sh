#!/bin/bash
# Install LTE automation scripts for Intel XMM7560 / Fibocom L860R+
# Must be run as root.

set -e

if [ "$(id -u)" -ne 0 ]; then
    echo "Run as root (sudo $0)"
    exit 1
fi

echo "Installing scripts..."
cp scripts/wwan-fcc-unlock.py  /usr/local/bin/wwan-fcc-unlock.py
cp scripts/wwan-fcc-unlock.sh  /usr/local/bin/wwan-fcc-unlock.sh
cp scripts/wwan-sim-unlock.sh  /usr/local/bin/wwan-sim-unlock.sh
cp scripts/wwan-setup-ip.py    /usr/local/bin/wwan-setup-ip.py
chmod +x /usr/local/bin/wwan-fcc-unlock.sh /usr/local/bin/wwan-sim-unlock.sh

echo "Installing systemd units..."
cp systemd/wwan-fcc-unlock.service /etc/systemd/system/
cp systemd/wwan-sim-unlock.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable wwan-fcc-unlock.service wwan-sim-unlock.service

echo "Installing NetworkManager dispatcher..."
cp networkmanager/dispatcher.d/99-wwan-ip /etc/NetworkManager/dispatcher.d/
chmod +x /etc/NetworkManager/dispatcher.d/99-wwan-ip

echo ""
echo "Done. Next steps:"
echo "  1. Create a NetworkManager GSM connection named 'Orange' (or edit 99-wwan-ip to match your connection name)"
echo "  2. Store your SIM PIN:  nmcli connection modify <name> gsm.pin <PIN>"
echo "  3. Set connection.autoconnect to no if you want manual-only connections"
echo "  4. Reboot and connect from your desktop network applet"
