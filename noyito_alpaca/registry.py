"""Live, editable set of Alpaca Switch devices (device number -> SwitchDevice).

Persisted in ``devices.json`` next to the config. On first start it is seeded from the
``[[device]]`` blocks of ``config.toml``; after that the /setup web page owns it.
"""

from __future__ import annotations

import json
import logging
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Dict, List, Optional

from .config import Config, DeviceConfig, StateStore
from .switch import SwitchDevice

log = logging.getLogger("noyito.registry")

_HW_FIELDS = ("serial", "usb_path", "channels", "mock")


class RegistryError(ValueError):
    pass


class Registry:
    def __init__(self, cfg: Config, store: StateStore):
        self.store = store
        self.path = cfg.base_dir / cfg.server.devices_file
        self._lock = threading.RLock()
        self._devices: Dict[int, SwitchDevice] = {}
        try:
            rows = json.loads(self.path.read_text())
            for r in rows:
                self._devices[int(r["number"])] = SwitchDevice(int(r["number"]), DeviceConfig(**r["device"]), store)
        except FileNotFoundError:  # first run: seed from config.toml
            for i, d in enumerate(cfg.devices):
                self._devices[i] = SwitchDevice(i, d, store)
            self._save()
        except (OSError, ValueError, KeyError, TypeError) as e:
            raise SystemExit(f"Cannot read {self.path}: {e}  (fix or delete the file)")

    # ---- access -------------------------------------------------------------------
    def get(self, number: int) -> Optional[SwitchDevice]:
        return self._devices.get(number)

    def list(self) -> List[SwitchDevice]:
        return [self._devices[n] for n in sorted(self._devices)]

    def shutdown(self) -> None:
        for d in self.list():
            d.set_connected(False)

    # ---- editing --------------------------------------------------------------------
    def upsert(self, data: dict, number: Optional[int] = None) -> SwitchDevice:
        """Create (number None -> lowest free, or requested) or update a device."""
        with self._lock:
            name = str(data.get("name", "")).strip()
            if not name:
                raise RegistryError("Name is required")
            serial = str(data.get("serial", "") or "").strip()
            usb_path = str(data.get("usb_path", "") or "").strip()
            mock = bool(data.get("mock", False))
            if not (mock or serial or usb_path):
                raise RegistryError("Pick a detected board (USB port) or enter a serial / USB path")
            ch = data.get("channels")
            try:
                ch = int(ch) if ch not in (None, "") else None
            except (TypeError, ValueError):
                raise RegistryError("Channels must be a number")
            if ch is not None and not 1 <= ch <= 64:
                raise RegistryError("Channels must be 1..64")
            for n, other in self._devices.items():
                if n != number and not mock and usb_path and other.cfg.usb_path == usb_path:
                    raise RegistryError(f"USB port {usb_path} is already used by device {n} ({other.name})")

            existing = self._devices.get(number) if number is not None else None
            if existing is None:
                if number is None:
                    number = next(i for i in range(10_000) if i not in self._devices)
                elif number in self._devices or number < 0:
                    raise RegistryError(f"Device number {number} is not available")
                cfg = DeviceConfig(name=name, serial=serial, usb_path=usb_path, channels=ch, mock=mock, unique_id=str(uuid.uuid4()))
                dev = SwitchDevice(number, cfg, self.store)
            else:
                cfg = existing.cfg
                changed = any(
                    getattr(cfg, f) != v for f, v in zip(_HW_FIELDS, (serial, usb_path, ch, mock))
                )
                cfg.name = name
                if changed:  # different hardware: drop the connection and rebuild
                    existing.set_connected(False)
                    cfg.serial, cfg.usb_path, cfg.channels, cfg.mock = serial, usb_path, ch, mock
                    dev = SwitchDevice(number, cfg, self.store)
                else:
                    dev = existing
            self._devices[number] = dev
            self._save()
            return dev

    def renumber(self, old: int, new: int) -> None:
        with self._lock:
            if old not in self._devices:
                raise RegistryError("No such device")
            if new < 0 or (new != old and new in self._devices):
                raise RegistryError(f"Device number {new} is not available")
            dev = self._devices.pop(old)
            dev.number = new
            self._devices[new] = dev
            self._save()

    def remove(self, number: int) -> None:
        with self._lock:
            dev = self._devices.pop(number, None)
            if dev is None:
                raise RegistryError("No such device")
            dev.set_connected(False)
            self._save()

    def _save(self) -> None:
        rows = [{"number": n, "device": self._devices[n].cfg.to_dict()} for n in sorted(self._devices)]
        try:
            with tempfile.NamedTemporaryFile("w", dir=self.path.parent, delete=False, suffix=".tmp") as tf:
                json.dump(rows, tf, indent=2)
            Path(tf.name).replace(self.path)
        except OSError as e:
            log.error("Could not save %s: %s", self.path, e)
