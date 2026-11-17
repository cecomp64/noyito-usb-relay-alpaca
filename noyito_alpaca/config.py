"""Configuration loading (config.toml) and persisted runtime state (state.json)."""

from __future__ import annotations

import json
import logging
import tempfile
import threading
import tomllib
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

log = logging.getLogger("noyito.config")


@dataclass
class DeviceConfig:
    name: str
    serial: str = ""
    usb_path: str = ""
    channels: Optional[int] = None  # None = auto-detect from the board
    mock: bool = False  # in-memory fake board, for development / ConformU without hardware
    unique_id: str = ""
    names: List[str] = field(default_factory=list)  # default per-channel names
    descriptions: List[str] = field(default_factory=list)

    def key(self) -> str:
        return self.serial or self.usb_path or self.name

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in ("name", "serial", "usb_path", "channels", "mock", "unique_id", "names", "descriptions")}


@dataclass
class ServerConfig:
    ip_address: str = "0.0.0.0"
    port: int = 11111
    server_name: str = "NOYITO USB Relay Alpaca Server"
    manufacturer: str = "Community"
    location: str = "Raspberry Pi"
    discovery: bool = True
    log_level: str = "INFO"
    state_file: str = "state.json"
    devices_file: str = "devices.json"


@dataclass
class Config:
    server: ServerConfig
    devices: List[DeviceConfig]
    base_dir: Path


def load_config(path: Path) -> Config:
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    srv = ServerConfig(**raw.get("server", {}))
    devices: List[DeviceConfig] = []
    for i, d in enumerate(raw.get("device", [])):
        d = dict(d)
        d.setdefault("name", f"NOYITO USB Relay {i + 1}")
        dev = DeviceConfig(**d)
        if not (dev.mock or dev.serial or dev.usb_path):
            raise ValueError(f"[[device]] #{i} '{dev.name}': set 'serial' and/or 'usb_path' (or mock = true)")
        if not dev.unique_id:
            dev.unique_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"noyito-relay:{dev.key()}"))
        devices.append(dev)
    # An empty list is fine: boards can be added from the /setup web page.
    return Config(srv, devices, path.resolve().parent)


class StateStore:
    """Tiny JSON store for names/descriptions edited by clients (SetSwitchName) or the setup page."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._data: Dict[str, dict] = {}
        try:
            self._data = json.loads(path.read_text())
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            log.warning("Ignoring unreadable state file %s: %s", path, e)

    def get(self, uid: str, kind: str, index: int) -> Optional[str]:
        return self._data.get(uid, {}).get(kind, {}).get(str(index))

    def set(self, uid: str, kind: str, index: int, value: str) -> None:
        with self._lock:
            self._data.setdefault(uid, {}).setdefault(kind, {})[str(index)] = value
            self._flush()

    def _flush(self) -> None:
        try:  # atomic replace so a power cut can't leave a half-written file
            with tempfile.NamedTemporaryFile("w", dir=self.path.parent, delete=False, suffix=".tmp") as tf:
                json.dump(self._data, tf, indent=2)
            Path(tf.name).replace(self.path)
        except OSError as e:
            log.error("Could not save %s: %s", self.path, e)
