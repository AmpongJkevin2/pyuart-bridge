#!/usr/bin/env python3
"""
pyuart-bridge — USB-serial ↔ TCP bridge for Termux on Android
=============================================================
Bridges a USB-UART device (CP210x, CH340, FTDI, …) to a TCP server socket so
that tools like esptool and mpremote can reach an ESP32/Arduino over the
network socket interface (socket://localhost:PORT).

How it works
------------
Termux cannot expose /dev/ttyUSBx because Android's kernel does not bind
USB-serial drivers.  Instead, termux-usb hands us a raw file descriptor that
libusb can wrap via libusb_wrap_sys_device().  We implement the serial
protocol (baud rate, line control) in Python against that fd, then pipe bytes
between the USB bulk endpoints and any TCP client that connects.

Key fixes vs. a naive bridge
-----------------------------
* TCP_NODELAY on every accepted socket — kills Nagle coalescing so Ctrl-C
  and single-character keystrokes reach the board immediately.
* Unbuffered byte-level reads in both directions — no readline(), no fixed
  chunk accumulation.  Both loops use small reads (≤512 bytes) so the
  bridge never sits on a partial packet.
* Non-blocking USB read with short timeout — the USB→TCP loop checks for
  incoming TCP data to forward without blocking on an idle USB endpoint.
* Graceful teardown — disconnect on either side closes the other cleanly,
  letting mpremote or esptool reconnect without restarting the bridge.

Usage (run inside termux-usb -e)
---------------------------------
    # 1. list devices
    termux-usb -l

    # 2. request permission AND exec bridge
    termux-usb -r -e python main.py /dev/bus/usb/001/002

    # 3. connect from another Termux session / proot-distro
    mpremote connect socket://localhost:7777
    esptool.py --port socket://localhost:7777 flash_id

Options (env vars, all optional)
---------------------------------
    BRIDGE_PORT   TCP listen port          (default: 7777)
    BRIDGE_BAUD   Serial baud rate         (default: 115200)
    BRIDGE_HOST   TCP bind address         (default: 127.0.0.1)
    BRIDGE_TIMEOUT_MS  USB read timeout ms (default: 20)
"""

from __future__ import annotations

import ctypes
import logging
import os
import select
import signal
import socket
import sys
import threading
import time
from typing import Optional

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.DEBUG if os.environ.get("BRIDGE_DEBUG") else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("pyuart_bridge")

# ---------------------------------------------------------------------------
# Config from environment
# ---------------------------------------------------------------------------
BRIDGE_PORT = int(os.environ.get("BRIDGE_PORT", 7777))
BRIDGE_HOST = os.environ.get("BRIDGE_HOST", "127.0.0.1")
BRIDGE_BAUD = int(os.environ.get("BRIDGE_BAUD", 115200))
USB_TIMEOUT_MS = int(os.environ.get("BRIDGE_TIMEOUT_MS", 20))

# ---------------------------------------------------------------------------
# libusb helpers — pure ctypes, no pyusb
# ---------------------------------------------------------------------------
# pyusb's get_backend() internally calls libusb_init() via _LibUSBContext,
# which fails on Android/Termux because enumerating /dev/bus/usb requires root.
# pyusb swallows that exception and returns None.
#
# Solution: bypass pyusb entirely.  We load libusb-1.0.so directly with
# ctypes and call only the 11 functions we actually need.  libusb_wrap_sys_device()
# with ctx=NULL skips all device enumeration — it just wraps the fd that
# termux-usb already opened for us.

import ctypes
import ctypes.util
import glob

# Ensure $PREFIX/lib is in LD_LIBRARY_PATH so libusb's own transitive deps
# (other Termux packages) can be resolved by dlopen().
_TERMUX_PREFIX = os.environ.get("PREFIX", "/data/data/com.termux/files/usr")
_TERMUX_LIB    = f"{_TERMUX_PREFIX}/lib"
if os.path.isdir(_TERMUX_LIB):
    _ld = os.environ.get("LD_LIBRARY_PATH", "")
    if _TERMUX_LIB not in _ld.split(":"):
        os.environ["LD_LIBRARY_PATH"] = f"{_TERMUX_LIB}:{_ld}".strip(":")


