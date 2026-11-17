"""Native Linux driver for DCTTech / NOYITO HID USB relay boards (VID 16C0, PID 05DF).

These boards enumerate as a plain HID device ("www.dcttech.com USBRelay4") and are
controlled with 8-byte HID *feature reports* (report id 0), exactly as in the
open-source ``usb-relay-hid`` project that the original Windows ASCOM driver wraps:

    write  [cmd, channel, 0, 0, 0, 0, 0, 0]
        cmd 0xFF = channel on     0xFD = channel off
        cmd 0xFE = all on         0xFC = all off   (channel byte ignored)
    read   [id0 id1 id2 id3 id4, 0, 0, state]
        bytes 0-4 : board "serial" (5 ASCII chars, factory-set, NOT guaranteed unique)
        byte  7   : bit (n-1) set  => channel n is on

The kernel's ``hidraw`` interface is used directly through ``ioctl`` so there is no
native dependency (no libhidapi / libusb needed).

Because the 5-character board id is not guaranteed to be unique (several boards from
one batch often share it) boards can also be addressed by their USB port path
(``HID_PHYS`` in sysfs, e.g. ``usb-xhci-hcd.1-1.4.4``), which is stable as long as the
board stays in the same physical port.
"""

from __future__ import annotations

import fcntl
import logging
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

log = logging.getLogger("noyito.hid")

VID = 0x16C0
PID = 0x05DF

CMD_ON = 0xFF
CMD_OFF = 0xFD
CMD_ALL_ON = 0xFE
CMD_ALL_OFF = 0xFC

_SYS_HIDRAW = Path("/sys/class/hidraw")

# ---- Linux ioctl number construction (asm-generic/ioctl.h) -----------------------
_IOC_WRITE = 1
_IOC_READ = 2


def _ioc(direction: int, typ: str, nr: int, size: int) -> int:
    return (direction << 30) | (size << 16) | (ord(typ) << 8) | nr


def _hidiocsfeature(length: int) -> int:
    return _ioc(_IOC_WRITE | _IOC_READ, "H", 0x06, length)


def _hidiocgfeature(length: int) -> int:
    return _ioc(_IOC_WRITE | _IOC_READ, "H", 0x07, length)


class RelayError(Exception):
    """Raised for any communication problem with a relay board."""


@dataclass(frozen=True)
class RelayInfo:
    """A relay board found on the USB bus."""

    devnode: str  # e.g. /dev/hidraw0 (can change across re-plugs)
    usb_path: str  # e.g. usb-xhci-hcd.1-1.4.4 (stable per physical port)
    product: str  # e.g. "www.dcttech.com USBRelay4"
    channels: int  # parsed from the product string, default 4
    serial: str = ""  # 5-char board id; filled in when the device could be opened

    @property
    def label(self) -> str:
        return f"{self.devnode} serial={self.serial or '?'} port={self.usb_path} channels={self.channels}"


def _parse_uevent(path: Path) -> dict:
    out = {}
    try:
        for line in path.read_text().splitlines():
            k, _, v = line.partition("=")
            out[k] = v
    except OSError:
        pass
    return out


def _channels_from_product(product: str) -> int:
    m = re.search(r"USBRelay(\d+)", product, re.I)
    return int(m.group(1)) if m else 4


def _read_serial(fd: int) -> tuple[str, int]:
    """Return (serial, state_byte) from a feature report read on an open fd."""
    buf = bytearray(9)  # [report id 0] + 8 data bytes
    try:
        fcntl.ioctl(fd, _hidiocgfeature(len(buf)), buf, True)
    except OSError as e:
        raise RelayError(f"HID feature report read failed: {e}") from e
    data = bytes(buf[1:9])
    raw = data[:5].rstrip(b"\x00")
    if raw and all(0x20 < c < 0x7F for c in raw):
        serial = raw.decode("ascii")
    else:  # blank/odd factory id (e.g. 0xFF first byte): use hex so it is still a stable key
        serial = data[:5].hex()
    return serial, data[7]


