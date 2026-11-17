"""Alpaca HTTP API (Falcon), management API, discovery and setup pages."""

from __future__ import annotations

import itertools
import json
import logging
import socket
import socketserver
import threading
from typing import Dict, List
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

import falcon

from . import __version__
from .config import Config, StateStore
from .registry import Registry
from .setup import add_setup_routes
from .switch import (
    DRIVER_ERROR,
    INVALID_VALUE,
    AlpacaError,
    SwitchDevice,
    not_implemented,
)

log = logging.getLogger("noyito.server")

API_VERSION = 1
DISCOVERY_PORT = 32227
DISCOVERY_MSG = b"alpacadiscovery1"

_txn = itertools.count(1)
_txn_lock = threading.Lock()


def _next_txn() -> int:
    with _txn_lock:
        return next(_txn)


# --------------------------------------------------------------------------------
# parameter helpers (Alpaca parameter names are case-insensitive)
# --------------------------------------------------------------------------------
# Canonical spellings of PUT form parameters. Per the Alpaca spec GET query parameter
# names are case-insensitive but PUT form parameter names are case-SENSITIVE: a
# wrongly-cased name is treated as missing (-> HTTP 400 when required).
_PUT_NAMES = {n.lower(): n for n in ("Id", "State", "Name", "Value", "Connected", "ClientID", "ClientTransactionID")}


def _params(req: falcon.Request) -> Dict[str, str]:
    if req.method in ("PUT", "POST"):
        p: Dict[str, str] = {}
        if req.content_length:
            try:
                form = req.get_media(default_when_empty={})
            except Exception:
                raise falcon.HTTPBadRequest(description="Unparseable form body")
            if isinstance(form, dict):
                for lower, canon in _PUT_NAMES.items():
                    if canon in form:
                        p[lower] = form[canon]
    else:
        p = {k.lower(): v for k, v in req.params.items()}
    return {k: (v[-1] if isinstance(v, list) else v) for k, v in p.items()}


def _need(p: Dict[str, str], key: str) -> str:
    if key not in p or p[key] == "":
        raise falcon.HTTPBadRequest(description=f"Missing parameter: {key}")
    return p[key]


def _int(p, key) -> int:
    try:
        return int(_need(p, key))
    except ValueError:
        # Per Alpaca, a malformed/non-integer Id is a client error
        raise falcon.HTTPBadRequest(description=f"Parameter {key} must be an integer")


def _float(p, key) -> float:
    try:
        return float(_need(p, key))
    except ValueError:
        raise falcon.HTTPBadRequest(description=f"Parameter {key} must be a number")


def _bool(p, key) -> bool:
    v = _need(p, key).strip().lower()
    if v not in ("true", "false"):
        raise falcon.HTTPBadRequest(description=f"Parameter {key} must be True or False")
    return v == "true"


def _envelope(p: Dict[str, str], value=None, err: AlpacaError | None = None, with_value=True) -> dict:
    ctid = 0
    if "clienttransactionid" in p:
        try:
            ctid = int(p["clienttransactionid"])
            if ctid < 0:
                raise ValueError
        except ValueError:
            raise falcon.HTTPBadRequest(description="Invalid ClientTransactionID")
    if "clientid" in p:
        try:
            if int(p["clientid"]) < 0:
                raise ValueError
        except ValueError:
            raise falcon.HTTPBadRequest(description="Invalid ClientID")
    out = {}
    if with_value:
        out["Value"] = value
    out.update(
        ClientTransactionID=ctid,
        ServerTransactionID=_next_txn(),
        ErrorNumber=err.number if err else 0,
        ErrorMessage=err.message if err else "",
    )
    return out


