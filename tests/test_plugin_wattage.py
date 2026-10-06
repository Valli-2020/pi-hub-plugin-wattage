"""wattage plugin: JSON path, schema, polling and badges against a stub server.

Run: python3 tests/test_plugin_wattage.py
"""

from __future__ import annotations

import http.server
import importlib.util
import json
import sys
import threading

sys.path.insert(0, ".")

spec = importlib.util.spec_from_file_location("wattage", "pi_hub_plugins/wattage/__init__.py")
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)

FAILED = []


def check(name, cond, detail=""):
    print(("  ok  " if cond else "FAIL  ") + name + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


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


srv = http.server.HTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
port = srv.server_port
base = "http://127.0.0.1:%d" % port


class Ctx:
    def __init__(self):
        self.cfg = {}

    def get_config(self):
        return self.cfg

    def get_hosts(self):
        return [{"id": "a", "ip": "127.0.0.1"}, {"id": "b", "ip": "127.0.0.1"}, {"id": "c", "ip": ""}]


print("extract")
check("dict+list path", w.extract({"a": [{"b": "3.5"}]}, "a.0.b") == 3.5)
for bad in (({"a": 1}, "b"), ({"a": True}, "a"), ({"a": [1]}, "a.5")):
    try:
        w.extract(*bad)
        check("bad path rejected %r" % (bad,), False)
    except (KeyError, IndexError, ValueError):
        check("bad path rejected %r" % (bad,), True)

print("parse_extra")
check("entries", w.parse_extra("a http://x p; b http://y q g") == {"a": ("http://x", "p", ""), "b": ("http://y", "q", "g")})
check("junk ignored", w.parse_extra("a b; ;") == {})

print("plugin")
p = w.Wattage()
ctx = Ctx()
p.load(ctx)
check("static schema, no host names", [f["name"] for f in p.get_config_schema()][:2] == ["agent_port", "extra"])

ctx.cfg["agent_port"] = port
p.poll_all()
out = p.p_watts()
check("agent auto-detected on hosts with an ip", set(out) == {"a", "b"} and out["a"]["text"] == "12.8 W", str(out))
check("idle integrated gpu hidden", p.p_gpu() == {})

ctx.cfg["extra"] = "a %s/x meters.0.power gpu_w; b %s/redir a" % (base, base)
p.poll_all()
out = p.p_watts()
check("extra source overrides agent", out["a"]["text"] == "42.5 W", str(out))
check("gpu badge", p.p_gpu()["a"]["text"] == "GPU 7.2 W" or p.p_gpu()["a"]["text"] == "GPU 7.3 W", str(p.p_gpu()))
check("redirect refused", out["b"]["text"] == "— W" and out["b"]["tone"] == "muted", str(out))
ctx.cfg["warn_w"] = 40
check("warn tone", p.p_watts()["a"]["tone"] == "warn")

ctx.cfg["extra"] = ""
ctx.cfg["agent_port"] = 1 
p.poll_all()
check("detection off drops everything", p.p_watts() == {})
ctx.cfg["agent_port"] = port + 1
p.poll_all()
check("no agent = no badge", p.p_watts() == {})
srv.shutdown()

print("FAILED: %s" % FAILED if FAILED else "all passed")
sys.exit(1 if FAILED else 0)
