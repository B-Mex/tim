#!/bin/bash
# Startet die Zentrale (Tim) auf Loopback plus - falls Tailscale laeuft -
# der Tailscale-Adresse. Bewusst nicht 0.0.0.0: sonst haengt Tim auch im
# heimischen WLAN. Die IP wird bei jedem Start frisch ermittelt, damit ein
# Wechsel der Tailscale-Adresse den Autostart nicht bricht.
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

TS="/Applications/Tailscale.app/Contents/MacOS/Tailscale"
[ -x "$TS" ] || TS="$(command -v tailscale)"
TS_IP="$("$TS" ip -4 2>/dev/null | head -1)"

if [ -n "$TS_IP" ]; then
    export M1_ZENTRALE_HOST="127.0.0.1,$TS_IP"
else
    export M1_ZENTRALE_HOST="127.0.0.1"
fi

exec /opt/ki-server/venv/bin/python /opt/ki-server/oberflaeche/m1_zentrale.py