def _find_libusb() -> str | None:
    """Return the absolute path of libusb-1.0.so on this device."""
    override = os.environ.get("BRIDGE_LIBUSB")
    if override:
        log.debug("_find_libusb: BRIDGE_LIBUSB override → %s", override)
        return override

    prefix = os.environ.get("PREFIX", "/data/data/com.termux/files/usr")
    dirs = [
        f"{prefix}/lib",
        "/data/data/com.termux/files/usr/lib",
        "/usr/lib",
        "/usr/lib/aarch64-linux-gnu",
        "/usr/lib/arm-linux-gnueabihf",
        "/usr/lib/x86_64-linux-gnu",
        "/usr/local/lib",
        "/lib",
    ]
    patterns = ("libusb-1.0.so", "libusb-1.0.so.0", "libusb-1.0.so.0.*", "libusb-1.so")
    for d in dict.fromkeys(dirs):
        for pat in patterns:
            for hit in sorted(glob.glob(os.path.join(d, pat))):
                if os.path.exists(hit):
                    log.debug("_find_libusb: found %s", hit)
                    return hit
    found = ctypes.util.find_library("usb-1.0") or ctypes.util.find_library("usb")
    if found:
        log.debug("_find_libusb: system linker found %s", found)
    return found


# ---- ctypes descriptor structure ------------------------------------------

class _LibUSBDescriptor(ctypes.Structure):
    """libusb_device_descriptor — stable ABI for all libusb 1.0.x."""
    _fields_ = [
        ("bLength",            ctypes.c_uint8),
        ("bDescriptorType",    ctypes.c_uint8),
        ("bcdUSB",             ctypes.c_uint16),
        ("bDeviceClass",       ctypes.c_uint8),
        ("bDeviceSubClass",    ctypes.c_uint8),
        ("bDeviceProtocol",    ctypes.c_uint8),
        ("bMaxPacketSize0",    ctypes.c_uint8),
        ("idVendor",           ctypes.c_uint16),
        ("idProduct",          ctypes.c_uint16),
        ("bcdDevice",          ctypes.c_uint16),
        ("iManufacturer",      ctypes.c_uint8),
        ("iProduct",           ctypes.c_uint8),
        ("iSerialNumber",      ctypes.c_uint8),
        ("bNumConfigurations", ctypes.c_uint8),
    ]


def _setup_libusb_prototypes(lib: ctypes.CDLL) -> None:
    """Declare ctypes argtypes/restype for the libusb functions we call."""
    vp  = ctypes.c_void_p
    pvp = ctypes.POINTER(ctypes.c_void_p)
    i   = ctypes.c_int
    u8  = ctypes.c_uint8
    u16 = ctypes.c_uint16
    u32 = ctypes.c_uint
    pi  = ctypes.POINTER(ctypes.c_int)
    cp  = ctypes.c_char_p

    lib.libusb_wrap_sys_device.restype  = i   # set argtypes per call (fd width varies)
    lib.libusb_get_device.restype       = vp;  lib.libusb_get_device.argtypes       = [vp]
    lib.libusb_get_device_descriptor.restype  = i;  lib.libusb_get_device_descriptor.argtypes  = [vp, vp]
    lib.libusb_get_string_descriptor_ascii.restype  = i;  lib.libusb_get_string_descriptor_ascii.argtypes = [vp, u8, cp, i]
    lib.libusb_claim_interface.restype  = i;   lib.libusb_claim_interface.argtypes  = [vp, i]
    lib.libusb_release_interface.restype = i;  lib.libusb_release_interface.argtypes = [vp, i]
    lib.libusb_kernel_driver_active.restype  = i;  lib.libusb_kernel_driver_active.argtypes  = [vp, i]
    lib.libusb_detach_kernel_driver.restype  = i;  lib.libusb_detach_kernel_driver.argtypes  = [vp, i]
    lib.libusb_set_configuration.restype     = i;  lib.libusb_set_configuration.argtypes     = [vp, i]
    lib.libusb_bulk_transfer.restype    = i;   lib.libusb_bulk_transfer.argtypes    = [vp, u8, cp, i, pi, u32]
    lib.libusb_control_transfer.restype = i;   lib.libusb_control_transfer.argtypes = [vp, u8, u8, u16, u16, cp, u16, u32]
    lib.libusb_close.restype = None;           lib.libusb_close.argtypes = [vp]


