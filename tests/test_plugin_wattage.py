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


class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
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
