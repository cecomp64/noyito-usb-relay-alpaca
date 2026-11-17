"""Web configuration UI: /setup (page) and /setup/api/* (JSON)."""

from __future__ import annotations

from pathlib import Path

import falcon

from .config import Config
from .hidrelay import enumerate_relays
from .registry import Registry, RegistryError
from .switch import AlpacaError, SwitchDevice

_PAGE = Path(__file__).parent / "web" / "setup.html"


def _device_json(d: SwitchDevice, with_state: bool = True) -> dict:
    n = d.max_switch
    return {
        "number": d.number,
        "name": d.name,
        "serial": d.cfg.serial,
        "usb_path": d.cfg.usb_path,
        "channels": d.cfg.channels,
        "mock": d.cfg.mock,
        "unique_id": d.unique_id,
        "connected": d.connected,
        "max_switch": n,
        "state": d.peek_state() if with_state else None,
        "outputs": [{"name": d.get_switch_name(i), "description": d.get_switch_description(i)} for i in range(n)],
    }


class Page:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def on_get(self, req, resp):
        resp.content_type = "text/html; charset=utf-8"
        resp.text = _PAGE.read_text().replace("{{SERVER_NAME}}", self.cfg.server.server_name)


class StateAPI:
    def __init__(self, registry: Registry, cfg: Config):
        self.registry, self.cfg = registry, cfg

    def on_get(self, req, resp):
        devices = self.registry.list()
        detected = []
        for r in enumerate_relays():
            owner = next(
                (d.number for d in devices if not d.cfg.mock and d.cfg.usb_path == r.usb_path), None
            )
            detected.append({"devnode": r.devnode, "usb_path": r.usb_path, "serial": r.serial,
                             "channels": r.channels, "product": r.product, "assigned": owner})
        resp.media = {
            "server": {"name": self.cfg.server.server_name, "port": self.cfg.server.port},
            "devices": [_device_json(d) for d in devices],
            "detected": detected,
        }


def _bad(msg: str):
    raise falcon.HTTPBadRequest(description=msg)


class DevicesAPI:
    """POST /setup/api/devices  -> create;  PUT/DELETE /setup/api/devices/{n}"""

    def __init__(self, registry: Registry):
        self.registry = registry

    def on_post(self, req, resp):
        try:
            data = req.get_media()
            num = data.get("number")
            dev = self.registry.upsert(data, int(num) if num not in (None, "") else None)
            self._apply_outputs(dev, data)
        except RegistryError as e:
            _bad(str(e))
        resp.media = _device_json(dev)

    def on_put(self, req, resp, devnum):
        data = req.get_media()
        try:
            if self.registry.get(devnum) is None:
                raise falcon.HTTPNotFound()
            dev = self.registry.upsert(data, devnum)
            new = data.get("number")
            if new not in (None, "") and int(new) != devnum:
                self.registry.renumber(devnum, int(new))
                dev = self.registry.get(int(new))
            self._apply_outputs(dev, data)
        except (RegistryError, ValueError) as e:
            _bad(str(e))
        resp.media = _device_json(dev)

    def on_delete(self, req, resp, devnum):
        try:
            self.registry.remove(devnum)
        except RegistryError as e:
            raise falcon.HTTPNotFound(description=str(e))
        resp.media = {"ok": True}

    @staticmethod
    def _apply_outputs(dev: SwitchDevice, data: dict) -> None:
        for i, o in enumerate(data.get("outputs") or []):
            if i < dev.max_switch:
                dev.set_switch_name(i, str(o.get("name", "")).strip() or f"Relay {i + 1}")
                dev.set_switch_description(i, str(o.get("description", "")).strip())


class ChannelAPI:
    """POST /setup/api/devices/{n}/channels/{i}  {"state": true|false}  - drives the relay coil."""

    def __init__(self, registry: Registry):
        self.registry = registry

    def on_post(self, req, resp, devnum, chan):
        dev = self.registry.get(devnum)
        if dev is None:
            raise falcon.HTTPNotFound()
        state = req.get_media().get("state")
        if not isinstance(state, bool):
            _bad("state must be true or false")
        try:
            dev.write_direct(chan, state)
        except AlpacaError as e:
            raise falcon.HTTPBadRequest(description=e.message)
        resp.media = _device_json(dev)


def add_setup_routes(app: falcon.App, registry: Registry, cfg: Config) -> None:
    app.add_route("/setup", Page(cfg))
    app.add_route("/setup/api/state", StateAPI(registry, cfg))
    app.add_route("/setup/api/devices", DevicesAPI(registry))
    app.add_route("/setup/api/devices/{devnum:int(min=0)}", DevicesAPI(registry))
    app.add_route("/setup/api/devices/{devnum:int(min=0)}/channels/{chan:int(min=0)}", ChannelAPI(registry))
