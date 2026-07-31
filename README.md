# pyuart-bridge

A **USB-serial ↔ TCP bridge** that runs entirely inside Termux on Android.
It replaces the TCPUART app for `mpremote` and `esptool` workflows.

## Why this exists

Android does not expose `/dev/ttyUSBx` nodes.  Termux's `termux-usb` utility
hands you a raw file descriptor that `libusb` can wrap.  This script
implements the CP210x / CH340 / FTDI serial protocol in Python on top of that
fd, then presents the device as a TCP server socket that both `esptool` and
`mpremote` can reach via `socket://localhost:PORT`.

### Key fixes over a naive bridge

| Problem | Fix |
|---|---|
| REPL hangs on Ctrl-C | `TCP_NODELAY` on every accepted socket |
| Keystrokes arrive in batches | Unbuffered byte-level reads (≤ 512 B per loop) |
| Bridge times out after 5 min | No arbitrary timeout — runs until Ctrl-C |
| Android kills bridge in background | Run in foreground with `termux-usb -e` |

---

## Requirements — install once

```sh
pkg update
pkg install termux-api libusb python
pip install pyusb
```

> **Termux:API** must also be installed from F-Droid or Google Play, and
> granted the *USB* permission when prompted.

---

## Quickstart

### 1 — Find your device

```sh
termux-usb -l
# example output: ["/dev/bus/usb/001/002"]
```

### 2 — Start the bridge (replaces TCPUART)

```sh
termux-usb -r -e python main.py /dev/bus/usb/001/002
```

Termux will pop up an Android USB permission dialog — tap **Allow**.
The bridge then starts listening:

```
15:30:00 [INFO] Listening on 127.0.0.1:7777  (Ctrl-C to stop)
15:30:00 [INFO] Connect with:  mpremote connect socket://localhost:7777
15:30:00 [INFO]            or: esptool.py --port socket://localhost:7777 flash_id
```

### 3 — Connect from another Termux session (or proot-distro Debian)

```sh
# interactive REPL
mpremote connect socket://localhost:7777 repl

# file copy
mpremote connect socket://localhost:7777 cp myfile.py :

# chip detection
esptool.py --port socket://localhost:7777 flash_id

# flash firmware
esptool.py --port socket://localhost:7777 --baud 460800 write_flash 0x0 firmware.bin
```

---

## Configuration (optional)

All options are set via environment variables **before** the `termux-usb` command:

| Variable | Default | Description |
|---|---|---|
| `BRIDGE_PORT` | `7777` | TCP listen port |
| `BRIDGE_HOST` | `127.0.0.1` | Bind address (use `0.0.0.0` to expose to LAN) |
| `BRIDGE_BAUD` | `115200` | Serial baud rate |
| `BRIDGE_TIMEOUT_MS` | `20` | USB read timeout in milliseconds |
| `BRIDGE_DRIVER` | auto | Force driver: `cp210x`, `ch34x`, or `ftdi` |

Example — 921600 baud, custom port:

```sh
BRIDGE_BAUD=921600 BRIDGE_PORT=8888 \
  termux-usb -r -e python main.py /dev/bus/usb/001/002
```

---

## Supported chips

| Chip | VID:PID | Notes |
|---|---|---|
| CP2102 / CP2104 | `10C4:EA60` | Most ESP32 dev boards |
| CP2102N | `10C4:EA70` | newer Espressif boards |
| CH340 | `1A86:7523` | cheap Arduino clones |
| CH341A | `1A86:5523` | programmer dongles |
| FT232R | `0403:6001` | FTDI USB-TTL |
| FT231X | `0403:6015` | FTDI low-pin-count |

If your device is not in the list the bridge defaults to the CP210x driver and logs a warning.
You can override with `BRIDGE_DRIVER=ch34x` or `BRIDGE_DRIVER=ftdi`.

---

## Troubleshooting

### Permission denied / no dialog appears
Make sure **Termux:API** is installed and that you used `-r` (request permission):
```sh
termux-usb -r -e python main.py /dev/bus/usb/001/002
#             ^^
```

### `libusb not found` error
```sh
pkg install libusb
```

### REPL still hangs after connecting
* Check that you see `TCP_NODELAY` set in the log — if not, you may be
  connecting via a wrapper that adds its own buffering.
* Try reducing `BRIDGE_TIMEOUT_MS` to `5` for lower latency.
* Confirm the board's UART baud matches `BRIDGE_BAUD`.

### esptool sync errors
esptool does a hard reset by toggling DTR/RTS.  The bridge asserts both
at startup; if your board has no auto-reset circuit you need to manually
press BOOT+RESET before running esptool.

---

## Next steps

If the TCP_NODELAY fix alone isn't enough, the plan is:

1. **Termux native path** — attempt `ptyserial` / `hutorny/usbuart` to expose
   a real `/dev/pts/N` node so mpremote sees a proper tty.
2. **Android app** — rebuild TCPUART as a proper Android foreground service
   targeting API 35 (Android 15) with DFU support for ESP32 / Arduino R4.

See `Building TCPUART app with DFU support for Android 15.md` for the full
app build specification.
# pyuart-bridge
