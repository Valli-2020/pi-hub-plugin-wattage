# Pi Hub Plugin: Wattage

The current power draw of every device, right on its Pi Hub host card:
a `42.5 W` badge, plus a `GPU 7.3 W` badge where a GPU reports power.
Any source that returns JSON works — a metered smart plug, Home Assistant,
or the small host agent bundled in this repo.

## What it does

- **Power badge** on each host card, updated every 15 s (configurable).
  Hosts without a URL show nothing.
- **GPU badge** (optional): a second JSON path read from the same URL.
- **Thresholds**: optional *warn* / *bad* watt limits change the badge
  colour. A failed or stale reading shows a muted `— W`; the reason is in
  the tooltip.
- **Host agent** (`agent/`): a read-only, stdlib-only exporter for Linux
  hosts serving CPU / DRAM watts (Intel RAPL), the integrated GPU (RAPL
  uncore), NVIDIA cards (`nvidia-smi`) and AMD cards (`amdgpu` hwmon).

## Install

1. Open **Settings → Plugins** in Pi Hub (admin account).
2. Add the repository `https://github.com/Valli-2020/pi-hub-plugin-wattage`,
   click **Scan**, then **Install** and **Enable**.
3. Approve the permissions: `hosts.read` (list your hosts) and `ui.slots`
   (the badges on the host cards).

## Configuration

**Settings → Plugins → wattage → Configure.** For every host:

| Field | Meaning |
|---|---|
| URL | Where to read the value. Empty = host not monitored |
| JSON path | Dotted path to the watt value, e.g. `meters.0.power` (list indices allowed) |
| GPU JSON path | Optional, same URL; adds the `GPU` badge |

Global: poll interval (5–3600 s, applied after a reload), timeout, warn /
bad thresholds in watts (`0` = no colour change).

Examples:

| Source | URL | JSON path |
|---|---|---|
| Shelly Gen1 | `http://<device-ip>/status` | `meters.0.power` |
| Shelly Gen2 | `http://<device-ip>/rpc/Switch.GetStatus?id=0` | `apower` |
| Tasmota | `http://<device-ip>/cm?cmnd=Status%2010` | `StatusSNS.ENERGY.Power` |
| Host agent | `http://<host-ip>:9871/` | `package_w` (GPU: `gpu_w`) |

## Host agent

Plugins cannot run arbitrary commands on a host, so CPU and GPU watts of a
Linux machine come from a tiny exporter:

```sh
sudo ./agent/install.sh <bind-ip>      # installs and starts wattage-agent.service
curl http://<bind-ip>:9871/            # {"package_w": 12.8, "cpu_w": 8.9, "gpu_w": 0.0, ...}
```

- Fields: `package_w`, `cpu_w`, `dram_w` (if present), `gpu_w`, and a `gpus`
  list with a name and watts per GPU.
- Integrated GPUs are read from the RAPL `uncore` domain; NVIDIA and AMD
  cards take precedence when present.
- Runs as root because the kernel only lets root read RAPL counters; the
  unit drops all capabilities and mounts the filesystem read-only.
- It binds to the address you pass and has **no authentication**: use a
  LAN or VPN address and keep the port closed to the internet.

## Notes

- Only `http://` and `https://` URLs are read; redirects are not followed
  and responses are capped at 64 KB.
- URLs are stored in the plugin's `config.json` in plain text, so keep
  tokens out of them.
- Up to 12 hosts fit into the config form.

## Limitations

- RAPL measures the CPU package, not wall power. For wall power use a
  metered plug.
- A host that is off shows `— W` until it answers again.

## Development

```sh
PYTHONPATH=<pi-hub checkout> python3 tests/test_plugin_wattage.py
```

The tests run the plugin against a local stub server (JSON path, redirect
refusal, GPU badge, thresholds).

## Release assets

Each GitHub release ships two assets:

- `pihub-plugin.json` — the Pi Hub plugin manifest
- `pi-hub-plugin-wattage-<version>.tar.gz` — the plugin code (top-level dir
  `pi_hub_plugins/wattage/`)

## License

MIT
