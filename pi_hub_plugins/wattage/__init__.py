"""wattage — show each host's current power draw on its card.

Hosts that run the bundled wattage-agent are found automatically (it answers
on a fixed port).  Per-host rows in the "Hosts" setting add another JSON
source (smart plug, Home Assistant), a base load or a different agent port.
A background task polls everything; a contribution renders the result as
badges on the host cards.
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
    "stale_polls": 3, "warn_w": 0, "bad_w": 0, "hosts": [],
}


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
    version = "1.3.0"
    description = "Current power draw (W) and GPU power on each host card"
    min_core_version = "8.3.0"
    plugin_api_version = 2
    capabilities = ["hosts.read", "ui.slots"]

    def load(self, ctx) -> None:
        self.ctx = ctx
        self._lock = threading.Lock()
        self._data: dict[str, dict[str, Any]] = {}   # host id -> {w, gpu, ts, err}
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
        with _opener.open(url, timeout=timeout) as r:
            body = r.read(_MAX_BODY + 1)
        if len(body) > _MAX_BODY:
            raise ValueError("response too large")
        data = json.loads(body)
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

    def poll_all(self) -> None:
        cfg = self._cfg()
        hosts_cfg = cfg["hosts"]
        jobs = [(hid, r["url"], str(r.get("path") or ""), str(r.get("gpu_path") or ""), False)
                for hid, r in hosts_cfg.items() if r.get("url") and r.get("path")]
        extra = {j[0] for j in jobs}
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
        if not jobs:
            return
        with ThreadPoolExecutor(max_workers=min(8, len(jobs))) as ex:
            for hid, url, path, gpu, auto in jobs:
                ex.submit(self._poll_one, hid, url, path, gpu, cfg["timeout"], auto)

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
                Contribution("hosts.card.badges", "gpu", self.p_gpu, poll=15, order=31)]

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
            return now, stale, dict(self._data)

    def p_watts(self, session=None):
        now, stale, snap = self._snapshot()
        hosts_cfg = self._cfg()["hosts"]
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
            else:
                text = (fmt + " W") % w
                title = "Power draw, updated %d s ago" % age
            out[hid] = {"type": "badge", "text": text, "tone": self._tone(w), "title": title}
        return out

    def p_gpu(self, session=None):
        now, stale, snap = self._snapshot()
        cfg = self._cfg()
        if not cfg["show_gpu"]:
            return {}
        gpu_min = cfg["gpu_min_w"]
        return {hid: {"type": "badge", "text": "GPU %.1f W" % r["gpu"], "tone": "info",
                      "title": "GPU power draw, updated %d s ago" % (now - r["ts"])}
                for hid, r in snap.items()
                if r["w"] is not None and r["gpu"] is not None and r["gpu"] >= gpu_min and now - r["ts"] <= stale}
