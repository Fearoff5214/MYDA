#!/data/data/com.termux/files/usr/bin/bash
# Jarvis Android setup, for Termux.
#
# Install Termux and Termux:API from F-Droid or GitHub -- NOT the Play Store.
# The Play Store builds are abandoned and their API bridge does not work.
#
#   curl -o setup.sh <your-repo-raw-url>/android/setup.sh && bash setup.sh
set -euo pipefail

echo "==> installing packages"
pkg update -y
pkg install -y python termux-api ffmpeg openssh

echo "==> installing python dependencies"
pip install --upgrade pip
pip install websockets

echo "==> requesting storage permission"
termux-setup-storage || true

echo "==> checking the Termux:API bridge"
if ! command -v termux-battery-status >/dev/null; then
  echo "termux-api package is missing."
  exit 1
fi
if ! termux-battery-status >/dev/null 2>&1; then
  cat <<'EOF'

The termux-api commands are installed but not responding.

That means the companion *app* (Termux:API) is missing or lacks permissions.
Install it from the same source as Termux itself (F-Droid or GitHub), open it
once, then grant SMS, phone, microphone and notification access under
Android Settings -> Apps -> Termux:API -> Permissions.

Notification reading also needs Termux:API enabled under
Settings -> Notifications -> Notification access.

EOF
  exit 1
fi

echo "==> keeping the connection alive in the background"
# Without these two, Android kills the client within minutes of screen-off.
termux-wake-lock || true
cat <<'EOF'

Exempt Termux from battery optimisation, or the client will be killed:
  Android Settings -> Apps -> Termux -> Battery -> Unrestricted

EOF

echo "==> writing the token"
if [ ! -f token ]; then
  read -r -p "Paste the token from scripts/new_device.py: " TOKEN
  printf '%s' "$TOKEN" > token
  chmod 600 token
fi

read -r -p "Hub address (e.g. ws://100.x.y.z:8080): " HUB
cat > run.sh <<RUNEOF
#!/data/data/com.termux/files/usr/bin/bash
# Background daemon: receives commands from the hub.
exec python "$PWD/client.py" --hub "$HUB" --device phone
RUNEOF

cat > ask.sh <<ASKEOF
#!/data/data/com.termux/files/usr/bin/bash
# One push-to-talk turn. Bind this to a Termux:Widget shortcut on the
# home screen -- that is the phone's equivalent of the PC hotkey.
exec python "$PWD/client.py" --hub "$HUB" --device phone --ask
ASKEOF

chmod +x run.sh ask.sh

# Termux:Widget reads scripts from here, giving you a home-screen button.
mkdir -p ~/.shortcuts
ln -sf "$PWD/ask.sh" ~/.shortcuts/jarvis-ask
ln -sf "$PWD/run.sh" ~/.shortcuts/jarvis-daemon

cat <<'EOF'

Done.

  ./run.sh    start the background daemon (phone actions)
  ./ask.sh    one voice turn

Install Termux:Widget to get "jarvis-ask" as a home-screen button.
To start the daemon automatically on boot, install Termux:Boot and put a
script in ~/.termux/boot/ that calls run.sh.
EOF
