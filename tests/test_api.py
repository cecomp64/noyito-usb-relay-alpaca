import tempfile, textwrap
from pathlib import Path
from falcon import testing
from noyito_alpaca.config import load_config
from noyito_alpaca.server import build_app

FORM = {"Content-Type": "application/x-www-form-urlencoded"}

def client():
    d = Path(tempfile.mkdtemp())
    (d / "c.toml").write_text("[server]\ndiscovery = false\n" + "".join(f'[[device]]\nname="M{i}"\nmock=true\n' for i in range(4)))
    app, _ = build_app(load_config(d / "c.toml"))
    return testing.TestClient(app)

def test_four_devices_independent():
    c = client()
    assert len(c.simulate_get("/management/v1/configureddevices").json["Value"]) == 4
    for n in range(4):
        c.simulate_put(f"/api/v1/switch/{n}/connected", body="Connected=true", headers=FORM)
    c.simulate_put("/api/v1/switch/2/setswitch", body="Id=1&State=True", headers=FORM)
    vals = [c.simulate_get(f"/api/v1/switch/{n}/getswitch", params={"Id": 1}).json["Value"] for n in range(4)]
    assert vals == [False, False, True, False]

def test_errors():
    c = client()
    assert c.simulate_get("/api/v1/switch/0/getswitch", params={"Id": 0}).json["ErrorNumber"] == 0x407
    assert c.simulate_get("/api/v1/switch/0/getswitch").status_code == 400
    assert c.simulate_get("/api/v1/switch/9/maxswitch").status_code == 404
    assert c.simulate_get("/api/v1/switch/0/getswitchname", params={"Id": 99}).json["ErrorNumber"] == 0x401
