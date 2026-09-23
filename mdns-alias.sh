#!/bin/sh
# Publishes <name>.local -> this device's LAN IP through avahi, so the panel
# is reachable without knowing the IP. The device keeps its own hostname.
# ponytail: IP read once at start; if DHCP changes it, the unit restart (or a reboot) picks it up.
NAME="${1:-claude-meter}"
while :; do
  IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
  [ -n "$IP" ] && break
  sleep 2
done
exec avahi-publish -a -R "$NAME.local" "$IP"
