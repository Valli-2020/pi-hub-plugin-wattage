"""wattage — show each host's current power draw on its card.

Per host you configure a URL that returns JSON and a dotted path to the watt
value (for example ``meters.0.power``).  A background task polls the URLs; a
contribution renders the result as a badge on the host card.
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from pi_hub.plugins.base import Contribution, Plugin, TaskDef

_MAX_BODY = 64 * 1024
_MAX_HOSTS = 12          # config schema holds at most 40 fields (4 + 3 per host)


def _key(host_id: str) -> str:
    """Config-field-safe form of a host id."""
    return re.sub(r"[^A-Za-z0-9_]", "_", host_id)


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
    version = "1.0.1"
    description = "Current power draw (W) on each host card, read from any JSON URL"
    min_core_version = "8.0.0"
    plugin_api_version = 2
    capabilities = ["hosts.read", "ui.slots"]

    def load(self, ctx) -> None:
        self.ctx = ctx
        self._lock = threading.Lock()
        self._data: dict[str, dict[str, Any]] = {}   # host id -> {w, ts, err}
        cfg = ctx.get_config()
        for k, v in (("interval", 15), ("timeout", 2), ("warn_w", 0), ("bad_w", 0)):
            cfg.setdefault(k, v)

    def get_config_schema(self):
        fields = [
            {"name": "interval", "label": "Poll interval (s)", "type": "number", "min": 5, "max": 3600,
             "help": "Applies after the plugin is reloaded."},
            {"name": "timeout", "label": "Timeout (s)", "type": "number", "min": 0.5, "max": 10},
            {"name": "warn_w", "label": "Warn above (W)", "type": "number", "min": 0,
             "help": "0 = no colour change."},
            {"name": "bad_w", "label": "Bad above (W)", "type": "number", "min": 0},
        ]
        try:
            hosts = self.ctx.get_hosts()[:_MAX_HOSTS]
        except Exception:
            hosts = []
        for h in hosts:
            k, label = _key(h["id"]), h.get("name") or h["id"]
            fields.append({"name": "url_" + k, "label": label + " — URL", "type": "text",
                           "placeholder": "http://<device-ip>/status",
                           "help": "Empty = not monitored. Visible in the config, so no secrets in the URL."})
            fields.append({"name": "path_" + k, "label": label + " — JSON path", "type": "text",
                           "placeholder": "meters.0.power"})
            fields.append({"name": "gpu_path_" + k, "label": label + " — GPU JSON path (optional)", "type": "text",
                           "placeholder": "gpu_w", "help": "Same URL. Shown as a second badge when present."})
        return fields

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

    def _poll_one(self, host_id: str, url: str, path: str, gpu_path: str, timeout: float) -> None:
        try:
            w, gpu = self._fetch(url, [path, gpu_path], timeout)
            res = {"w": w, "gpu": gpu, "ts": time.time(), "err": ""}
        except urllib.error.HTTPError as e:
            res = {"w": None, "gpu": None, "ts": time.time(), "err": "HTTP %d" % e.code}
        except Exception as e:
            res = {"w": None, "gpu": None, "ts": time.time(), "err": "%s: %s" % (type(e).__name__, e)}
        with self._lock:
            self._data[host_id] = res

    def poll_all(self) -> None:
        cfg = self.ctx.get_config()
        jobs = []
        for h in self.ctx.get_hosts()[:_MAX_HOSTS]:
            k = _key(h["id"])
            url, path = str(cfg.get("url_" + k) or "").strip(), str(cfg.get("path_" + k) or "").strip()
            gpu_path = str(cfg.get("gpu_path_" + k) or "").strip()
            if url and path:
                jobs.append((h["id"], url, path, gpu_path))
        with self._lock:
            for hid in list(self._data):
                if hid not in {j[0] for j in jobs}:
                    del self._data[hid]
        if not jobs:
            return
        timeout = float(cfg["timeout"])
        with ThreadPoolExecutor(max_workers=min(8, len(jobs))) as ex:
            for hid, url, path, gpu_path in jobs:
                ex.submit(self._poll_one, hid, url, path, gpu_path, timeout)

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

    def p_watts(self, session=None):
        stale = 3 * float(self.ctx.get_config()["interval"])
        now = time.time()
        with self._lock:
            snap = dict(self._data)
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
        stale = 3 * float(self.ctx.get_config()["interval"])
        now = time.time()
        with self._lock:
            snap = dict(self._data)
        return {hid: {"type": "badge", "text": "GPU %.1f W" % r["gpu"], "tone": "info",
                      "title": "GPU power draw, updated %d s ago" % (now - r["ts"])}
                for hid, r in snap.items()
                if r["gpu"] is not None and r["w"] is not None and now - r["ts"] <= stale}
