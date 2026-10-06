"""wattage — show each host's current power draw on its card.

Hosts that run the bundled wattage-agent are found automatically (it answers
on a fixed port).  Any other JSON source (smart plug, Home Assistant) can be
added in the "Extra sources" setting.  A background task polls everything; a
contribution renders the result as badges on the host cards.
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
_GPU_MIN_W = 0.1        # hide the GPU badge while an integrated GPU idles


def parse_extra(text: str) -> dict[str, tuple[str, str, str]]:
    """``"id url path [gpu-path]; id url path"`` -> {id: (url, path, gpu_path)}."""
    out = {}
    for entry in str(text or "").split(";"):
        parts = entry.split()
        if len(parts) in (3, 4):
            out[parts[0]] = (parts[1], parts[2], parts[3] if len(parts) == 4 else "")
    return out


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


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


class Wattage(Plugin):
    name = "wattage"
    version = "1.1.0"
    description = "Current power draw (W) and GPU power on each host card"
    min_core_version = "8.0.0"
    plugin_api_version = 2
    capabilities = ["hosts.read", "ui.slots"]

    def load(self, ctx) -> None:
        self.ctx = ctx
        self._lock = threading.Lock()
        self._data: dict[str, dict[str, Any]] = {}   # host id -> {w, gpu, ts, err}
        cfg = ctx.get_config()
        for k, v in (("interval", 15), ("timeout", 2), ("agent_port", 9871),
                     ("warn_w", 0), ("bad_w", 0), ("extra", "")):
            cfg.setdefault(k, v)

    def get_config_schema(self):
        return [
            {"name": "agent_port", "label": "Host agent port", "type": "number", "min": 1, "max": 65535,
             "help": "Hosts running wattage-agent are detected on this port. Set to 1 to switch detection off."},
            {"name": "extra", "label": "Extra sources", "type": "text",
             "placeholder": "host-id http://<ip>/status meters.0.power; host-id2 http://<ip>/x path gpu-path",
             "help": "Other JSON sources: host id, URL, JSON path and optional GPU path, entries separated by ';'."},
            {"name": "interval", "label": "Poll interval (s)", "type": "number", "min": 5, "max": 3600,
             "help": "Applies after the plugin is reloaded."},
            {"name": "timeout", "label": "Timeout (s)", "type": "number", "min": 0.5, "max": 10},
            {"name": "warn_w", "label": "Warn above (W)", "type": "number", "min": 0,
             "help": "0 = no colour change."},
            {"name": "bad_w", "label": "Bad above (W)", "type": "number", "min": 0},
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
            res = {"w": w, "gpu": gpu, "ts": time.time(), "err": ""}
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
        cfg = self.ctx.get_config()
        extra = parse_extra(cfg.get("extra"))
        jobs = [(hid, url, path, gpu, False) for hid, (url, path, gpu) in extra.items()]
        port = int(cfg.get("agent_port") or 0)
        hosts = self.ctx.get_hosts()
        if port > 1:
            jobs += [(h["id"], "http://%s:%d/" % (h["ip"], port), "package_w", "gpu_w", True)
                     for h in hosts if h.get("ip") and h["id"] not in extra]
        known = {j[0] for j in jobs}
        with self._lock:
            for hid in list(self._data):
                if hid not in known:
                    del self._data[hid]
        if not jobs:
            return
        timeout = float(cfg["timeout"])
        with ThreadPoolExecutor(max_workers=min(8, len(jobs))) as ex:
            for hid, url, path, gpu, auto in jobs:
                ex.submit(self._poll_one, hid, url, path, gpu, timeout, auto)

    def get_tasks(self):
        return [TaskDef("poll", self.poll_all, interval=float(self.ctx.get_config()["interval"]))]

    # ── UI ───────────────────────────────────────────────────────────────

    def get_contributions(self):
        return [Contribution("hosts.card.badges", "watts", self.p_watts, poll=15, order=30),
                Contribution("hosts.card.badges", "gpu", self.p_gpu, poll=15, order=31)]

    def _tone(self, w: float) -> str:
        cfg = self.ctx.get_config()
        bad, warn = float(cfg["bad_w"] or 0), float(cfg["warn_w"] or 0)
        if bad and w >= bad:
            return "bad"
        if warn and w >= warn:
            return "warn"
        return "info"

    def _snapshot(self):
        stale = 3 * float(self.ctx.get_config()["interval"])
        now = time.time()
        with self._lock:
            return now, stale, dict(self._data)

    def p_watts(self, session=None):
        now, stale, snap = self._snapshot()
        out = {}
        for hid, r in snap.items():
            age = now - r["ts"]
            if r["w"] is None or age > stale:
                out[hid] = {"type": "badge", "text": "— W", "tone": "muted",
                            "title": r["err"] or "no recent reading"}
            else:
                out[hid] = {"type": "badge", "text": "%.1f W" % r["w"] if r["w"] < 100 else "%.0f W" % r["w"],
                            "tone": self._tone(r["w"]),
                            "title": "Power draw, updated %d s ago" % age}
        return out

    def p_gpu(self, session=None):
        now, stale, snap = self._snapshot()
        return {hid: {"type": "badge", "text": "GPU %.1f W" % r["gpu"], "tone": "info",
                      "title": "GPU power draw, updated %d s ago" % (now - r["ts"])}
                for hid, r in snap.items()
                if r["w"] is not None and r["gpu"] is not None and r["gpu"] >= _GPU_MIN_W and now - r["ts"] <= stale}