def enumerate_relays(read_serials: bool = True) -> List[RelayInfo]:
    """List all attached NOYITO/DCTTech HID relay boards."""
    found: List[RelayInfo] = []
    if not _SYS_HIDRAW.is_dir():
        return found
    for entry in sorted(_SYS_HIDRAW.iterdir(), key=lambda p: int(re.sub(r"\D", "", p.name) or 0)):
        uev = _parse_uevent(entry / "device" / "uevent")
        hid_id = uev.get("HID_ID", "").upper()
        if not hid_id.endswith(f"{VID:08X}:{PID:08X}"):
            continue
        product = uev.get("HID_NAME", "")
        phys = uev.get("HID_PHYS", "")
        usb_path = phys.split("/")[0]
        devnode = f"/dev/{entry.name}"
        serial = ""
        if read_serials:
            try:
                fd = os.open(devnode, os.O_RDWR)
                try:
                    serial, _ = _read_serial(fd)
                finally:
                    os.close(fd)
            except (OSError, RelayError) as e:
                log.warning("Could not read serial of %s: %s", devnode, e)
        found.append(RelayInfo(devnode, usb_path, product, _channels_from_product(product), serial))
    return found


class HidRelayBoard:
    """One physical board, selected by serial and/or USB port path.

    All hardware access is serialised with a lock. The device node is (re)opened on
    demand, so unplug/replug (which changes /dev/hidrawN) is handled transparently.
    """

    def __init__(self, serial: str = "", usb_path: str = "", channels: Optional[int] = None):
        if not serial and not usb_path:
            raise ValueError("A board needs a serial and/or a usb_path")
        self.serial = serial
        self.usb_path = usb_path
        self._channels_override = channels
        self._channels = channels or 4
        self._fd: Optional[int] = None
        self._lock = threading.RLock()

    # ---- identification ---------------------------------------------------------
    def _find(self) -> RelayInfo:
        cands = enumerate_relays(read_serials=True)
        if self.usb_path:
            cands = [c for c in cands if c.usb_path == self.usb_path]
        if self.serial:
            cands = [c for c in cands if c.serial == self.serial]
        if not cands:
            raise RelayError(f"Relay board not found (serial={self.serial!r}, usb_path={self.usb_path!r})")
        if len(cands) > 1:
            raise RelayError(
                f"{len(cands)} boards match serial={self.serial!r}; set usb_path to disambiguate "
                f"({', '.join(c.usb_path for c in cands)})"
            )
        return cands[0]

    # ---- connection -------------------------------------------------------------
    @property
    def is_open(self) -> bool:
        return self._fd is not None

    @property
    def channels(self) -> int:
        return self._channels

    def open(self) -> None:
        with self._lock:
            if self._fd is not None:
                return
            info = self._find()
            try:
                self._fd = os.open(info.devnode, os.O_RDWR)
            except PermissionError as e:
                raise RelayError(
                    f"Permission denied opening {info.devnode}. Install the udev rule "
                    f"(see README) and make sure you are in the 'plugdev' group."
                ) from e
            except OSError as e:
                raise RelayError(f"Cannot open {info.devnode}: {e}") from e
            self._channels = self._channels_override or info.channels
            log.info("Opened %s", info.label)

    def close(self) -> None:
        with self._lock:
            if self._fd is not None:
                try:
                    os.close(self._fd)
                except OSError:
                    pass
                self._fd = None

    def _drop(self) -> None:
        """Forget a (probably unplugged) fd so the next call re-enumerates."""
        self.close()

    # ---- operations -------------------------------------------------------------
    def _check_channel(self, channel: int) -> None:
        if not 1 <= channel <= self._channels:
            raise ValueError(f"channel must be 1..{self._channels}")

    def _send(self, cmd: int, channel: int = 0) -> None:
        buf = bytes([0, cmd, channel, 0, 0, 0, 0, 0, 0])
        try:
            fcntl.ioctl(self._fd, _hidiocsfeature(len(buf)), bytearray(buf), True)
        except OSError as e:
            raise RelayError(f"HID feature report write failed: {e}") from e

    def _state_byte(self) -> int:
        return _read_serial(self._fd)[1]

    def _with_retry(self, fn):
        """Run fn once; on I/O failure re-open (handles replug) and retry once."""
        with self._lock:
            if self._fd is None:
                raise RelayError("Board is not open")
            try:
                return fn()
            except RelayError:
                self._drop()
                self.open()
                return fn()

    def read_channel(self, channel: int) -> bool:
        self._check_channel(channel)
        return bool(self._with_retry(self._state_byte) & (1 << (channel - 1)))

    def read_all(self) -> List[bool]:
        st = self._with_retry(self._state_byte)
        return [bool(st & (1 << i)) for i in range(self._channels)]

    def write_channel(self, channel: int, on: bool) -> None:
        self._check_channel(channel)
        self._with_retry(lambda: self._send(CMD_ON if on else CMD_OFF, channel))
