#!/data/data/com.termux/files/usr/bin/bash
# Termux:Boot script — runs automatically every time the MK15 powers on.
# Always fetches the LATEST gimbal_control.py before running it, so code
# changes made on the PC take effect on next boot/restart with zero manual
# action on this device. If the script ever crashes, this loop re-fetches
# and restarts it automatically (crash-resistant, like a systemd service).

termux-wake-lock 2>/dev/null   # stop Android from killing this for power saving

cd ~ || exit 1

while true; do
  # Download to a temp file first — if this fails (no network yet, etc.),
  # keep running the last known-good copy instead of crashing on a partial file.
  if curl -sf -o gimbal_control.py.new https://livestreamxk.com/gimbal_control.py; then
    mv gimbal_control.py.new gimbal_control.py
  fi
  python gimbal_control.py
  sleep 3
done
