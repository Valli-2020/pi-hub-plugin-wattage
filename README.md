# Pi Hub Plugin: Wattage

The current power draw of every device, right on its Pi Hub host card:
a `42.5 W` badge, plus a `GPU 7.3 W` badge where a GPU reports power.
Any source that returns JSON works — a metered smart plug, Home Assistant,
or the small host agent bundled in this repo.

## What it does

- **Power badge** on each host card, updated every 15 s (configurable).
  Hosts without a reading show nothing.
- **Automatic detection**: every host that runs the bundled host agent is
  found on its own — no per-host setup.
- **GPU badge**: shown when the host reports GPU power (NVIDIA, AMD, or an
  integrated GPU under load).
- **Extra sources**: any other JSON endpoint (metered smart plug, Home
  Assistant) can be added in one setting.
- **Thresholds**: optional *warn* / *bad* watt limits change the badge
  colour. A failed or stale reading shows a muted `— W`; the reason is in
  the tooltip.

## Install

1. Open **Settings → Plugins** in Pi Hub (admin account).
2. Add the repository `https://github.com/Valli-2020/pi-hub-plugin-wattage`,
   click **Scan**, then **Install** and **Enable**.
3. Approve the permissions: `hosts.read` (list your hosts) and `ui.slots`
   (the badges on the host cards).
4. Install the host agent on the machines you want to measure (below).

## Configuration

**Settings → Plugins → wattage → Configure.** No host list to fill in.

| Setting | Meaning |
|---|---|
| Host agent port | Port the agent answers on (default `9871`); `1` switches detection off |
| Extra sources | Other JSON sources: `host-id url json-path [gpu-path]`, entries separated by `;` |
| Poll interval / Timeout | Seconds (interval applies after a reload) |
| Warn / Bad above (W) | Badge colour limits; `0` = no colour change |

Examples for *Extra sources* (the host id is the id of the card it belongs to):

| Source | Entry |
|---|---|
| Shelly Gen1 | `myhost http://<device-ip>/status meters.0.power` |
| Shelly Gen2 | `myhost http://<device-ip>/rpc/Switch.GetStatus?id=0 apower` |
| Tasmota | `myhost http://<device-ip>/cm?cmnd=Status%2010 StatusSNS.ENERGY.Power` |

An extra source wins over the agent for the same host. The JSON path is
dotted, list indices allowed.

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
- Extra-source URLs are stored in the plugin's `config.json` in plain text,
  so keep tokens out of them.
- Every host with an IP is probed on the agent port each interval (1–2 s
  timeout, in parallel); hosts without an agent simply show nothing.

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
