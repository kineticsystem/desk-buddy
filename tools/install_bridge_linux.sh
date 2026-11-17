#!/bin/bash
# Install (or update) the Desk Buddy bridge on Linux, as a sandboxed system service that runs as you:
#   deskbuddy-bridge.service   Python bridge: Claude Code hooks -> Lump over USB or Wi-Fi, and HoloPi
# Without the app watcher of macOS: Chat and Cowork are Mac apps; Claude Code's hooks are all there is.
#
# The sandbox: the bridge sees, of your home, only ~/.desk-buddy, and PipeWire's socket for his voice; it
# cannot write anywhere else, gain privileges or load kernel modules, and of the devices only the board's
# USB serial port, /dev/ttyACM*, through the dialout group, which it gets without you joining it. A system
# service, not a user one: a user service's sandbox needs unprivileged user namespaces, which Ubuntu
# restricts, and systemd then skips it without a word. After installing, a probe runs in the same sandbox
# and shows what it can and cannot reach.
#
# Runtime lives in ~/.desk-buddy:  venv/  bridge.py  holo_driver.py  bridge.log
# Needs sudo, once, for /etc/systemd/system. Uninstall: tools/install_bridge_linux.sh --uninstall
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RT="$HOME/.desk-buddy"
NAME="deskbuddy-bridge"
UNIT="/etc/systemd/system/$NAME.service"
RUNTIME_DIR="/run/user/$(id -u)"

# The sandbox, shared by the service and the probe.
SANDBOX=(
  "User=$(id -un)" "Group=$(id -gn)" "SupplementaryGroups=dialout"
  "Environment=HOME=$HOME" "Environment=XDG_RUNTIME_DIR=$RUNTIME_DIR"
  "UMask=0077"
  "NoNewPrivileges=yes" "CapabilityBoundingSet=" "AmbientCapabilities="
  "ProtectSystem=strict" "ProtectHome=tmpfs" "BindPaths=$RT" "PrivateTmp=yes"
  "BindReadOnlyPaths=-$RUNTIME_DIR/pipewire-0" "BindReadOnlyPaths=-$RUNTIME_DIR/pulse"
  "DevicePolicy=closed" "DeviceAllow=char-ttyACM rw"
  "ProtectKernelTunables=yes" "ProtectKernelModules=yes" "ProtectKernelLogs=yes" "ProtectControlGroups=yes"
  "ProtectClock=yes" "ProtectHostname=yes" "ProtectProc=invisible"
  "RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX" "RestrictNamespaces=yes" "RestrictSUIDSGID=yes"
  "RestrictRealtime=yes" "LockPersonality=yes" "MemoryDenyWriteExecute=yes"
  "SystemCallArchitectures=native" "SystemCallFilter=@system-service" "SystemCallFilter=~@privileged @resources"
)

if [ "${1:-}" = "--uninstall" ]; then
  sudo systemctl disable --now "$NAME.service" 2>/dev/null || true
  sudo rm -f "$UNIT"; sudo systemctl daemon-reload
  echo "Bridge removed (runtime left in $RT)."
  exit 0
fi

mkdir -p "$RT"
chmod 700 "$RT"                                    # config.json holds tokens and your calendar's private link
[ -x "$RT/venv/bin/python" ] || python3 -m venv "$RT/venv"
# Exact versions, so that a new release of one of them never runs here before we choose it.
"$RT/venv/bin/pip" -q install --disable-pip-version-check pyserial==3.5 icalendar==7.3.0 recurring-ical-events==3.8.2
cp "$ROOT/tools/buddy_bridge.py" "$RT/bridge.py"
cp "$ROOT/tools/holo_driver.py" "$RT/holo_driver.py"   # Lump on a Looking Glass, when config.json has holo_url
cp "$ROOT/tools/sandbox_probe.py" "$RT/sandbox_probe.py"
# Lump's voice, and the sound effects of his tap gags.
python3 "$ROOT/tools/make_sounds.py"
python3 "$ROOT/tools/make_gag_sounds.py"

{
  echo "[Unit]"
  echo "Description=Desk Buddy bridge: Claude Code -> Lump"
  echo "After=network-online.target"
  echo "Wants=network-online.target"
  echo
  echo "[Service]"
  echo "ExecStart=$RT/venv/bin/python -u $RT/bridge.py"
  echo "Restart=always"
  echo "RestartSec=3"
  printf '%s\n' "${SANDBOX[@]}"
  echo
  echo "[Install]"
  echo "WantedBy=multi-user.target"
} | sudo tee "$UNIT" >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable "$NAME.service" >/dev/null
sudo systemctl restart "$NAME.service"

echo "What the bridge can reach, from inside its sandbox:"
PROPERTIES=()
for p in "${SANDBOX[@]}"; do PROPERTIES+=(-p "$p"); done
sudo systemd-run --quiet --wait --pipe "${PROPERTIES[@]}" "$RT/venv/bin/python" "$RT/sandbox_probe.py" || true

sleep 2
curl -s -m 2 http://127.0.0.1:47823/status >/dev/null && echo "Bridge running: http://127.0.0.1:47823/status" \
  || echo "The bridge didn't answer: journalctl -u $NAME -n 30"
