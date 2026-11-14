"""wattage plugin: JSON path, schema, polling and badges against a stub server.

Run: python3 tests/test_plugin_wattage.py   (also collectable by pytest)
"""

from __future__ import annotations

import http.server
import importlib.util
import json
import sys
import threading

sys.path.insert(0, ".")
import os
CORE = os.environ.get("PIHUB_CORE") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "pi-hub-dev")
sys.path.insert(0, CORE)                     # a Pi Hub core checkout (PIHUB_CORE overrides the sibling guess)

spec = importlib.util.spec_from_file_location("wattage", "pi_hub_plugins/wattage/__init__.py")
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)

FAILED = []


GEN2 = {"switch:0": {"id": 0, "output": True, "apower": 69.3, "voltage": 226.5, "current": 0.58,
                    "aenergy": {"total": 2541733.3}, "temperature": {"tC": 28.5}},
        "sys": {"uptime": 1}, "wifi": {"rssi": -61}}
GEN1 = {"relays": [{"ison": False}], "meters": [{"power": 12.5, "total": 6000}], "temperature": 41.2}


class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/g2/rpc/Shelly.GetStatus", "/g1/status"):
            body = json.dumps(GEN2 if self.path.startswith("/g2") else GEN1).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith(("/g1/", "/g2/", "/auth/")):
            self.send_response(401 if self.path.startswith("/auth/") else 404)
            self.end_headers()
            return
        if self.path == "/redir":
            self.send_response(302)
            self.send_header("Location", "file:///etc/passwd")
            self.end_headers()
            return
        if self.path == "/":
            body = json.dumps({"package_w": 12.8, "gpu_w": 0.0}).encode()
        else:
            body = json.dumps({"meters": [{"power": 42.5}], "gpu_w": 7.25}).encode()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class Ctx:
    def __init__(self, cfg=None):
        self.cfg = cfg or {}

    def get_config(self):
        return self.cfg

    def get_hosts(self):
        return [{"id": "a", "ip": "127.0.0.1"}, {"id": "b", "ip": "127.0.0.1"}, {"id": "c", "ip": ""}]


def make(cfg=None):
    p = w.Wattage()
    ctx = Ctx(cfg)
    p.load(ctx)
    return p, ctx


def test_extract():
    assert w.extract({"a": [{"b": "3.5"}]}, "a.0.b") == 3.5
    for bad in (({"a": 1}, "b"), ({"a": True}, "a"), ({"a": [1]}, "a.5")):
        try:
            w.extract(*bad)
            raise AssertionError("accepted %r" % (bad,))
        except (KeyError, IndexError, ValueError):
            pass


def test_parse_shelly():
    r = w.parse_shelly(GEN2)
    assert r == {"w": 69.3, "wh": 2541733.3, "v": 226.5, "a": 0.58, "temp": 28.5, "on": True}, r
    r = w.parse_shelly(GEN1)
    assert r["w"] == 12.5 and r["wh"] == 100.0 and r["on"] is False and r["temp"] == 41.2, r
    two = {"switch:0": {"apower": 10, "output": False}, "switch:1": {"apower": 5.5, "output": True}}
    assert w.parse_shelly(two)["w"] == 15.5 and w.parse_shelly(two)["on"] is True
    em = {"em:0": {"total_act_power": 230.0, "a_voltage": 231.0}, "emdata:0": {"total_act": 1000.0}}
    assert w.parse_shelly(em)["w"] == 230.0 and w.parse_shelly(em)["wh"] == 1000.0
    for bad in ({}, {"sys": {}}, [], {"switch:0": {"output": True}}):
        try:
            w.parse_shelly(bad)
            raise AssertionError("accepted %r" % (bad,))
        except ValueError:
            pass
    assert w.shelly_base("192.0.2.5") == "http://192.0.2.5"
    assert w.shelly_base(" http://shelly.lan:8080/ ") == "http://shelly.lan:8080"
    for bad in ("", "file:///etc", "ftp://x"):
        try:
            w.shelly_base(bad)
            raise AssertionError("accepted %r" % bad)
        except ValueError:
            pass


