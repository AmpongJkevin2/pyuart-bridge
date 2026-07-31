#!/data/data/com.termux/files/usr/bin/bash
# bridge.sh — wrapper so termux-usb -e can call "python main.py"
# Usage: termux-usb -r -e ./bridge.sh /dev/bus/usb/002/003
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec python "$SCRIPT_DIR/main.py" "$@"
