#!/data/data/com.termux/files/usr/bin/bash
# Jarvis Android setup, for Termux.
#
# Before running this you need FOUR apps, all from F-Droid or GitHub --
# NOT the Play Store, whose builds are abandoned and whose API bridge is
# broken:
#
#   Termux           the terminal this runs in
#   Termux:API       the bridge to SMS / mic / notifications   (required)
#   Termux:Widget    home-screen button for push-to-talk       (recommended)
#   Termux:Boot      start the daemon on reboot                (optional)
#
# Then, in Termux:
#   pkg install git
#   git clone <your-repo> jarvis && cd jarvis/android
#   bash setup.sh
#
# This script checks the things that actually break rather than assuming
# them: it records two seconds of real audio, and it talks to your hub.
set -uo pipefail

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
ok()   { printf '  [ok]   %s\n' "$*"; }
bad()  { printf '  [FAIL] %s\n' "$*"; }
warn() { printf '  [warn] %s\n' "$*"; }

FAILED=0
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

# ---------------------------------------------------------------------------
say "installing packages"
pkg update -y >/dev/null 2>&1 || warn "pkg update had problems; continuing"
pkg install -y python termux-api ffmpeg >/dev/null 2>&1
command -v python  >/dev/null && ok "python $(python -V 2>&1 | cut -d' ' -f2)" || { bad "python missing"; FAILED=1; }
command -v ffmpeg  >/dev/null && ok "ffmpeg"  || { bad "ffmpeg missing";  FAILED=1; }
command -v ffplay  >/dev/null && ok "ffplay"  || { bad "ffplay missing (comes with ffmpeg)"; FAILED=1; }

# websockets is the ONLY python dependency. Pure python, so no compiler and no
# native wheels -- which is exactly why this client is viable on Termux.
pip install --quiet --upgrade websockets 2>/dev/null \
  && ok "websockets" || { bad "pip install websockets failed"; FAILED=1; }

# ---------------------------------------------------------------------------
say "storage permission"
termux-setup-storage 2>/dev/null || true
ok "requested (approve the dialog if one appeared)"

# ---------------------------------------------------------------------------
say "checking the Termux:API bridge"
if ! command -v termux-battery-status >/dev/null; then
  bad "termux-api package missing"
  FAILED=1
elif ! timeout 20 termux-battery-status >/dev/null 2>&1; then
  bad "termux-api is installed but not responding"
  cat <<'EOF'

         The CLI shim is installed; the companion APP is what does the work.
         Install "Termux:API" from the SAME source as Termux (F-Droid or
         GitHub -- mixing sources fails on signature mismatch), open it once,
         then grant permissions under:
           Settings -> Apps -> Termux:API -> Permissions
         Enable at least Microphone, SMS and Phone.
EOF
  FAILED=1
else
  ok "bridge responding ($(timeout 20 termux-battery-status 2>/dev/null \
        | tr -d ' \n' | sed 's/.*"percentage":\([0-9]*\).*/\1%/') battery)"
fi

# ---------------------------------------------------------------------------
# The highest-risk piece by far. Android 14+ restricts microphone access, and
# the mic cannot be shared -- so prove it works now rather than discovering it
# the first time you press the button.
say "recording two seconds of real audio (the part most likely to fail)"
TMP="${TMPDIR:-/data/data/com.termux/files/usr/tmp}"
CLIP="$TMP/jarvis_miccheck.m4a"
rm -f "$CLIP"
if timeout 30 termux-microphone-record -f "$CLIP" -l 2 -r 16000 -c 1 >/dev/null 2>&1; then
  sleep 3
  timeout 10 termux-microphone-record -q >/dev/null 2>&1 || true
  if [ -s "$CLIP" ]; then
    SIZE=$(wc -c < "$CLIP" | tr -d ' ')
    ok "captured ${SIZE} bytes"
    if ffmpeg -y -loglevel error -i "$CLIP" -f s16le -ar 16000 -ac 1 \
         "$TMP/jarvis_miccheck.pcm" 2>/dev/null; then
      ok "ffmpeg transcodes it to 16kHz mono PCM"
    else
      bad "ffmpeg could not transcode the recording"; FAILED=1
    fi
  else
    bad "recording produced an empty file -- microphone permission, or"
    warn "another app is holding the mic. Close anything recording and retry."
    FAILED=1
  fi
else
  bad "termux-microphone-record failed outright"
  warn "grant Termux:API the Microphone permission, then rerun this script"
  FAILED=1
fi
rm -f "$CLIP" "$TMP/jarvis_miccheck.pcm"

# ---------------------------------------------------------------------------
say "optional permissions"
timeout 15 termux-notification-list >/dev/null 2>&1 \
  && ok "notification reading works" \
  || warn "notification reading unavailable -- enable Termux:API under
         Settings -> Notifications -> Notification access"
