"""wattage — show each host's current power draw on its card.

Hosts that run the bundled wattage-agent are found automatically (it answers
on a fixed port).  Per-host rows in the "Hosts" setting add another JSON
source (smart plug, Home Assistant), a base load or a different agent port.
Shelly meters (Gen1 and Gen2+) are read natively: each shows a panel above
the host cards (power, voltage, current, energy, relay) and, with a host id,
the badge of that host card.
A background task polls everything; contributions render the result.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from pi_hub.plugins.base import Contribution, Plugin, TaskDef

_MAX_BODY = 64 * 1024


def extract(data: Any, path: str) -> float:
    """Walk a dotted path (dict keys, list indices) and return a float."""
    cur = data
    for part in [p for p in path.split(".") if p]:
        if isinstance(cur, list):
            cur = cur[int(part)]
        elif isinstance(cur, dict):
            cur = cur[part]
        else:
            raise KeyError(part)
    if isinstance(cur, bool):
        raise ValueError("not a number")
    return float(cur)


DEFAULTS: dict[str, Any] = {
    "agent_port": 9871, "show_gpu": True, "gpu_min_w": 0.1, "interval": 15, "timeout": 2,
    "stale_polls": 3, "warn_w": 0, "bad_w": 0, "hosts": [], "shellys": [],
}


def _f(v: Any) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


_GEN2_PREFIXES = ("switch:", "pm1:", "em1:", "cover:", "light:")


def parse_shelly(data: Any) -> dict[str, Any]:
    """Shelly status JSON (Gen2+ ``Shelly.GetStatus`` or Gen1 ``/status``) -> one reading.

    Power and energy are summed over all metered channels; voltage, current
    and temperature come from the first channel that has them.  ``wh`` is the
    lifetime energy counter in Wh, ``on`` whether any relay is on (None when
    the device has no relay).  Raises ValueError when no power value exists.
    """
    if not isinstance(data, dict):
        raise ValueError("not a Shelly status object")
    w = wh = v = a = temp = None
    on: bool | None = None

    def add(cur, x):
        return x if cur is None else (cur if x is None else cur + x)

    def first(cur, x):
        return cur if cur is not None else x

    for key in sorted(data):
        ch = data[key]
        if not isinstance(ch, dict):
            continue
        if key.startswith(_GEN2_PREFIXES):
            w = add(w, _f(ch.get("apower", ch.get("act_power"))))
            if isinstance(ch.get("aenergy"), dict):
                wh = add(wh, _f(ch["aenergy"].get("total")))
            v, a = first(v, _f(ch.get("voltage"))), first(a, _f(ch.get("current")))
            if isinstance(ch.get("temperature"), dict):
                temp = first(temp, _f(ch["temperature"].get("tC")))
            if isinstance(ch.get("output"), bool):
                on = bool(on) or ch["output"]
        elif key.startswith("em:"):                       # 3-phase Pro 3EM
            w = add(w, _f(ch.get("total_act_power")))
            a = first(a, _f(ch.get("total_current")))
            v = first(v, _f(ch.get("a_voltage")))
        elif key.startswith("emdata:"):
            wh = add(wh, _f(ch.get("total_act")))
    if w is None:                                         # Gen1
        for m in data.get("meters") or []:
            if isinstance(m, dict):
                w = add(w, _f(m.get("power")))
                t = _f(m.get("total"))                    # watt-minutes
                wh = add(wh, t / 60 if t is not None else None)
        for m in data.get("emeters") or []:
            if isinstance(m, dict):
                w = add(w, _f(m.get("power")))
                wh = add(wh, _f(m.get("total")))
                v, a = first(v, _f(m.get("voltage"))), first(a, _f(m.get("current")))
        for r in data.get("relays") or []:
            if isinstance(r, dict) and isinstance(r.get("ison"), bool):
                on = bool(on) or r["ison"]
        v = first(v, _f(data.get("voltage")))
        tmp = data.get("tmp")
        temp = first(_f(data.get("temperature")), _f(tmp.get("tC")) if isinstance(tmp, dict) else None)
    if w is None:
        raise ValueError("no power reading in the Shelly status")
    return {"w": w, "wh": wh, "v": v, "a": a, "temp": temp, "on": on}


def shelly_base(address: str) -> str:
    """'192.168.1.5' / 'http://shelly.lan/' -> 'http://192.168.1.5' (no trailing slash)."""
    address = str(address or "").strip()
    if not address:
        raise ValueError("no address")
    if "://" not in address:
        address = "http://" + address
    parts = urllib.parse.urlsplit(address)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError("address must be an IP, host name or http(s) URL")
    return "%s://%s%s" % (parts.scheme, parts.netloc, parts.path.rstrip("/"))


def _num(value: Any, default: float) -> float:
    try:
        if isinstance(value, bool):
            return float(default)
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def migrate_hosts(config: dict) -> dict:
    """Old ``extra`` ("id url path [gpu]; ...") and ``base`` ("id watts; ...") strings -> ``hosts`` rows."""
    config = dict(config)
    rows: dict[str, dict[str, Any]] = {}
    for entry in str(config.pop("extra", "") or "").split(";"):
        parts = entry.split()
        if len(parts) in (3, 4):
            rows.setdefault(parts[0], {"host_id": parts[0]}).update(
                url=parts[1], path=parts[2], gpu_path=parts[3] if len(parts) == 4 else "")
    for entry in str(config.pop("base", "") or "").split(";"):
        parts = entry.split()
        if len(parts) == 2:
            try:
                rows.setdefault(parts[0], {"host_id": parts[0]})["base_w"] = max(0.0, float(parts[1]))
            except ValueError:
                pass
    if rows and not config.get("hosts"):
        config["hosts"] = list(rows.values())
    return config


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


class Wattage(Plugin):
    name = "wattage"
    version = "1.4.0"
    description = "Current power draw (W) and GPU power on each host card, Shelly meters above them"
    min_core_version = "8.3.0"
    plugin_api_version = 2
    capabilities = ["hosts.read", "ui.slots"]

    def load(self, ctx) -> None:
        self.ctx = ctx
        self._lock = threading.Lock()
        self._data: dict[str, dict[str, Any]] = {}   # host id -> {w, gpu, ts, err}
        self._meters: dict[str, dict[str, Any]] = {}  # shelly row key -> reading
        self._shelly_rpc: dict[str, str] = {}         # base url -> status path that worked
        self._last_poll = 0.0

    # ── settings ─────────────────────────────────────────────────────────

    def migrate_config(self, old_version: str, config: dict) -> dict:
        return migrate_hosts(config)

    def on_config_change(self, old: dict, new: dict) -> None:
        self._last_poll = 0.0          # the next tick polls with the new settings

    def _cfg(self) -> dict[str, Any]:
        raw = self.ctx.get_config() or {}
        cfg = {k: raw.get(k, v) for k, v in DEFAULTS.items()}
        cfg["interval"] = max(1.0, _num(cfg["interval"], 15))
        cfg["timeout"] = max(0.1, _num(cfg["timeout"], 2))
        cfg["stale_polls"] = max(1.0, _num(cfg["stale_polls"], 3))
        cfg["agent_port"] = int(_num(cfg["agent_port"], 9871))
        for k in ("warn_w", "bad_w", "gpu_min_w"):
            cfg[k] = _num(cfg[k], DEFAULTS[k])
        rows = cfg["hosts"] if isinstance(cfg["hosts"], list) else []
        cfg["hosts"] = {str(r["host_id"]).strip(): r for r in rows
                        if isinstance(r, dict) and str(r.get("host_id") or "").strip()}
        shellys: dict[str, dict[str, Any]] = {}
        for r in cfg["shellys"] if isinstance(cfg["shellys"], list) else []:
            if isinstance(r, dict) and str(r.get("address") or "").strip():
                addr = str(r["address"]).strip()
                shellys.setdefault(addr, {"address": addr, "name": str(r.get("name") or "").strip() or addr,
                                          "host_id": str(r.get("host_id") or "").strip()})
        cfg["shellys"] = shellys
        return cfg

    def get_config_schema(self):
        return [
            {"name": "agent_port", "label": "Host agent port", "type": "number", "min": 1, "max": 65535,
             "default": 9871, "section": "Detection",
             "help": "Hosts running wattage-agent are detected on this port. Set to 1 to switch detection off."},
            {"name": "hosts", "label": "Per-host settings", "type": "list", "section": "Hosts", "max_items": 50,
             "help": "Optional. Host id = id of the host card. A URL adds a JSON source (wins over the agent); "
                     "base load is added to CPU-only agent readings; agent port overrides the default.",
             "fields": [
                 {"name": "host_id", "label": "Host id", "type": "text", "required": True},
                 {"name": "url", "label": "Source URL", "type": "url"},
                 {"name": "path", "label": "Power JSON path", "type": "text", "placeholder": "meters.0.power"},
                 {"name": "gpu_path", "label": "GPU JSON path", "type": "text"},
                 {"name": "base_w", "label": "Base load (W)", "type": "number", "min": 0, "max": 100000},
                 {"name": "agent_port", "label": "Agent port", "type": "number", "min": 0, "max": 65535},
             ]},
            {"name": "shellys", "label": "Shelly meters", "type": "list", "section": "Shelly", "max_items": 20,
             "help": "Shelly plugs and relays with power metering (Gen1 and Gen2+, no device password). "
                     "Each shows a panel above the host cards. With a host id its reading is also that "
                     "host's badge (wins over a source URL and the agent).",
             "fields": [
                 {"name": "name", "label": "Name", "type": "text", "placeholder": "Homelab"},
                 {"name": "address", "label": "IP or URL", "type": "text", "required": True,
                  "placeholder": "192.0.2.10"},
                 {"name": "host_id", "label": "Host id", "type": "text"},
             ]},
            {"name": "show_gpu", "label": "Show GPU badge", "type": "checkbox", "default": True,
             "section": "Badges"},
            {"name": "gpu_min_w", "label": "Hide GPU badge below (W)", "type": "number", "min": 0,
             "default": 0.1, "section": "Badges",
             "help": "Keeps idle integrated GPUs quiet."},
            {"name": "warn_w", "label": "Warn above (W)", "type": "number", "min": 0, "default": 0,
             "section": "Badges", "help": "0 = no colour change."},
            {"name": "bad_w", "label": "Bad above (W)", "type": "number", "min": 0, "default": 0,
             "section": "Badges", "help": "0 = no colour change."},
            {"name": "interval", "label": "Poll interval (s)", "type": "number", "min": 5, "max": 3600,
             "default": 15, "section": "Polling"},
            {"name": "timeout", "label": "Timeout (s)", "type": "number", "min": 0.5, "max": 10,
             "default": 2, "section": "Polling"},
            {"name": "stale_polls", "label": "Stale after (polls)", "type": "number", "min": 1, "max": 20,
             "default": 3, "section": "Polling",
             "help": "A reading older than this many poll intervals shows as a muted '— W'."},
        ]

    # ── polling ──────────────────────────────────────────────────────────

    def _fetch(self, url: str, paths: list[str], timeout: float) -> list[float | None]:
        if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
            raise ValueError("URL must be http(s)")
        data = self._get_json(url, timeout)
        out: list[float | None] = []
        for i, p in enumerate(paths):
            try:
                out.append(extract(data, p) if p else None)
            except (KeyError, IndexError, ValueError, TypeError):
                if i == 0:
                    raise
                out.append(None)          # optional GPU path: absent on hosts without a GPU
        return out

    def _poll_one(self, host_id: str, url: str, path: str, gpu_path: str, timeout: float, auto: bool) -> None:
        try:
            w, gpu = self._fetch(url, [path, gpu_path], timeout)
            res = {"w": w, "gpu": gpu, "ts": time.time(), "err": "", "cpu_only": auto}
        except Exception as e:
            if auto:                      # no agent on this host: not an error, just nothing to show
                with self._lock:
                    self._data.pop(host_id, None)
                return
            err = "HTTP %d" % e.code if isinstance(e, urllib.error.HTTPError) else "%s: %s" % (type(e).__name__, e)
            res = {"w": None, "gpu": None, "ts": time.time(), "err": err}
        with self._lock:
            self._data[host_id] = res

    def _get_json(self, url: str, timeout: float) -> Any:
        with _opener.open(url, timeout=timeout) as r:
            body = r.read(_MAX_BODY + 1)
        if len(body) > _MAX_BODY:
            raise ValueError("response too large")
        return json.loads(body)

    def _poll_shelly(self, addr: str, timeout: float) -> None:
        """Gen2+ answers /rpc/Shelly.GetStatus, Gen1 /status; the path that worked is remembered."""
        try:
            base = shelly_base(addr)
            known = self._shelly_rpc.get(base)
            paths = [known] if known else ["/rpc/Shelly.GetStatus", "/status"]
            last: Exception | None = None
            for path in paths:
                try:
                    reading = parse_shelly(self._get_json(base + path, timeout))
                    self._shelly_rpc[base] = path
                    break
                except urllib.error.HTTPError as e:
                    last = e
                    if e.code == 401:
                        raise
                except ValueError as e:
                    last = e
            else:
                self._shelly_rpc.pop(base, None)
                raise last or ValueError("no answer")
            res = dict(reading, ts=time.time(), err="")
        except Exception as e:
            if isinstance(e, urllib.error.HTTPError):
                err = "HTTP 401: device password not supported" if e.code == 401 else "HTTP %d" % e.code
            else:
                err = "%s: %s" % (type(e).__name__, e)
            res = {"w": None, "ts": time.time(), "err": err}
        with self._lock:
            self._meters[addr] = res

    def poll_all(self) -> None:
        cfg = self._cfg()
        hosts_cfg = cfg["hosts"]
        shellys = cfg["shellys"]
        shelly_hosts = {r["host_id"] for r in shellys.values() if r["host_id"]}
        jobs = [(hid, r["url"], str(r.get("path") or ""), str(r.get("gpu_path") or ""), False)
                for hid, r in hosts_cfg.items() if r.get("url") and r.get("path") and hid not in shelly_hosts]
        extra = {j[0] for j in jobs} | shelly_hosts
        default_port = cfg["agent_port"]
        for h in self.ctx.get_hosts():
            hid = h.get("id")
            if not hid or not h.get("ip") or hid in extra:
                continue
            port = int(_num(hosts_cfg.get(hid, {}).get("agent_port"), 0)) or default_port
            if port > 1:
                jobs.append((hid, "http://%s:%d/" % (h["ip"], port), "package_w", "gpu_w", True))
        known = {j[0] for j in jobs}
        with self._lock:
            for hid in list(self._data):
                if hid not in known:
                    del self._data[hid]
            for addr in list(self._meters):
                if addr not in shellys:
                    del self._meters[addr]
        if not jobs and not shellys:
            return
        with ThreadPoolExecutor(max_workers=min(8, len(jobs) + len(shellys))) as ex:
            for hid, url, path, gpu, auto in jobs:
                ex.submit(self._poll_one, hid, url, path, gpu, cfg["timeout"], auto)
            for addr in shellys:
                ex.submit(self._poll_shelly, addr, cfg["timeout"])

    def _tick(self) -> None:
        """Runs every second; polls when the (live) interval has passed."""
        if time.time() - self._last_poll >= self._cfg()["interval"]:
            self._last_poll = time.time()
            self.poll_all()

    def get_tasks(self):
        return [TaskDef("poll", self._tick, interval=1.0)]

    # ── UI ───────────────────────────────────────────────────────────────

    def get_contributions(self):
        return [Contribution("hosts.card.badges", "watts", self.p_watts, poll=15, order=30),
                Contribution("hosts.card.badges", "gpu", self.p_gpu, poll=15, order=31),
                Contribution("hosts.top", "shelly", self.p_shelly, poll=15, order=30)]

    def _tone(self, w: float) -> str:
        cfg = self._cfg()
        bad, warn = cfg["bad_w"], cfg["warn_w"]
        if bad and w >= bad:
            return "bad"
        if warn and w >= warn:
            return "warn"
        return "info"

    def _snapshot(self):
        cfg = self._cfg()
        stale = cfg["stale_polls"] * cfg["interval"]
        now = time.time()
        with self._lock:
            return now, stale, dict(self._data), dict(self._meters)

    def p_watts(self, session=None):
        now, stale, snap, meters = self._snapshot()
        cfg = self._cfg()
        hosts_cfg = cfg["hosts"]
        for addr, row in cfg["shellys"].items():          # a Shelly with a host id is that host's badge
            m = meters.get(addr)
            if row["host_id"] and m:
                snap[row["host_id"]] = {"w": m["w"], "gpu": None, "ts": m["ts"], "err": m["err"],
                                        "shelly": row["name"]}
        base = {hid: max(0.0, _num(r.get("base_w"), 0)) for hid, r in hosts_cfg.items() if r.get("base_w")}
        out = {}
        for hid, r in snap.items():
            age = now - r["ts"]
            if r["w"] is None or age > stale:
                out[hid] = {"type": "badge", "text": "— W", "tone": "muted",
                            "title": r["err"] or "no recent reading"}
                continue
            w, cpu_only = r["w"], r.get("cpu_only")
            fmt = "%.1f" if w < 100 else "%.0f"
            if cpu_only and hid in base:
                total = w + base[hid]
                text = "≈ %.0f W" % total
                title = "Estimate: CPU %.1f W + base load %.0f W, updated %d s ago" % (w, base[hid], age)
                w = total
            elif cpu_only:
                text = ("CPU " + fmt + " W") % w
                title = "CPU package only (RAPL): excludes board, disks, fans and PSU losses. Set a base load to estimate the whole device."
            elif r.get("shelly"):
                text = (fmt + " W") % w
                title = "Shelly %s, updated %d s ago" % (r["shelly"], age)
            else:
                text = (fmt + " W") % w
                title = "Power draw, updated %d s ago" % age
            out[hid] = {"type": "badge", "text": text, "tone": self._tone(w), "title": title}
        return out

    def p_gpu(self, session=None):
        now, stale, snap, _ = self._snapshot()
        cfg = self._cfg()
        if not cfg["show_gpu"]:
            return {}
        gpu_min = cfg["gpu_min_w"]
        return {hid: {"type": "badge", "text": "GPU %.1f W" % r["gpu"], "tone": "info",
                      "title": "GPU power draw, updated %d s ago" % (now - r["ts"])}
                for hid, r in snap.items()
                if r["w"] is not None and r["gpu"] is not None and r["gpu"] >= gpu_min and now - r["ts"] <= stale}

    def p_shelly(self, session=None):
        """One panel per Shelly above the host cards; nothing when none is configured."""
        now, stale, _, meters = self._snapshot()
        rows = self._cfg()["shellys"]
        if not rows:
            return {"type": "stack", "children": []}
        panels = []
        for addr, row in rows.items():
            m = meters.get(addr)
            if not m:
                panels.append({"type": "section", "title": row["name"],
                               "children": [{"type": "text", "text": "Waiting for the first reading…",
                                             "tone": "muted"}]})
                continue
            age = now - m["ts"]
            if m["w"] is None or age > stale:
                panels.append({"type": "section", "title": row["name"], "children": [
                    {"type": "badge", "text": "— W", "tone": "muted",
                     "title": m["err"] or "no recent reading"},
                    {"type": "text", "text": m["err"] or "No reading for %d s" % age, "tone": "muted"}]})
                continue
            w = m["w"]
            stats = [{"type": "stat", "label": "Power", "value": ("%.1f" if w < 100 else "%.0f") % w,
                      "unit": "W", "tone": "" if self._tone(w) == "info" else self._tone(w)}]
            if m.get("v") is not None:
                stats.append({"type": "stat", "label": "Voltage", "value": "%.1f" % m["v"], "unit": "V"})
            if m.get("a") is not None:
                stats.append({"type": "stat", "label": "Current", "value": "%.2f" % m["a"], "unit": "A"})
            if m.get("wh") is not None:
                stats.append({"type": "stat", "label": "Energy total", "value": "%.1f" % (m["wh"] / 1000),
                              "unit": "kWh"})
            if m.get("temp") is not None:
                stats.append({"type": "stat", "label": "Device temp", "value": "%.0f" % m["temp"], "unit": "°C"})
            head = []
            if m.get("on") is not None:
                head.append({"type": "status", "state": "ok" if m["on"] else "off",
                             "text": "Relay on" if m["on"] else "Relay off"})
            head.append({"type": "text", "text": "updated %d s ago" % age, "tone": "muted"})
            panels.append({"type": "section", "title": row["name"], "children": [
                {"type": "stack", "dir": "row", "children": head},
                {"type": "columns", "children": stats}]})
        return panels[0] if len(panels) == 1 else {"type": "stack", "children": panels}