def test_shelly_polling_and_panel():
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "127.0.0.1:%d" % srv.server_port
    try:
        p, ctx = make({"agent_port": 1})
        assert p.p_shelly() == {"type": "stack", "children": []}
        ctx.cfg["shellys"] = [{"name": "Homelab", "address": base + "/g2"},
                              {"address": "http://%s/g1" % base, "host_id": "b"},
                              {"name": "Locked", "address": base + "/auth"}]
        p.poll_all()
        assert p._shelly_rpc["http://%s/g2" % base] == "/rpc/Shelly.GetStatus"
        assert p._shelly_rpc["http://%s/g1" % base] == "/status"       # Gen1 found via fallback
        panel = p.p_shelly()
        homelab, gen1, locked = panel["children"]
        st, badge, txt = homelab["children"]
        assert st == {"type": "status", "state": "ok", "text": "Homelab"} and badge["text"] == "69.3 W", homelab
        assert txt["text"].startswith("226.5 V · 0.58 A · 2541.7 kWh · 28 °C · "), txt
        assert gen1["children"][0]["text"] == "http://%s/g1" % base and gen1["children"][0]["state"] == "off"
        assert locked["children"][0]["state"] == "bad" and "password" in locked["children"][2]["text"], locked
        out = p.p_watts()                                              # host id -> card badge
        assert set(out) == {"b"} and out["b"]["text"] == "12.5 W" and "Shelly" in out["b"]["title"], out
        try:
            from pi_hub.plugins import contrib
            contrib.validate_node(panel)
            contrib.validate_keyed(out)
        except ImportError:
            pass
        ctx.cfg["shellys"] = ctx.cfg["shellys"][:1]
        p.poll_all()
        assert set(p._meters) == {base + "/g2"} and p.p_watts() == {}
        assert p.p_shelly()["children"][0]["text"] == "Homelab"         # one meter: no wrapper
        ctx.cfg["warn_w"] = 50
        assert p.p_shelly()["children"][1]["tone"] == "warn"
    finally:
        srv.shutdown()


def test_total_pill_toggle_and_history():
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_port
    base = "127.0.0.1:%d" % port
    try:
        p, ctx = make({"agent_port": port})
        p.poll_all()
        assert p.p_pill() is None, "no mains Shelly, hosts off: no total"
        ctx.cfg["total_hosts"] = True
        pill = p.p_pill()
        assert pill["text"] == "25.6 W" and "2 hosts 26 W" in pill["title"], pill   # 2 agents x 12.8
        ctx.cfg.update(total_hosts=False, shellys=[{"name": "Homelab", "address": base + "/g2"},
                                                   {"address": base + "/g1", "host_id": "b"}])
        p.poll_all()
        pill = p.p_pill()
        assert pill["text"] == "69.3 W" and pill["title"] == "Total power: Homelab 69 W", pill
        ctx.cfg["total_hosts"] = True                         # a = agent 12.8, b = Shelly with host id 12.5
        pill = p.p_pill()
        assert pill["text"] == "94.6 W" and "2 hosts 25 W" in pill["title"], pill
        ctx.cfg["show_pill"] = False
        assert p.p_pill() is None
        ctx.cfg["show_pill"] = True
        try:
            from pi_hub.plugins import contrib
            contrib.validate_node(pill)
        except ImportError:
            pass

        p._record()
        p._record()                                           # within the sample gap: dropped
        assert len(p._history) == 1 and p._history[0]["total"] == 94.6, p._history
        assert p._history[0]["m"] == {base + "/g2": 69.3} and p._history[0]["hosts"] == 25.3
        now = __import__("time").time()
        p._history[:0] = [{"t": now - 7200, "total": 50.0, "hosts": None, "m": {base + "/g2": 50.0}},
                          {"t": now - 1800, "total": 60.0, "hosts": 20.0, "m": {base + "/g2": 40.0}}]
        h = p.r_history({"range": "1h"})
        names = [x["name"] for x in h["series"]]
        assert names == ["Total", "Homelab", "Hosts (sum)"], names       # hosts on: mains shown apart
        tot = [v for _, v in h["series"][0]["points"] if v is not None]
        assert tot == [60.0, 94.6], tot                       # the 2 h old sample is outside 1h
        assert len(p.r_history({"range": "24h"})["series"][0]["points"]) == 3
        assert p.r_history({"range": "bogus"})["range"] == 3600
        ctx.cfg["total_hosts"] = False
        assert [x["name"] for x in p.r_history({"range": "1h"})["series"]] == ["Total", "Hosts (sum)"]
    finally:
        srv.shutdown()


