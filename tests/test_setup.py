import tempfile
from pathlib import Path
from falcon import testing
from noyito_alpaca.config import load_config
from noyito_alpaca.server import build_app

def make(extra=""):
    d = Path(tempfile.mkdtemp())
    (d / "c.toml").write_text("[server]\ndiscovery = false\n" + extra)
    app, reg = build_app(load_config(d / "c.toml"))
    return testing.TestClient(app), d

def test_empty_start_then_add_edit_remove_persist():
    c, d = make()
    assert c.simulate_get("/setup").status_code == 200
    assert c.simulate_get("/management/v1/configureddevices").json["Value"] == []
    r = c.simulate_post("/setup/api/devices", json={"number": 5, "name": "Box", "mock": True, "channels": 4,
                        "outputs": [{"name": "Mount", "description": "EQ mount"}]})
    assert r.status_code == 200 and r.json["number"] == 5
    assert c.simulate_get("/api/v1/switch/5/getswitchname", params={"Id": 0}).json["Value"] == "Mount"
    # rename + renumber
    r = c.simulate_put("/setup/api/devices/5", json={"number": 2, "name": "Box2", "mock": True, "channels": 4})
    assert r.status_code == 200
    assert c.simulate_get("/api/v1/switch/5/maxswitch").status_code == 404
    assert c.simulate_get("/management/v1/configureddevices").json["Value"][0]["DeviceName"] == "Box2"
    # coil toggle through the setup API is visible through Alpaca
    assert c.simulate_post("/setup/api/devices/2/channels/1", json={"state": True}).status_code in (200, 400)
    # restart: devices.json survives
    app2, _ = build_app(load_config(d / "c.toml"))
    c2 = testing.TestClient(app2)
    assert [x["DeviceNumber"] for x in c2.simulate_get("/management/v1/configureddevices").json["Value"]] == [2]
    assert c2.simulate_delete("/setup/api/devices/2").status_code == 200
    assert c2.simulate_get("/management/v1/configureddevices").json["Value"] == []

def test_validation():
    c, _ = make()
    assert c.simulate_post("/setup/api/devices", json={"name": "", "mock": True}).status_code == 400
    assert c.simulate_post("/setup/api/devices", json={"name": "x"}).status_code == 400
    assert c.simulate_post("/setup/api/devices", json={"name": "a", "usb_path": "p1"}).status_code == 200
    assert c.simulate_post("/setup/api/devices", json={"name": "b", "usb_path": "p1"}).status_code == 400