# --------------------------------------------------------------------------------
# /api/v1/switch/{devnum}/{method}
# --------------------------------------------------------------------------------
class SwitchAPI:
    """Single resource dispatching on the method name.

    ``GET`` handlers return a value; ``PUT`` handlers return None (no ``Value`` key).
    """

    def __init__(self, registry: Registry):
        self.registry = registry
        self.get_methods = {
            # common
            "connected": lambda dev, p: dev.connected,
            "connecting": lambda dev, p: False,
            "description": lambda dev, p: dev.description,
            "devicestate": lambda dev, p: dev.device_state(),
            "driverinfo": lambda dev, p: dev.driver_info,
            "driverversion": lambda dev, p: dev.driver_version,
            "interfaceversion": lambda dev, p: dev.INTERFACE_VERSION,
            "name": lambda dev, p: dev.name,
            "supportedactions": lambda dev, p: dev.supported_actions,
            # switch
            "maxswitch": lambda dev, p: dev.max_switch,
            "canwrite": lambda dev, p: dev.can_write(_int(p, "id")),
            "canasync": lambda dev, p: dev.can_async(_int(p, "id")),
            "getswitch": lambda dev, p: dev.get_switch(_int(p, "id")),
            "getswitchdescription": lambda dev, p: dev.get_switch_description(_int(p, "id")),
            "getswitchname": lambda dev, p: dev.get_switch_name(_int(p, "id")),
            "getswitchvalue": lambda dev, p: dev.get_switch_value(_int(p, "id")),
            "minswitchvalue": lambda dev, p: dev.min_switch_value(_int(p, "id")),
            "maxswitchvalue": lambda dev, p: dev.max_switch_value(_int(p, "id")),
            "switchstep": lambda dev, p: dev.switch_step(_int(p, "id")),
            "statechangecomplete": lambda dev, p: dev.async_unsupported(_int(p, "id"), "StateChangeComplete"),
        }
        self.put_methods = {
            "connected": lambda dev, p: dev.set_connected(_bool(p, "connected")),
            "connect": lambda dev, p: dev.set_connected(True),
            "disconnect": lambda dev, p: dev.set_connected(False),
            "setswitch": lambda dev, p: dev.set_switch(_int(p, "id"), _bool(p, "state")),
            "setswitchname": lambda dev, p: dev.set_switch_name(_int(p, "id"), _need(p, "name")),
            "setswitchvalue": lambda dev, p: dev.set_switch_value(_int(p, "id"), _float(p, "value")),
            "setasync": lambda dev, p: dev.async_unsupported(_int(p, "id"), "SetAsync"),
            "setasyncvalue": lambda dev, p: dev.async_unsupported(_int(p, "id"), "SetAsyncValue"),
            "cancelasync": lambda dev, p: dev.async_unsupported(_int(p, "id"), "CancelAsync"),
            "action": self._unsupported("Action"),
            "commandblind": self._unsupported("CommandBlind"),
            "commandbool": self._unsupported("CommandBool"),
            "commandstring": self._unsupported("CommandString"),
        }
        # Action/Command* return a Value on PUT; since they always fail it doesn't matter.

    @staticmethod
    def _unsupported(what):
        def f(dev, p):
            raise not_implemented(what)

        return f

    def _dispatch(self, req, resp, devnum: int, method: str, table: dict, has_value: bool):
        p = _params(req)
        dev = self.registry.get(devnum)
        fn = table.get(method.lower())
        if dev is None or fn is None:
            raise falcon.HTTPNotFound()
        err = None
        value = None
        try:
            value = fn(dev, p)
        except AlpacaError as e:
            err = e
        body = _envelope(p, value, err, with_value=has_value)
        if err and has_value:
            body["Value"] = None if not isinstance(value, (list,)) else value
        if err:
            log.info("dev%d %s -> error 0x%X %s", devnum, method, err.number, err.message)
        resp.content_type = "application/json"
        resp.text = json.dumps(body)

    def on_get(self, req, resp, devnum, method):
        self._dispatch(req, resp, devnum, method, self.get_methods, True)

    def on_put(self, req, resp, devnum, method):
        self._dispatch(req, resp, devnum, method, self.put_methods, False)


# --------------------------------------------------------------------------------
# Management API
# --------------------------------------------------------------------------------
class ApiVersions:
    def on_get(self, req, resp):
        resp.media = _envelope(_params(req), [API_VERSION])


class Description:
    def __init__(self, cfg: Config):
        self.cfg = cfg.server

    def on_get(self, req, resp):
        s = self.cfg
        resp.media = _envelope(
            _params(req),
            {"ServerName": s.server_name, "Manufacturer": s.manufacturer, "ManufacturerVersion": __version__, "Location": s.location},
        )


class ConfiguredDevices:
    def __init__(self, registry: Registry):
        self.registry = registry

    def on_get(self, req, resp):
        resp.media = _envelope(
            _params(req),
            [{"DeviceName": d.name, "DeviceType": "Switch", "DeviceNumber": d.number, "UniqueID": d.unique_id} for d in self.registry.list()],
        )


# --------------------------------------------------------------------------------
# Discovery (UDP broadcast, IPv4)
# --------------------------------------------------------------------------------
def _discovery_thread(port: int) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    except (AttributeError, OSError):
        pass
    try:
        sock.bind(("", DISCOVERY_PORT))
    except OSError as e:
        log.error("Discovery disabled, cannot bind UDP %d: %s", DISCOVERY_PORT, e)
        return
    log.info("Discovery listening on UDP %d", DISCOVERY_PORT)
    reply = json.dumps({"AlpacaPort": port}).encode()
    while True:
        data, addr = sock.recvfrom(1024)
        if data.strip().startswith(DISCOVERY_MSG):
            sock.sendto(reply, addr)


# --------------------------------------------------------------------------------
# App assembly
# --------------------------------------------------------------------------------
class _Threaded(socketserver.ThreadingMixIn, WSGIServer):
    daemon_threads = True
    request_queue_size = 128  # ConformU and busy clients open bursts of connections


class _QuietHandler(WSGIRequestHandler):
    def log_message(self, fmt, *args):
        log.debug("%s %s", self.client_address[0], fmt % args)


def build_app(cfg: Config) -> tuple[falcon.App, Registry]:
    store = StateStore(cfg.base_dir / cfg.server.state_file)
    registry = Registry(cfg, store)
    app = falcon.App()
    app.req_options.auto_parse_form_urlencoded = False  # we parse PUT bodies ourselves
    app.add_route("/api/v1/switch/{devnum:int(min=0)}/{method}", SwitchAPI(registry))
    app.add_route("/management/apiversions", ApiVersions())
    app.add_route("/management/v1/description", Description(cfg))
    app.add_route("/management/v1/configureddevices", ConfiguredDevices(registry))
    add_setup_routes(app, registry, cfg)

    def on_error(req, resp, ex, params):
        log.exception("Unhandled error in %s %s", req.method, req.path)
        raise falcon.HTTPInternalServerError(description="See server log")

    app.add_error_handler(Exception, on_error)
    return app, registry


def serve(cfg: Config) -> None:
    app, registry = build_app(cfg)
    if cfg.server.discovery:
        threading.Thread(target=_discovery_thread, args=(cfg.server.port,), daemon=True).start()
    with make_server(cfg.server.ip_address, cfg.server.port, app, server_class=_Threaded, handler_class=_QuietHandler) as httpd:
        log.info("Serving %d device(s) on %s:%d - setup page at /setup", len(registry.list()), cfg.server.ip_address, cfg.server.port)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            registry.shutdown()