def test_history_persists(tmp=None):
    import tempfile
    d = tempfile.mkdtemp()
    p, ctx = make()
    ctx.data_dir = lambda: d
    p.load(ctx)
    p._history = [{"t": __import__("time").time() - 60, "total": 10.0, "hosts": None, "m": {}},
                  {"t": 1.0, "total": 1.0, "hosts": None, "m": {}}]
    ctx.data_dir = lambda: (_ for _ in ()).throw(RuntimeError("context closed"))   # like a real unload
    p.unload()
    q, ctx2 = make()
    ctx2.data_dir = lambda: d
    q.load(ctx2)
    assert [r["total"] for r in q._history] == [10.0], q._history     # too old dropped on load


def test_frame_and_routes_declared():
    p, _ = make()
    f = p.get_frames()[0]
    assert f.id == "power" and {s["type"] for s in f.surfaces} == {"tab", "widget"}
    js = open(os.path.join("pi_hub_plugins", "wattage", "static", "frame", "power.js")).read()
    assert "if (!TAB) { compact(); return; }" in js            # Hosts widget = slim sparkline row
    root = os.path.join("pi_hub_plugins", "wattage", "static", "frame")
    for name in f.entry + f.css:
        body = open(os.path.join(root, name)).read()
        assert "</script" not in body and "<!--" not in body
    pill = [c for c in p.get_contributions() if c.slot == "header.pill"][0]
    assert pill.opens == "power"
    assert {"ui.frame", "ui.header", "ui.style"} <= set(p.capabilities)
    style = [c for c in p.get_contributions() if c.slot == "style"][0]
    try:
        from pi_hub.plugins import contrib
        out = contrib.validate_style(style.static, "wattage")
        assert ".plg-wattage" in json.dumps(out), out
    except ImportError:
        pass
    man = json.load(open("pi_hub_plugins/wattage/pihub-plugin.json"))
    assert man["version"] == p.version and set(man["capabilities"]) == set(p.capabilities)
    assert json.load(open("pihub-plugin.json")) == man


OLD = {"interval": 15, "timeout": 2, "agent_port": 9871, "warn_w": 0, "bad_w": 0,
       "extra": "a http://192.0.2.5/status meters.0.power gpu_w; b http://192.0.2.6/x p",
       "base": "a 15; c 2.5; junk; d x"}


def test_migrate_old_shape():
    p, _ = make()
    new = p.migrate_config("1.2.0", dict(OLD))
    assert "extra" not in new and "base" not in new
    rows = {r["host_id"]: r for r in new["hosts"]}
    assert rows["a"] == {"host_id": "a", "url": "http://192.0.2.5/status", "path": "meters.0.power",
                         "gpu_path": "gpu_w", "base_w": 15.0}
    assert rows["b"]["url"] == "http://192.0.2.6/x" and rows["b"]["gpu_path"] == ""
    assert rows["c"] == {"host_id": "c", "base_w": 2.5}
    assert "d" not in rows and new["interval"] == 15 and new["agent_port"] == 9871
    assert p.migrate_config("1.3.0", new) == new            # idempotent
    assert p.migrate_config("1.2.0", {"extra": "", "base": ""}) == {}