# ---- RawUSBDevice ---------------------------------------------------------

class RawUSBDevice:
    """
    Thin ctypes wrapper around a libusb_device_handle.
    Presents the same interface _BaseSerial subclasses use so they don't
    need to know whether we're going through pyusb or raw ctypes.
    """

    LIBUSB_SUCCESS       =  0
    LIBUSB_ERROR_TIMEOUT = -7

    def __init__(self, lib: ctypes.CDLL, handle, desc: _LibUSBDescriptor):
        self._lib    = lib
        self._handle = handle   # c_void_p
        self._desc   = desc

    # ---- descriptor properties ------------------------------------------------

    @property
    def idVendor(self) -> int:   return self._desc.idVendor
    @property
    def idProduct(self) -> int:  return self._desc.idProduct
    @property
    def manufacturer(self) -> str: return self._get_string(self._desc.iManufacturer)
    @property
    def product(self) -> str:      return self._get_string(self._desc.iProduct)

    def _get_string(self, index: int) -> str:
        if not self._handle or index == 0:
            return ""
        buf = ctypes.create_string_buffer(256)
        rc  = self._lib.libusb_get_string_descriptor_ascii(
            self._handle, ctypes.c_uint8(index), buf, 256
        )
        return buf.value.decode("utf-8", errors="replace") if rc > 0 else ""

    # ---- pyusb-compatible interface -------------------------------------------

    def is_kernel_driver_active(self, interface: int) -> bool:
        return self._lib.libusb_kernel_driver_active(self._handle, interface) == 1

    def detach_kernel_driver(self, interface: int) -> None:
        self._lib.libusb_detach_kernel_driver(self._handle, interface)

    def set_configuration(self, configuration: int = 1) -> None:
        self._lib.libusb_set_configuration(self._handle, configuration)

    def ctrl_transfer(self, bmRequestType: int, bRequest: int,
                      wValue: int = 0, wIndex: int = 0,
                      data_or_wLength=None, timeout: int = 1000):
        if data_or_wLength is None or data_or_wLength == 0:
            buf, length = None, 0
        elif isinstance(data_or_wLength, int):
            buf    = ctypes.create_string_buffer(data_or_wLength)
            length = data_or_wLength
        else:
            raw    = bytes(data_or_wLength)
            buf    = ctypes.create_string_buffer(raw, len(raw))
            length = len(raw)

        rc = self._lib.libusb_control_transfer(
            self._handle,
            ctypes.c_uint8(bmRequestType),  ctypes.c_uint8(bRequest),
            ctypes.c_uint16(wValue),        ctypes.c_uint16(wIndex),
            buf, ctypes.c_uint16(length),   ctypes.c_uint(timeout),
        )
        if rc < 0:
            log.debug("ctrl_transfer rc=%d (bmRT=0x%02X req=0x%02X val=0x%04X idx=0x%04X)",
                      rc, bmRequestType, bRequest, wValue, wIndex)
            return None
        if isinstance(data_or_wLength, int) and data_or_wLength > 0 and buf is not None:
            return buf.raw[:rc]
        return rc

    def read(self, endpoint: int, size: int, timeout: int = 20) -> bytes:
        buf         = ctypes.create_string_buffer(size)
        transferred = ctypes.c_int(0)
        rc = self._lib.libusb_bulk_transfer(
            self._handle, ctypes.c_uint8(endpoint),
            buf, ctypes.c_int(size), ctypes.byref(transferred), ctypes.c_uint(timeout),
        )
        return buf.raw[:transferred.value] if rc in (self.LIBUSB_SUCCESS, self.LIBUSB_ERROR_TIMEOUT) else b""

    def write(self, endpoint: int, data: bytes, timeout: int = 1000) -> int:
        raw         = bytes(data)
        buf         = ctypes.create_string_buffer(raw, len(raw))
        transferred = ctypes.c_int(0)
        rc = self._lib.libusb_bulk_transfer(
            self._handle, ctypes.c_uint8(endpoint),
            buf, ctypes.c_int(len(raw)), ctypes.byref(transferred), ctypes.c_uint(timeout),
        )
        return transferred.value if rc == self.LIBUSB_SUCCESS else 0

    def close(self) -> None:
        if self._handle:
            try:
                self._lib.libusb_release_interface(self._handle, 0)
            except Exception:
                pass
            self._lib.libusb_close(self._handle)
            self._handle = None


