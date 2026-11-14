"""wattage — show each host's current power draw on its card.

Hosts that run the bundled wattage-agent are found automatically (it answers
on a fixed port).  Per-host rows in the "Hosts" setting add another JSON
source (smart plug, Home Assistant), a base load or a different agent port.
Shelly meters (Gen1 and Gen2+) are read natively: each shows a panel above
the host cards (power, voltage, current, energy, relay) and, with a host id,
the badge of that host card.
A header pill shows the total (mains Shellys, optionally plus the host
readings) and opens the Power tab, a sandboxed frame with the power graph
(also shown on the Hosts view).  The history (24 h) is kept in data/.
A background task polls everything; contributions render the result.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from pi_hub.plugins.base import Contribution, FrameDef, Plugin, RouteDef, TaskDef

_MAX_BODY = 64 * 1024
HISTORY_S = 24 * 3600          # graph history kept
SAMPLE_GAP_S = 14.5            # at most one sample per ~15 s (24 h ≈ 5760 samples)
SAVE_EVERY_S = 300             # history written to data/ this often
RANGES = {"1h": 3600, "6h": 6 * 3600, "24h": 24 * 3600}
MAX_POINTS = 360               # points per series sent to the graph


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
    "show_pill": True, "total_hosts": False, "price_kwh": 0, "currency": "€",
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


BOLT_SVG = ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
            'stroke-linecap="round" stroke-linejoin="round"><path d="M13 2 4 14h7l-1 8 9-12h-7l1-8z"/></svg>')


# Scoped by the core to .plg-wattage, so it only reaches this plugin's nodes.
# Core health pills (.hpill) are 26 px with a status dot; plugin badges are
# 20 px.  The total pill takes the health-pill look: same height, padding,
# font and border, an accent dot in front (warn/bad colour it like .hpill.bad).
PILL_CSS = (
    '[data-slot="header.pill"] & .ph-badge { height:26px; padding:0 10px 0 9px; gap:6px; '
    'font-size:12px; font-weight:600; letter-spacing:0; font-variant-numeric:tabular-nums; '
    'color:var(--text); background:var(--surface); border-color:var(--border); }\n'
    '[data-slot="header.pill"] & .ph-badge::before { content:""; flex:none; width:6px; height:6px; '
    'border-radius:50%; background:var(--accent); }\n'
    '[data-slot="header.pill"] & .ph-badge[data-tone="warn"] { color:var(--warn); '
    'border-color:color-mix(in oklab, var(--warn) 45%, var(--border)); '
    'background:color-mix(in oklab, var(--warn) 8%, var(--surface)); }\n'
    '[data-slot="header.pill"] & .ph-badge[data-tone="warn"]::before { background:var(--warn); }\n'
    '[data-slot="header.pill"] & .ph-badge[data-tone="bad"] { color:var(--bad); '
    'border-color:color-mix(in oklab, var(--bad) 45%, var(--border)); '
    'background:color-mix(in oklab, var(--bad) 8%, var(--surface)); }\n'
    '[data-slot="header.pill"] & .ph-badge[data-tone="bad"]::before { background:var(--bad); }\n'
)


class Wattage(Plugin):
    name = "wattage"
    version = "1.6.0"
    description = "Current power draw (W) and GPU power on each host card, Shelly meters above them"
    min_core_version = "8.3.0"
    plugin_api_version = 2
    capabilities = ["hosts.read", "ui.frame", "ui.header", "ui.slots", "ui.style"]

    def load(self, ctx) -> None:
        self.ctx = ctx
        self._lock = threading.Lock()
        self._data: dict[str, dict[str, Any]] = {}   # host id -> {w, gpu, ts, err}
        self._meters: dict[str, dict[str, Any]] = {}  # shelly row key -> reading
        self._shelly_rpc: dict[str, str] = {}         # base url -> status path that worked
        self._last_poll = 0.0
        self._history: list[dict[str, Any]] = []      # {t, total, hosts, m: {addr: w}}
        self._last_save = time.time()
        try:                              # resolved now: at unload the context is already closed
            self._hist_path: str | None = os.path.join(ctx.data_dir(), "history.json")
        except Exception:                 # no data dir (old core, test context)
            self._hist_path = None
        self._load_history()

    def unload(self) -> None:
        self._save_history()

    # ── history ──────────────────────────────────────────────────────────

    def _history_path(self) -> str | None:
        return self._hist_path

    def _load_history(self) -> None:
        path = self._history_path()
        if not path:
            return
        try:
            with open(path) as f:
                rows = json.load(f)
        except (OSError, ValueError):
            return
        cutoff = time.time() - HISTORY_S
        if isinstance(rows, list):
            self._history = [r for r in rows if isinstance(r, dict) and _num(r.get("t"), 0) >= cutoff]

    def _save_history(self) -> None:
        path = self._history_path()
        if not path:
            return
        with self._lock:
            rows = list(self._history)
        tmp = path + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(rows, f, separators=(",", ":"))
            os.replace(tmp, path)
        except OSError:
            pass
        self._last_save = time.time()

    def _record(self) -> None:
        """After a poll: append one sample (total, hosts sum, each Shelly) to the history."""
        now = time.time()
        t = self._totals()
        sample = {"t": round(now), "total": t["total"], "hosts": t["hosts"],
                  "m": {a: m["w"] for a, m in t["mains"].items()},
                  "wh": {a: m["wh"] for a, m in t["mains"].items() if m.get("wh") is not None}}
        with self._lock:
            if self._history and now - self._history[-1]["t"] < SAMPLE_GAP_S:
                return
            self._history.append(sample)
            cutoff = now - HISTORY_S
            while self._history and self._history[0]["t"] < cutoff:
                self._history.pop(0)
        if now - self._last_save >= SAVE_EVERY_S:
            self._save_history()

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
        cfg["currency"] = str(cfg["currency"] or "")[:8]
        for k in ("warn_w", "bad_w", "gpu_min_w", "price_kwh"):
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
            {"name": "show_pill", "label": "Show total in the header", "type": "checkbox", "default": True,
             "section": "Total",
             "help": "A pill next to the health chips with the total power; a click opens the Power tab."},
            {"name": "total_hosts", "label": "Add host readings to the total", "type": "checkbox",
             "default": False, "section": "Total",
             "help": "Off: the total is the Shelly meters without a host id (e.g. the one feeding the whole "
                     "homelab). On: the host badges are added too — leave it off when the hosts already "
                     "hang behind that Shelly, or they are counted twice."},
            {"name": "price_kwh", "label": "Energy price per kWh", "type": "number", "min": 0, "max": 100,
             "default": 0, "section": "Total",
             "help": "For the cost figures in the Power tab, e.g. 0.32. 0 hides the costs."},
            {"name": "currency", "label": "Currency", "type": "text", "default": "€", "section": "Total"},
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
            self._record()

    def get_tasks(self):
        return [TaskDef("poll", self._tick, interval=1.0)]

    # ── UI ───────────────────────────────────────────────────────────────

    def get_contributions(self):
        return [Contribution("hosts.card.badges", "watts", self.p_watts, poll=15, order=30),
                Contribution("hosts.card.badges", "gpu", self.p_gpu, poll=15, order=31),
                Contribution("hosts.top", "shelly", self.p_shelly, poll=15, order=30),
                Contribution("header.pill", "total", self.p_pill, poll=15, order=30, opens="power"),
                Contribution("style", "pill", static={"css": PILL_CSS})]

    def get_frames(self):
        return [FrameDef("power", entry=["power.js"], css=["power.css"],
                         surfaces=[{"type": "tab", "label": "Power", "icon_svg": BOLT_SVG},
                                   {"type": "widget", "view": "hosts"}],
                         height=44)]

    def get_routes(self):
        return [RouteDef("GET", "/history/{range}", self.r_history),
                RouteDef("GET", "/details", self.r_details)]

    def p_pill(self, session=None):
        if not self._cfg()["show_pill"]:
            return None
        t = self._totals()
        if t["total"] is None:
            return None
        parts = ["%s %.0f W" % (m["name"], m["w"]) for m in t["mains"].values()]
        if t["with_hosts"] and t["hosts"] is not None:
            parts.append("%d host%s %.0f W" % (t["host_n"], "" if t["host_n"] == 1 else "s", t["hosts"]))
        w = t["total"]
        return {"type": "badge", "text": (("%.1f" if w < 100 else "%.0f") % w) + " W",
                "tone": self._tone(w) if self._tone(w) != "info" else "accent",
                "title": "Total power: " + " + ".join(parts)}

    def r_history(self, params):
        """Graph data for one range, averaged down to at most MAX_POINTS per series."""
        span = RANGES.get(str(params.get("range")), RANGES["1h"])
        cfg = self._cfg()
        now = time.time()
        with self._lock:
            rows = [r for r in self._history if r["t"] >= now - span]
        mains = {a: r["name"] for a, r in cfg["shellys"].items() if not r["host_id"]}
        bucket = max(span / MAX_POINTS, SAMPLE_GAP_S)
        keys = ["total", "hosts"] + ["m:" + a for a in mains]
        acc: dict[int, dict[str, list[float]]] = {}
        for r in rows:
            b = acc.setdefault(int((r["t"] - (now - span)) // bucket), {})
            for k in keys:
                v = r["m"].get(k[2:]) if k.startswith("m:") else r.get(k)
                if v is not None:
                    b.setdefault(k, []).append(float(v))
        idx = sorted(acc)
        t0 = now - span
        series = {k: [(round(t0 + (i + 0.5) * bucket),
                       round(sum(acc[i][k]) / len(acc[i][k]), 1) if acc[i].get(k) else None) for i in idx]
                  for k in keys}
        out = [{"key": "total", "name": "Total", "points": series["total"]}]
        if len(mains) > 1 or cfg["total_hosts"]:
            out += [{"key": "m:" + a, "name": n, "points": series["m:" + a]} for a, n in mains.items()]
        if any(v is not None for _, v in series["hosts"]):
            out.append({"key": "hosts", "name": "Hosts (sum)", "points": series["hosts"]})
        out = [s for s in out if any(v is not None for _, v in s["points"])]
        return {"range": span, "now": round(now), "with_hosts": bool(cfg["total_hosts"]),
                "warn_w": cfg["warn_w"], "bad_w": cfg["bad_w"], "series": out,
                "stats": self._stats(rows, list(mains), cfg)}

    def _stats(self, rows, mains: list[str], cfg) -> dict[str, Any]:
        """Now/avg/min/max of the total, energy in the range and a per-day projection.

        Energy: the Shelly lifetime counters (exact) when every mains meter has
        one at both ends and hosts are not added; else the integral of the
        total over the samples (gaps longer than 3 poll intervals skipped).
        """
        pts = [(r["t"], r["total"]) for r in rows if r.get("total") is not None]
        if not pts:
            return {}
        vals = [w for _, w in pts]
        gap = max(3 * cfg["interval"], 3 * SAMPLE_GAP_S)
        wh = covered = 0.0
        for (t0, w0), (t1, w1) in zip(pts, pts[1:]):
            if 0 < t1 - t0 <= gap:
                wh += (w0 + w1) / 2 * (t1 - t0) / 3600
                covered += t1 - t0
        source = "integral"
        if mains and not cfg["total_hosts"]:
            first = next((r for r in rows if r.get("wh")), None)
            last = next((r for r in reversed(rows) if r.get("wh")), None)
            if first and last and first is not last and all(a in first["wh"] and a in last["wh"] for a in mains):
                delta = sum(last["wh"][a] - first["wh"][a] for a in mains)
                if delta >= 0:
                    wh, covered, source = delta, float(last["t"] - first["t"]), "counter"
        avg = sum(vals) / len(vals)
        price = cfg["price_kwh"]
        out = {"now": vals[-1], "avg": round(avg, 1), "min": min(vals), "max": max(vals),
               "kwh": round(wh / 1000, 3), "covered_s": round(covered), "energy_source": source,
               "per_day_kwh": round(avg * 24 / 1000, 2), "samples": len(vals),
               "price_kwh": price, "currency": cfg["currency"]}
        if price:
            out["cost"] = round(wh / 1000 * price, 2)
            out["per_day_cost"] = round(avg * 24 / 1000 * price, 2)
        return out

    def r_details(self):
        """Current readings for the Power tab tables: meters, hosts, rest."""
        now, stale, snap, meters = self._snapshot()
        cfg = self._cfg()
        names = {h.get("id"): h.get("name") or h.get("id") for h in self.ctx.get_hosts()}
        base = {hid: max(0.0, _num(r.get("base_w"), 0)) for hid, r in cfg["hosts"].items() if r.get("base_w")}
        t = self._totals()
        out_m = []
        for addr, row in cfg["shellys"].items():
            m = meters.get(addr) or {}
            fresh = m.get("w") is not None and now - m.get("ts", 0) <= stale
            out_m.append({"name": row["name"], "host": names.get(row["host_id"], row["host_id"]) if row["host_id"] else "",
                          "mains": not row["host_id"], "w": m.get("w") if fresh else None,
                          "v": m.get("v"), "a": m.get("a"), "kwh": round(m["wh"] / 1000, 2) if m.get("wh") is not None else None,
                          "temp": m.get("temp"), "on": m.get("on"),
                          "age": round(now - m["ts"]) if m.get("ts") else None, "err": m.get("err", "")})
        out_h = []
        for hid, r in sorted(self._host_readings(now, stale, snap, meters, cfg).items(),
                             key=lambda kv: str(names.get(kv[0], kv[0])).lower()):
            fresh = r["w"] is not None and now - r["ts"] <= stale
            if r.get("shelly"):
                src = "Shelly " + r["shelly"]
            elif r.get("cpu_only"):
                src = "agent: CPU + base %.0f W" % base[hid] if hid in base else "agent: CPU only"
            else:
                src = "JSON source"
            out_h.append({"id": hid, "name": names.get(hid, hid), "source": src,
                          "w": round(self._host_watts(r, base, hid), 1) if fresh else None,
                          "gpu": r.get("gpu") if fresh else None,
                          "age": round(now - r["ts"]), "err": r.get("err", "")})
        mains_w = sum(m["w"] for m in t["mains"].values()) if t["mains"] else None
        rest = None
        if mains_w is not None and t["hosts"] is not None and not t["with_hosts"]:
            rest = round(mains_w - t["hosts"], 1)
        return {"total": t["total"], "mains_w": mains_w, "hosts_w": t["hosts"], "rest_w": rest,
                "with_hosts": t["with_hosts"], "meters": out_m, "hosts": out_h,
                "interval": cfg["interval"]}

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

    def _host_readings(self, now, stale, snap, meters, cfg) -> dict[str, dict[str, Any]]:
        """Host id -> reading, with Shellys that carry a host id merged in."""
        snap = dict(snap)
        for addr, row in cfg["shellys"].items():          # a Shelly with a host id is that host's badge
            m = meters.get(addr)
            if row["host_id"] and m:
                snap[row["host_id"]] = {"w": m["w"], "gpu": None, "ts": m["ts"], "err": m["err"],
                                        "shelly": row["name"]}
        return snap

    def _host_watts(self, r: dict[str, Any], base: dict[str, float], hid: str) -> float:
        """The watts a host badge shows (CPU + base load when a base load is set)."""
        if r.get("cpu_only") and hid in base:
            return r["w"] + base[hid]
        return r["w"]

    def _totals(self) -> dict[str, Any]:
        """Mains Shellys (no host id), the sum of fresh host readings and the pill total."""
        now, stale, snap, meters = self._snapshot()
        cfg = self._cfg()
        base = {hid: max(0.0, _num(r.get("base_w"), 0)) for hid, r in cfg["hosts"].items() if r.get("base_w")}
        mains = {}
        for addr, row in cfg["shellys"].items():
            m = meters.get(addr)
            if not row["host_id"] and m and m["w"] is not None and now - m["ts"] <= stale:
                mains[addr] = {"name": row["name"], "w": m["w"], "wh": m.get("wh")}
        hosts = {hid: self._host_watts(r, base, hid)
                 for hid, r in self._host_readings(now, stale, snap, meters, cfg).items()
                 if r["w"] is not None and now - r["ts"] <= stale}
        hosts_w = sum(hosts.values()) if hosts else None
        parts = [m["w"] for m in mains.values()]
        if cfg["total_hosts"] and hosts_w is not None:
            parts.append(hosts_w)
        total = sum(parts) if parts else None
        return {"total": total, "mains": mains, "hosts": hosts_w, "host_n": len(hosts),
                "with_hosts": bool(cfg["total_hosts"])}

    def p_watts(self, session=None):
        now, stale, snap, meters = self._snapshot()
        cfg = self._cfg()
        hosts_cfg = cfg["hosts"]
        snap = self._host_readings(now, stale, snap, meters, cfg)
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
        """One slim row per Shelly above the host cards; nothing when none is configured.

        [● Homelab]  [65.8 W]  226.8 V · 0.58 A · 2541.7 kWh · 28 °C · 3 s ago
        """
        now, stale, _, meters = self._snapshot()
        rows = self._cfg()["shellys"]
        if not rows:
            return {"type": "stack", "children": []}
        lines = []
        for addr, row in rows.items():
            m = meters.get(addr)
            if not m:
                lines.append({"type": "stack", "dir": "row", "children": [
                    {"type": "status", "state": "unknown", "text": row["name"]},
                    {"type": "text", "text": "waiting for the first reading…", "tone": "muted"}]})
                continue
            age = now - m["ts"]
            if m["w"] is None or age > stale:
                lines.append({"type": "stack", "dir": "row", "children": [
                    {"type": "status", "state": "bad", "text": row["name"]},
                    {"type": "badge", "text": "— W", "tone": "muted", "title": m["err"] or "no recent reading"},
                    {"type": "text", "text": m["err"] or "no reading for %d s" % age, "tone": "muted"}]})
                continue
            w = m["w"]
            state = "unknown" if m.get("on") is None else ("ok" if m["on"] else "off")
            details = []
            if m.get("v") is not None:
                details.append("%.1f V" % m["v"])
            if m.get("a") is not None:
                details.append("%.2f A" % m["a"])
            if m.get("wh") is not None:
                details.append("%.1f kWh" % (m["wh"] / 1000))
            if m.get("temp") is not None:
                details.append("%.0f °C" % m["temp"])
            details.append("%d s ago" % age)
            relay = "" if m.get("on") is None else (" — relay on" if m["on"] else " — relay off")
            lines.append({"type": "stack", "dir": "row", "children": [
                {"type": "status", "state": state, "text": row["name"]},
                {"type": "badge", "text": ("%.1f" if w < 100 else "%.0f") % w + " W", "tone": self._tone(w),
                 "title": "Shelly %s%s" % (row["name"], relay)},
                {"type": "text", "text": " · ".join(details), "tone": "muted"}]})
        return lines[0] if len(lines) == 1 else {"type": "stack", "children": lines}