def test_schema_valid_and_defaults():
    p, _ = make()
    schema = p.get_config_schema()
    names = [f["name"] for f in schema]
    assert len(names) == len(set(names))
    assert set(w.DEFAULTS) <= set(names)
    try:
        from pi_hub.plugins import contrib
    except ImportError:
        print("skip: core contrib not importable")
        return
    if "list" not in getattr(contrib, "CONFIG_FIELD_TYPES", getattr(contrib, "FIELD_TYPES", ())):
        print("skip: core contrib lacks 'list' in FIELD_TYPES (needs 8.3.0)")
        return
    out = contrib.validate_config_schema(schema)
    assert [f["name"] for f in out] == names, "validator dropped fields"
    defaults = {f["name"]: f["default"] for f in schema if "default" in f}
    values, err = contrib.coerce_config_values(out, defaults, {})
    assert err is None, err
    values, err = contrib.coerce_config_values(out, {"hosts": [{"host_id": "a", "url": "http://192.0.2.5/s",
                                                                 "path": "p", "base_w": 3}]}, {})
    assert err is None, err


def test_defaults_when_config_empty():
    p, ctx = make()
    cfg = p._cfg()
    assert cfg["interval"] == 15 and cfg["agent_port"] == 9871 and cfg["hosts"] == {}
    assert p.p_watts() == {} and p.p_gpu() == {}


def test_polling_and_badges():
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_port
    base = "http://127.0.0.1:%d" % port
    try:
        p, ctx = make({"agent_port": port})
        p.poll_all()
        out = p.p_watts()
        assert set(out) == {"a", "b"} and out["a"]["text"] == "CPU 12.8 W", out
        ctx.cfg["hosts"] = [{"host_id": "a", "base_w": 15}, {"host_id": "b", "base_w": 0}]
        out = p.p_watts()
        assert out["a"]["text"] == "≈ 28 W" and out["b"]["text"] == "CPU 12.8 W", out
        ctx.cfg["hosts"] = []
        assert p.p_gpu() == {}                                      # idle iGPU hidden

        ctx.cfg["hosts"] = [{"host_id": "a", "url": base + "/x", "path": "meters.0.power", "gpu_path": "gpu_w"},
                            {"host_id": "b", "url": base + "/redir", "path": "a"}]
        p.poll_all()
        out = p.p_watts()
        assert out["a"]["text"] == "42.5 W", out
        assert p.p_gpu()["a"]["text"] in ("GPU 7.2 W", "GPU 7.3 W"), p.p_gpu()
        assert out["b"]["text"] == "— W" and out["b"]["tone"] == "muted", out
        ctx.cfg["show_gpu"] = False
        assert p.p_gpu() == {}
        ctx.cfg["show_gpu"] = True
        ctx.cfg["gpu_min_w"] = 10
        assert p.p_gpu() == {}
        ctx.cfg["warn_w"] = 40
        assert p.p_watts()["a"]["tone"] == "warn"
        ctx.cfg["bad_w"] = 42
        assert p.p_watts()["a"]["tone"] == "bad"

        ctx.cfg.update(hosts=[], agent_port=1)
        p.poll_all()
        assert p.p_watts() == {}, "detection off drops everything"
        ctx.cfg["agent_port"] = port + 1
        p.poll_all()
        assert p.p_watts() == {}, "no agent = no badge"
        ctx.cfg["hosts"] = [{"host_id": "a", "agent_port": port}]   # per-host port override
        p.poll_all()
        assert set(p.p_watts()) == {"a"}
    finally:
        srv.shutdown()


def test_on_config_change_and_live_interval():
    p, ctx = make({"interval": 3600})
    calls = []
    p.poll_all = lambda: calls.append(1)
    p._tick()
    assert len(calls) == 1
    p._tick()
    assert len(calls) == 1                       # interval not yet passed
    ctx.cfg["interval"] = 5                      # live change: still not due
    p._tick()
    assert len(calls) == 1
    p.on_config_change({}, ctx.cfg)              # settings saved: poll on the next tick
    p._tick()
    assert len(calls) == 2
    assert p.get_tasks()[0].interval == 1.0


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("  ok  " + name)
            except Exception as e:                # noqa: BLE001
                import traceback
                traceback.print_exc()
                FAILED.append(name)
    print("FAILED: %s" % FAILED if FAILED else "all passed")
    sys.exit(1 if FAILED else 0)
