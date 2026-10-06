# wattage

Shows each host's current power draw (e.g. `42.5 W`) as a badge on its host card.

Settings → Plugins → wattage → **Configure**: per host, a URL that returns JSON and a
dotted path to the watt value. Hosts with an empty URL are skipped. Examples:

| Source | URL | JSON path |
|---|---|---|
| Shelly Gen1 | `http://<ip>/status` | `meters.0.power` |
| Shelly Gen2 | `http://<ip>/rpc/Switch.GetStatus?id=0` | `apower` |
| Tasmota | `http://<ip>/cm?cmnd=Status%2010` | `StatusSNS.ENERGY.Power` |

An optional **GPU JSON path** (same URL) adds a second `GPU x W` badge.
Optional thresholds colour the badge (warn / bad). A failed or stale reading shows a muted
`— W` with the reason on hover. Redirects are not followed; only http(s) is allowed.
URLs are stored in the plugin config in plain text, so keep tokens out of them.


Capabilities: `hosts.read`, `ui.slots`.

## Host agent (CPU / GPU watts of Linux hosts)

`agent/wattage-agent.py` (repo root, stdlib only, read-only) serves RAPL CPU/DRAM watts, the
integrated GPU (RAPL uncore), NVIDIA (`nvidia-smi`) and AMD (`amdgpu` hwmon) as JSON on port 9871.
Install: `sudo ./agent/install.sh <bind-ip>`. In the plugin config use URL `http://<ip>:9871/`,
path `package_w` and GPU path `gpu_w`.
