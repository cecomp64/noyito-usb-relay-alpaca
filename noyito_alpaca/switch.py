"""ASCOM Switch (interface v3) device logic, independent of HTTP.

One SwitchDevice == one physical relay board == one Alpaca device number.
Every channel is a writable boolean switch (min 0, max 1, step 1).
"""

from __future__ import annotations

import datetime
import threading
from typing import Any, List

from . import __version__
from .config import DeviceConfig, StateStore
from .hidrelay import HidRelayBoard, RelayError
from .mock import MockBoard

# ASCOM Alpaca error numbers
NOT_IMPLEMENTED = 0x400
INVALID_VALUE = 0x401
NOT_CONNECTED = 0x407
INVALID_OPERATION = 0x40B
DRIVER_ERROR = 0x500


class AlpacaError(Exception):
    def __init__(self, number: int, message: str):
        super().__init__(message)
        self.number = number
        self.message = message


def not_implemented(what: str) -> AlpacaError:
    return AlpacaError(NOT_IMPLEMENTED, f"{what} is not implemented")


class SwitchDevice:
    INTERFACE_VERSION = 3

    def __init__(self, number: int, cfg: DeviceConfig, store: StateStore):
        self.number = number
        self.cfg = cfg
        self.store = store
        self.board = (
            MockBoard(channels=cfg.channels)
            if cfg.mock
            else HidRelayBoard(serial=cfg.serial, usb_path=cfg.usb_path, channels=cfg.channels)
        )
        self._connected = False
        self._lock = threading.Lock()

    # ---- helpers ----------------------------------------------------------------
    @property
    def unique_id(self) -> str:
        return self.cfg.unique_id

    @property
    def max_switch(self) -> int:
        # Known even when disconnected: configured value, else board's 4-channel default
        # (updated to the detected value once connected).
        return self.board.channels

    def _id(self, id_: int) -> int:
        if not 0 <= id_ < self.max_switch:
            raise AlpacaError(INVALID_VALUE, f"Switch id {id_} out of range 0..{self.max_switch - 1}")
        return id_

    def _need_connected(self) -> None:
        if not self._connected:
            raise AlpacaError(NOT_CONNECTED, "Device is not connected")

    def _hw(self, fn, *args):
        try:
            return fn(*args)
        except RelayError as e:
            raise AlpacaError(DRIVER_ERROR, str(e)) from e

    # ---- direct access for the setup page (works whether or not a client is connected) ----
    def _direct(self, fn):
        with self._lock:
            if self._connected:
                return fn()
            self._hw(self.board.open)
            try:
                return fn()
            finally:
                self.board.close()

    def peek_state(self):
        """Current relay states, or None if the board can't be read (unplugged etc.)."""
        try:
            return self._direct(lambda: self._hw(self.board.read_all))
        except AlpacaError:
            return None

    def write_direct(self, id_: int, state: bool) -> None:
        self._id(id_)
        self._direct(lambda: self._hw(self.board.write_channel, id_ + 1, bool(state)))

    # ---- common -----------------------------------------------------------------
    @property
    def connected(self) -> bool:
        return self._connected

    def set_connected(self, value: bool) -> None:
        with self._lock:
            if value and not self._connected:
                self._hw(self.board.open)
                self._connected = True
            elif not value and self._connected:
                self._connected = False
                self.board.close()

    name = property(lambda self: self.cfg.name)
    description = property(lambda self: f"NOYITO USB relay board ({self.max_switch} channels)")
    driver_info = property(lambda self: f"NOYITO USB Relay Alpaca driver (Python) v{__version__}")
    driver_version = property(lambda self: __version__)
    supported_actions: List[str] = []

    def device_state(self) -> List[dict]:
        out: List[dict] = []
        if self._connected:
            states = self._hw(self.board.read_all)
            for i, on in enumerate(states):
                out.append({"Name": f"GetSwitch{i}", "Value": on})
                out.append({"Name": f"GetSwitchValue{i}", "Value": 1.0 if on else 0.0})
        out.append(
            {"Name": "TimeStamp", "Value": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")}
        )
        return out

    # ---- switch -----------------------------------------------------------------
    def can_write(self, id_: int) -> bool:
        self._id(id_)
        return True

    def get_switch_name(self, id_: int) -> str:
        self._id(id_)
        saved = self.store.get(self.unique_id, "names", id_)
        if saved is not None:
            return saved
        return self.cfg.names[id_] if id_ < len(self.cfg.names) else f"Relay {id_ + 1}"

    def get_switch_description(self, id_: int) -> str:
        self._id(id_)
        saved = self.store.get(self.unique_id, "descriptions", id_)
        if saved is not None:
            return saved
        return self.cfg.descriptions[id_] if id_ < len(self.cfg.descriptions) else f"NOYITO relay channel {id_ + 1}"

    def set_switch_name(self, id_: int, name: str) -> None:
        self._id(id_)
        self.store.set(self.unique_id, "names", id_, name)

    def set_switch_description(self, id_: int, text: str) -> None:
        self._id(id_)
        self.store.set(self.unique_id, "descriptions", id_, text)

    min_switch_value = lambda self, id_: (self._id(id_), 0.0)[1]  # noqa: E731
    max_switch_value = lambda self, id_: (self._id(id_), 1.0)[1]  # noqa: E731
    switch_step = lambda self, id_: (self._id(id_), 1.0)[1]  # noqa: E731

    def get_switch(self, id_: int) -> bool:
        self._id(id_)
        self._need_connected()
        return self._hw(self.board.read_channel, id_ + 1)

    def set_switch(self, id_: int, state: bool) -> None:
        self._id(id_)
        self._need_connected()
        self._hw(self.board.write_channel, id_ + 1, bool(state))

    def get_switch_value(self, id_: int) -> float:
        return 1.0 if self.get_switch(id_) else 0.0

    def set_switch_value(self, id_: int, value: float) -> None:
        self._id(id_)
        if not 0.0 <= value <= 1.0:
            raise AlpacaError(INVALID_VALUE, f"Value {value} outside 0..1")
        self.set_switch(id_, value >= 0.5)

    # ---- async (not supported: relays switch instantly) ---------------------------
    def can_async(self, id_: int) -> bool:
        self._id(id_)
        return False

    def async_unsupported(self, id_: int, what: str) -> Any:
        self._id(id_)
        raise not_implemented(what)