def _device_from_fd(fd: int) -> RawUSBDevice:
    """
    Wrap a termux-usb file descriptor into a RawUSBDevice using direct ctypes.

    ctx=NULL in libusb_wrap_sys_device() is intentional: it skips libusb_init()
    and device enumeration entirely (both fail on Android without root).
    libusb >= 1.0.23 supports NULL context for this call.
    """
    log.debug("_device_from_fd: fd=%d", fd)

    libusb_path = _find_libusb()
    if libusb_path is None:
        raise RuntimeError("libusb-1.0.so not found.  Run:  pkg install libusb")
    log.debug("_device_from_fd: loading %s", libusb_path)

    # Ensure $PREFIX/lib is in LD_LIBRARY_PATH for transitive deps
    lib_dir = os.path.dirname(os.path.abspath(libusb_path))
    _ld = os.environ.get("LD_LIBRARY_PATH", "")
    if lib_dir not in _ld.split(":"):
        os.environ["LD_LIBRARY_PATH"] = f"{lib_dir}:{_ld}".strip(":")

    try:
        lib = ctypes.CDLL(libusb_path)
    except OSError as e:
        raise RuntimeError(
            f"Cannot load {libusb_path}: {e}\n"
            f"Try:  LD_LIBRARY_PATH={lib_dir} termux-usb -r -e ./main.py <device>"
        ) from e

    _setup_libusb_prototypes(lib)
    log.debug("_device_from_fd: libusb loaded OK")

    # Wrap Android fd — NULL context skips libusb_init() which fails on Android
    handle = ctypes.c_void_p()
    rc     = -1
    for fd_ctype in (ctypes.c_int64, ctypes.c_int):
        lib.libusb_wrap_sys_device.argtypes = [
            ctypes.c_void_p,
            fd_ctype,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        rc = lib.libusb_wrap_sys_device(None, fd_ctype(fd), ctypes.byref(handle))
        log.debug("libusb_wrap_sys_device(ctx=NULL, %s) → %d", fd_ctype.__name__, rc)
        if rc == 0:
            break

    if rc != 0:
        raise RuntimeError(f"libusb_wrap_sys_device failed: rc={rc} (fd={fd})")

    log.debug("_device_from_fd: handle=%s", handle)

    device = lib.libusb_get_device(handle)
    desc   = _LibUSBDescriptor()
    lib.libusb_get_device_descriptor(device, ctypes.byref(desc))
    log.debug("_device_from_fd: VID=%04X PID=%04X", desc.idVendor, desc.idProduct)

    # Claim interface 0 so bulk transfers work.
    # Android may already hold the interface (rc=-6 BUSY) — still fine for I/O.
    rc_claim = lib.libusb_claim_interface(handle, 0)
    log.debug("libusb_claim_interface(0) → %d (0=OK, -6=BUSY is also OK)", rc_claim)

    return RawUSBDevice(lib, handle, desc)


# ---------------------------------------------------------------------------
# Serial chip drivers
# ---------------------------------------------------------------------------

class _BaseSerial:
    """Minimal interface every driver must implement."""

    READ_EP: int = 0x81
    WRITE_EP: int = 0x01
    CHUNK: int = 512

    def __init__(self, dev, baud: int = 115200):
        self._dev = dev
        self._baud = baud

    def open(self):
        raise NotImplementedError

    def close(self):
        pass

    def read(self, size: int, timeout_ms: int) -> bytes:
        try:
            data = self._dev.read(self.READ_EP, size, timeout=timeout_ms)
            return bytes(data)
        except Exception:
            return b""

    def write(self, data: bytes, timeout_ms: int = 1000) -> int:
        try:
            return self._dev.write(self.WRITE_EP, data, timeout=timeout_ms)
        except Exception:
            return 0


class CP210xSerial(_BaseSerial):
    """
    CP210x (Silicon Labs) USB-UART driver.

    Command codes from the Linux kernel driver (drivers/usb/serial/cp210x.c)
    and felHR85/UsbSerial Java implementation.
    """

    _REQ_H2D = 0x41   # bmRequestType host→device, vendor, interface
    _REQ_D2H = 0xC1   # bmRequestType device→host, vendor, interface

    _IFC_ENABLE   = 0x00
    _SET_BAUDDIV  = 0x01
    _SET_LINE_CTL = 0x03
    _SET_MHS      = 0x07
    _SET_BAUDRATE = 0x1E
    _PURGE        = 0x12

    _UART_ENABLE  = 0x0001
    _UART_DISABLE = 0x0000
    _LINE_CTL_DEFAULT = 0x0800   # 8N1
    _MHS_DTR_ON   = 0x0101
    _MHS_RTS_ON   = 0x0202
    _PURGE_ALL    = 0x000F

    def _ctrl(self, req_type, request, value=0, index=0, data_or_length=None):
        return self._dev.ctrl_transfer(req_type, request, value, index, data_or_length)

    def open(self):
        try:
            if self._dev.is_kernel_driver_active(0):
                self._dev.detach_kernel_driver(0)
        except Exception as e:
            log.debug("detach_kernel_driver skipped: %s", e)
        # Android already sets the configuration; calling set_configuration()
        # again returns LIBUSB_ERROR_NO_DEVICE — catch and continue.
        try:
            self._dev.set_configuration()
        except Exception as e:
            log.debug("set_configuration skipped (Android already configured): %s", e)

        # Enable UART
        self._ctrl(self._REQ_H2D, self._IFC_ENABLE, self._UART_ENABLE)
        # Set baud rate (4-byte little-endian)
        import struct
        baud_bytes = struct.pack("<I", self._baud)
        self._ctrl(self._REQ_H2D, self._SET_BAUDRATE, 0, 0, baud_bytes)
        # Set 8N1
        self._ctrl(self._REQ_H2D, self._SET_LINE_CTL, self._LINE_CTL_DEFAULT)
        # Assert DTR + RTS (needed by esptool for auto-reset)
        self._ctrl(self._REQ_H2D, self._SET_MHS, self._MHS_DTR_ON | self._MHS_RTS_ON)
        # Purge buffers
        self._ctrl(self._REQ_H2D, self._PURGE, self._PURGE_ALL)
        log.info("CP210x opened at %d baud (8N1)", self._baud)

    def close(self):
        try:
            self._ctrl(self._REQ_H2D, self._IFC_ENABLE, self._UART_DISABLE)
        except Exception:
            pass


class CH34xSerial(_BaseSerial):
    """
    CH340/CH341 USB-UART driver.

    Divisor formula from the Linux kernel driver (drivers/usb/serial/ch341.c).
    """

    _REQ_WRITE  = 0x40   # bmRequestType vendor out
    _REQ_READ   = 0xC0   # bmRequestType vendor in

    _CMD_WRITE_REG = 0x9A
    _CMD_SERIAL_INIT = 0xA1
    _CMD_MODEM_CTRL  = 0xA4

    # Clock frequency used by CH340
    _CH341_BAUDBASE_FACTOR = 1532620800
    _CH341_BAUDBASE_DIVMAX = 3

    def _ctrl_write(self, request, value, index):
        self._dev.ctrl_transfer(self._REQ_WRITE, request, value, index, None)

    def _baud_divisor(self, baud: int):
        """Return (factor, divisor) for CH340 baud rate register."""
        a = self._CH341_BAUDBASE_FACTOR // baud
        b = self._CH341_BAUDBASE_DIVMAX
        while b > 0 and a > 0xFF:
            a //= 8
            b -= 1
        if a > 0xFF:
            raise ValueError(f"Baud rate {baud} too low for CH340")
        return a, b

    def open(self):
        try:
            if self._dev.is_kernel_driver_active(0):
                self._dev.detach_kernel_driver(0)
        except Exception as e:
            log.debug("detach_kernel_driver skipped: %s", e)
        try:
            self._dev.set_configuration()
        except Exception as e:
            log.debug("set_configuration skipped (Android already configured): %s", e)

        # Serial init
        self._ctrl_write(self._CMD_SERIAL_INIT, 0, 0)
        # Set baud rate
        factor, divisor = self._baud_divisor(self._baud)
        self._ctrl_write(self._CMD_WRITE_REG, 0x1312, (factor & 0xFF) | ((divisor & 0x07) << 8))
        self._ctrl_write(self._CMD_WRITE_REG, 0x0F2C, 0x0008)  # 8N1
        # Assert DTR + RTS
        self._ctrl_write(self._CMD_MODEM_CTRL, ~(0x20 | 0x40) & 0xFF, 0)
        log.info("CH34x opened at %d baud (8N1)", self._baud)


class FTDISerial(_BaseSerial):
    """
    FTDI (FT232R, FT231X, …) USB-UART driver.

    Uses FTDI's vendor command set (reverse-engineered, widely documented).
    """

    _REQ_OUT = 0x40
    _CMD_RESET        = 0x00
    _CMD_SET_BAUDRATE = 0x03
    _CMD_SET_DATA     = 0x04
    _CMD_SET_FLOW     = 0x02
    _CMD_SET_MODEM    = 0x01

    _RESET_SIO = 0
    _PURGE_RX  = 1
    _PURGE_TX  = 2

    # FTDI base clock for baud calculation
    _BASE_CLOCK = 3_000_000

    READ_EP  = 0x81
    WRITE_EP = 0x02   # FTDI typically uses EP2 for bulk out

    def _ctrl(self, request, value, index=0):
        self._dev.ctrl_transfer(self._REQ_OUT, request, value, index, None)

    def _baud_divisor(self, baud: int) -> int:
        """Return 14-bit FTDI baud divisor (integer + 3-bit sub-int)."""
        div = self._BASE_CLOCK // baud
        return div

    def open(self):
        try:
            if self._dev.is_kernel_driver_active(0):
                self._dev.detach_kernel_driver(0)
        except Exception as e:
            log.debug("detach_kernel_driver skipped: %s", e)
        try:
            self._dev.set_configuration()
        except Exception as e:
            log.debug("set_configuration skipped (Android already configured): %s", e)

        self._ctrl(self._CMD_RESET, self._RESET_SIO)
        self._ctrl(self._CMD_SET_BAUDRATE, self._baud_divisor(self._baud))
        self._ctrl(self._CMD_SET_DATA, 0x0008)   # 8N1
        self._ctrl(self._CMD_SET_FLOW, 0x0000)   # no flow control
        self._ctrl(self._CMD_SET_MODEM, 0x0303)  # DTR + RTS on
        self._ctrl(self._CMD_RESET, self._PURGE_RX)
        self._ctrl(self._CMD_RESET, self._PURGE_TX)
        log.info("FTDI opened at %d baud (8N1)", self._baud)

    def read(self, size: int, timeout_ms: int) -> bytes:
        """FTDI prepends a 2-byte status header to every bulk read packet."""
        try:
            raw = bytes(self._dev.read(self.READ_EP, size + 2, timeout=timeout_ms))
            # Strip the 2-byte modem status prefix if present
            return raw[2:] if len(raw) >= 2 else b""
        except Exception:
            return b""


# ---------------------------------------------------------------------------
# Device detection
# ---------------------------------------------------------------------------

# (VID, PID) → driver class
_KNOWN_DEVICES: dict[tuple[int, int], type[_BaseSerial]] = {
    # CP210x family (Silicon Labs)
    (0x10C4, 0xEA60): CP210xSerial,  # CP2102 / CP2104 — most common ESP32 adapters
    (0x10C4, 0xEA61): CP210xSerial,
    (0x10C4, 0xEA63): CP210xSerial,
    (0x10C4, 0xEA70): CP210xSerial,
    (0x10C4, 0xEA71): CP210xSerial,
    # CH340 / CH341 (WCH)
    (0x1A86, 0x7523): CH34xSerial,   # CH340
    (0x1A86, 0x5523): CH34xSerial,   # CH341A
    (0x1A86, 0x7522): CH34xSerial,   # CH340K
    # FTDI
    (0x0403, 0x6001): FTDISerial,    # FT232R
    (0x0403, 0x6015): FTDISerial,    # FT231X
    (0x0403, 0x6010): FTDISerial,    # FT2232
    (0x0403, 0x6011): FTDISerial,    # FT4232
    # Arduino Uno R4 Minima built-in USB-CDC (native, no UART chip)
    (0x2341, 0x006D): CP210xSerial,  # placeholder; R4 uses CDC-ACM natively
}


def _auto_detect_driver(dev, baud: int) -> _BaseSerial:
    """Pick a driver based on VID:PID; fall back to CP210x if unknown."""
    vid = dev.idVendor
    pid = dev.idProduct
    cls = _KNOWN_DEVICES.get((vid, pid))
    if cls:
        log.info("Detected %s (VID=%04X PID=%04X) → using %s", dev.product or "?", vid, pid, cls.__name__)
        return cls(dev, baud)

    log.warning(
        "Unknown device VID=%04X PID=%04X — defaulting to CP210x driver. "
        "Set BRIDGE_DRIVER=ch34x or bridge_driver=ftdi to override.",
        vid,
        pid,
    )
    override = os.environ.get("BRIDGE_DRIVER", "").lower()
    if override == "ch34x":
        return CH34xSerial(dev, baud)
    if override == "ftdi":
        return FTDISerial(dev, baud)
    return CP210xSerial(dev, baud)


# ---------------------------------------------------------------------------
# Bridge core
# ---------------------------------------------------------------------------

_running = threading.Event()
_running.set()


def _usb_to_tcp(serial: _BaseSerial, conn: socket.socket, stop: threading.Event):
    """Forward USB→TCP.  Runs in its own thread."""
    conn.setblocking(False)
    while not stop.is_set():
        chunk = serial.read(serial.CHUNK, USB_TIMEOUT_MS)
        if chunk:
            try:
                conn.sendall(chunk)
            except (BrokenPipeError, ConnectionResetError, OSError):
                log.info("TCP client disconnected (USB→TCP)")
                stop.set()
                break
    log.debug("USB→TCP thread exit")


def _tcp_to_usb(serial: _BaseSerial, conn: socket.socket, stop: threading.Event):
    """Forward TCP→USB.  Runs in its own thread."""
    conn.setblocking(True)
    conn.settimeout(0.1)
    while not stop.is_set():
        try:
            data = conn.recv(serial.CHUNK)
            if not data:
                log.info("TCP client disconnected (TCP→USB)")
                stop.set()
                break
            serial.write(data)
        except socket.timeout:
            continue
        except (BrokenPipeError, ConnectionResetError, OSError):
            stop.set()
            break
    log.debug("TCP→USB thread exit")


def serve(serial: _BaseSerial, host: str, port: int):
    """Accept TCP connections one at a time and bridge each to USB."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(1)
    srv.settimeout(1.0)
    log.info("Listening on %s:%d  (Ctrl-C to stop)", host, port)
    log.info("Connect with:  mpremote connect socket://localhost:%d", port)
    log.info("           or: esptool.py --port socket://localhost:%d flash_id", port)

    try:
        while _running.is_set():
            try:
                conn, addr = srv.accept()
            except socket.timeout:
                continue

            log.info("Client connected from %s:%d", *addr)
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

            stop = threading.Event()
            t_usb2tcp = threading.Thread(
                target=_usb_to_tcp, args=(serial, conn, stop), daemon=True
            )
            t_tcp2usb = threading.Thread(
                target=_tcp_to_usb, args=(serial, conn, stop), daemon=True
            )
            t_usb2tcp.start()
            t_tcp2usb.start()

            # Wait for session to end
            stop.wait()
            conn.close()
            t_usb2tcp.join(timeout=2)
            t_tcp2usb.join(timeout=2)
            log.info("Session ended, waiting for next client…")
    finally:
        srv.close()


# ---------------------------------------------------------------------------
# Signal handling
# ---------------------------------------------------------------------------

def _on_signal(signum, frame):
    log.info("Signal %d received, shutting down…", signum)
    _running.clear()


signal.signal(signal.SIGINT, _on_signal)
signal.signal(signal.SIGTERM, _on_signal)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    # ---- Determine USB file descriptor ----
    fd_env = os.environ.get("TERMUX_USB_FD")
    if fd_env:
        # Launched via:  termux-usb -r -e python main.py /dev/bus/usb/...
        fd = int(fd_env)
        log.info("Using TERMUX_USB_FD=%d from environment", fd)
    elif len(sys.argv) > 1 and sys.argv[1].isdigit():
        # Direct FD passed as argument (advanced / scripted use)
        fd = int(sys.argv[1])
        log.info("Using file descriptor %d from command-line argument", fd)
    else:
        log.error(
            "No USB file descriptor found.\n"
            "Run via:  termux-usb -r -e python main.py /dev/bus/usb/001/002\n"
            "Or set TERMUX_USB_FD manually for testing."
        )
        sys.exit(1)

    # ---- Wrap fd into pyusb Device ----
    log.info("Opening USB device from fd=%d…", fd)
    try:
        dev = _device_from_fd(fd)
    except Exception as e:
        log.error("Failed to wrap USB fd: %s", e)
        log.error("Is libusb installed?  pkg install libusb")
        sys.exit(1)

    log.info(
        "USB device: VID=%04X PID=%04X  %s / %s",
        dev.idVendor,
        dev.idProduct,
        getattr(dev, "manufacturer", "?"),
        getattr(dev, "product", "?"),
    )

    # ---- Select driver ----
    serial = _auto_detect_driver(dev, BRIDGE_BAUD)

    try:
        serial.open()
    except Exception as e:
        log.error("Could not open serial device: %s", e)
        sys.exit(1)

    # ---- Bridge ----
    try:
        serve(serial, BRIDGE_HOST, BRIDGE_PORT)
    finally:
        serial.close()
        log.info("Bridge stopped.")


if __name__ == "__main__":
    main()
