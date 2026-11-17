#!/bin/bash
# Install (or update) the Desk Buddy bridge on Linux, as a systemd user service that starts at login:
#   deskbuddy-bridge.service   Python bridge: Claude Code hooks -> Lump over USB or Wi-Fi, and HoloPi
# Without the app watcher of macOS: Chat and Cowork are Mac apps; Claude Code's hooks are all there is.
# Runtime lives in ~/.desk-buddy:  venv/  bridge.py  holo_driver.py  bridge.log
# For the USB cable, your user must be in the dialout group: sudo usermod -aG dialout "$USER", then log in again.
# Uninstall: tools/install_bridge_linux.sh --uninstall
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RT="$HOME/.desk-buddy"
UNIT="$HOME/.config/systemd/user/deskbuddy-bridge.service"

if [ "${1:-}" = "--uninstall" ]; then
  systemctl --user disable --now deskbuddy-bridge.service 2>/dev/null || true
  rm -f "$UNIT"; systemctl --user daemon-reload
  echo "Bridge removed (runtime left in $RT)."
  exit 0
fi

mkdir -p "$RT" "$(dirname "$UNIT")"
[ -x "$RT/venv/bin/python" ] || python3 -m venv "$RT/venv"
"$RT/venv/bin/pip" -q install --disable-pip-version-check --upgrade pip
"$RT/venv/bin/pip" -q install --disable-pip-version-check pyserial icalendar recurring-ical-events
cp "$ROOT/tools/buddy_bridge.py" "$RT/bridge.py"
cp "$ROOT/tools/holo_driver.py" "$RT/holo_driver.py"   # Lump on a Looking Glass, when config.json has holo_url
# Lump's voice, and the sound effects of his tap gags.
python3 "$ROOT/tools/make_sounds.py"
python3 "$ROOT/tools/make_gag_sounds.py"

cat > "$UNIT" <<UNIT
[Unit]
Description=Desk Buddy bridge: Claude Code -> Lump
After=network-online.target

[Service]
ExecStart=$RT/venv/bin/python -u $RT/bridge.py
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
UNIT
systemctl --user daemon-reload
systemctl --user enable deskbuddy-bridge.service >/dev/null
systemctl --user restart deskbuddy-bridge.service

sleep 3
curl -s -m 2 http://127.0.0.1:47823/status >/dev/null && echo "Bridge running: http://127.0.0.1:47823/status" \
  || echo "The bridge didn't answer: journalctl --user -u deskbuddy-bridge -n 30"