timeout 15 termux-sms-list -l 1 >/dev/null 2>&1 \
  && ok "SMS reading works" \
  || warn "SMS unavailable -- grant Termux:API the SMS permission"

# ---------------------------------------------------------------------------
say "hub address and token"
if [ -f token ]; then
  ok "token already present"
else
  echo "  On the hub, run:  python scripts/new_device.py phone --alias 'my phone'"
  read -r -p "  Paste the token it printed: " TOKEN
  [ -n "$TOKEN" ] || { bad "no token given"; exit 1; }
  printf '%s' "$TOKEN" > token
  chmod 600 token
  ok "saved to android/token"
fi

echo
echo "  The hub must be reachable from the phone. On the same Wi-Fi its LAN"
echo "  address works; anywhere else you need Tailscale on both. Get it with"
echo "  'tailscale ip -4' on the hub."
read -r -p "  Hub address [ws://100.x.y.z:8080]: " HUB
HUB="${HUB:-ws://100.x.y.z:8080}"

# Reachability check before writing anything -- a wrong address here is the
# second most common way this ends up mysteriously not working.
HOSTPORT="${HUB#ws://}"; HOSTPORT="${HOSTPORT#wss://}"; HOSTPORT="${HOSTPORT%%/*}"
HOST="${HOSTPORT%%:*}"; PORT="${HOSTPORT##*:}"; [ "$PORT" = "$HOST" ] && PORT=8080
if timeout 8 python - "$HOST" "$PORT" <<'PY' 2>/dev/null
import socket, sys
s = socket.socket(); s.settimeout(6)
sys.exit(0 if s.connect_ex((sys.argv[1], int(sys.argv[2]))) == 0 else 1)
PY
then
  ok "$HOST:$PORT is reachable"
else
  warn "cannot reach $HOST:$PORT from this phone. Check the hub is running,"
  warn "that server.host in hub/config.yaml is its tailnet IP and not"
  warn "127.0.0.1, and that Tailscale is up on both ends."
fi

# ---------------------------------------------------------------------------
say "writing launchers"
cat > run.sh <<RUNEOF
#!/data/data/com.termux/files/usr/bin/bash
# Background daemon: holds a socket open so the hub can reach the phone
# (texts, alarms, notifications, and announcing timers set elsewhere).
termux-wake-lock
exec python "$HERE/client.py" --hub "$HUB" --device phone
RUNEOF

cat > ask.sh <<ASKEOF
#!/data/data/com.termux/files/usr/bin/bash
# One push-to-talk turn. Bind this to a Termux:Widget shortcut -- it is the
# phone's equivalent of the hotkey on a PC. There is no wake word here:
# openWakeWord needs onnxruntime, which has no Termux wheel.
exec python "$HERE/client.py" --hub "$HUB" --device phone --ask
ASKEOF

chmod +x run.sh ask.sh
ok "run.sh and ask.sh"

# Termux:Widget reads scripts from ~/.shortcuts, giving a home-screen button.
mkdir -p ~/.shortcuts
ln -sf "$HERE/ask.sh" ~/.shortcuts/jarvis-ask
ln -sf "$HERE/run.sh" ~/.shortcuts/jarvis-daemon
ok "~/.shortcuts entries for Termux:Widget"

read -r -p "  Start the daemon automatically on boot? [y/N] " BOOT
case "${BOOT:-n}" in
  [yY]*)
    mkdir -p ~/.termux/boot
    printf '#!/data/data/com.termux/files/usr/bin/bash\ntermux-wake-lock\nexec %s/run.sh\n' \
      "$HERE" > ~/.termux/boot/jarvis
    chmod +x ~/.termux/boot/jarvis
    ok "~/.termux/boot/jarvis (needs the Termux:Boot app installed)"
    ;;
  *) ok "skipped" ;;
esac

# ---------------------------------------------------------------------------
cat <<'EOF'

==> One thing left, and it is not optional

    Android will kill the daemon within minutes of the screen going off
    unless you exempt Termux:
      Settings -> Apps -> Termux -> Battery -> Unrestricted
    Some phones (Xiaomi, Samsung, Oppo) hide this under "Autostart" or
    "Battery saver" as well. If the phone stops responding to the hub after
    a while, this is why.

==> Using it

    ./ask.sh     one voice turn        (bind to a Termux:Widget button)
    ./run.sh     background daemon     (phone actions from the hub)

    Then from any other device try:  "what's the battery on my phone"
EOF

if [ "$FAILED" -ne 0 ]; then
  printf '\n\033[1mSome checks failed above. Fix those first -- the client will\n'
  printf 'not work properly until they pass.\033[0m\n'
  exit 1
fi
printf '\n\033[1mAll checks passed.\033[0m\n'
