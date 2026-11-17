"""In-memory stand-in for HidRelayBoard (development, CI, ConformU without hardware)."""

from __future__ import annotations

import threading
from typing import List, Optional

from .hidrelay import RelayError


class MockBoard:
    def __init__(self, channels: Optional[int] = 4, **_):
        self._channels = channels or 4
        self._state = [False] * self._channels
        self._open = False
        self._lock = threading.Lock()

    @property
    def is_open(self) -> bool:
        return self._open

    @property
    def channels(self) -> int:
        return self._channels

    def open(self) -> None:
        self._open = True

    def close(self) -> None:
        self._open = False

    def _chk(self, ch: int) -> None:
        if not self._open:
            raise RelayError("Board is not open")
        if not 1 <= ch <= self._channels:
            raise ValueError("bad channel")

    def read_channel(self, channel: int) -> bool:
        with self._lock:
            self._chk(channel)
            return self._state[channel - 1]

    def read_all(self) -> List[bool]:
        with self._lock:
            return list(self._state)

    def write_channel(self, channel: int, on: bool) -> None:
        with self._lock:
            self._chk(channel)
            self._state[channel - 1] = on
