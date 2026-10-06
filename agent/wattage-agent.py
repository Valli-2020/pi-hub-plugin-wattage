#!/usr/bin/env python3
"""wattage-agent — read-only power exporter for Linux hosts (stdlib only).

Serves JSON on GET /  e.g. {"cpu_w": 7.9, "gpu_w": 0.1, "dram_w": 1.2, "gpus": [...]}.
Sources: Intel RAPL (package / core / uncore = integrated GPU / dram),
NVIDIA (nvidia-smi) and AMD (amdgpu hwmon power1_average).
Usage: wattage-agent.py [--bind IP] [--port 9871]
"""
import argparse
import glob
import json
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

RAPL = "/sys/class/powercap"
_state = {"ts": 0.0}
_lock = threading.Lock()


def _read(path):
    with open(path) as f:
        return f.read().strip()


def rapl_domains():
    out = {}
    for d in glob.glob(RAPL + "/intel-rapl:*"):
        try:
            out[_read(d + "/name")] = (d, int(_read(d + "/max_energy_range_uj")))
        except OSError:
            pass
    return out


def energies(doms):
    res = {}
    for name, (d, _) in doms.items():
        try:
            res[name] = int(_read(d + "/energy_uj"))
        except OSError:
            pass
    return res


def nvidia():
    if not shutil.which("nvidia-smi"):
        return []
    try:
        o = subprocess.run(["nvidia-smi", "--query-gpu=name,power.draw", "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=3).stdout
        return [{"name": n.strip(), "w": float(w)} for n, w in (l.rsplit(",", 1) for l in o.splitlines() if "," in l)]
    except Exception:
        return []


def amd():
    out = []
    for p in glob.glob("/sys/class/drm/card*/device/hwmon/hwmon*/power1_average"):
        try:
            out.append({"name": p.split("/")[4], "w": int(_read(p)) / 1e6})
        except (OSError, ValueError):
            pass
    return out


def sampler(interval=2.0):
    doms = rapl_domains()
    prev, t0 = energies(doms), time.time()
    while True:
        time.sleep(interval)
        cur, t1 = energies(doms), time.time()
        watts = {}
        for name, e1 in cur.items():
            if name in prev:
                delta = e1 - prev[name]
                if delta < 0:
                    delta += doms[name][1]
                watts[name] = round(delta / 1e6 / (t1 - t0), 2)
        prev, t0 = cur, t1
        gpus = nvidia() + amd()
        data = {"ts": t1}
        if "package-0" in watts:
            data["package_w"] = watts["package-0"]
        if "core" in watts:
            data["cpu_w"] = watts["core"]
        if "dram" in watts:
            data["dram_w"] = watts["dram"]
        if gpus:
            data["gpus"] = gpus
            data["gpu_w"] = round(sum(g["w"] for g in gpus), 2)
        elif "uncore" in watts:
            data["gpu_w"] = watts["uncore"]          # integrated GPU
            data["gpus"] = [{"name": "integrated (RAPL uncore)", "w": watts["uncore"]}]
        with _lock:
            _state.clear()
            _state.update(data)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        with _lock:
            body = json.dumps(_state).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=9871)
    a = ap.parse_args()
    threading.Thread(target=sampler, daemon=True).start()
    ThreadingHTTPServer((a.bind, a.port), Handler).serve_forever()
