# Pi Hub Plugin: Wattage

The current power draw of every device, right on its Pi Hub host card:
a `42.5 W` badge, plus a `GPU 7.3 W` badge where a GPU reports power.
Any source that returns JSON works — a metered smart plug, Home Assistant,
or the small host agent bundled in this repo.

## What it does

- **Power badge** on each host card, updated every 15 s (configurable).
  Hosts without a reading show nothing. Agent readings are labelled
  `CPU 12.8 W`; with a base load set they show an estimate, `≈ 28 W`.
- **Automatic detection**: every host that runs the bundled host agent is
  found on its own — no per-host setup.
- **GPU badge**: shown when the host reports GPU power (NVIDIA, AMD, or an
  integrated GPU under load).
- **Shelly meters**: a Shelly plug or relay with power metering (e.g. the
  one feeding the whole rack) gets its own panel above the host cards —
  power, voltage, current, energy total, device temperature, relay state.
  Optionally its reading also becomes one host's badge.
- **Total pill** in the header (`69.3 W`, styled like the health pills): the mains Shellys, optionally
  plus all host readings (a toggle). A click opens the Power tab.
- **Power graph**: a *Power* tab and a widget on the Hosts view — total,
  each mains Shelly and the hosts sum over 1 h, 6 h or 24 h. The history
  is kept for 24 h in the plugin's `data/` folder.
- **Extra sources**: any other JSON endpoint (metered smart plug, Home
  Assistant) can be added as a row in the *Hosts* setting.
- **Thresholds**: optional *warn* / *bad* watt limits change the badge
  colour. A failed or stale reading shows a muted `— W`; the reason is in
  the tooltip.

## Install

1. Open **Settings → Plugins** in Pi Hub (admin account).
2. Add the repository `https://github.com/Valli-2020/pi-hub-plugin-wattage`,
   click **Scan**, then **Install** and **Enable**.
3. Approve the permissions: `hosts.read` (list your hosts), `ui.slots`
   (badges and the Shelly panel), `ui.header` (the total pill),
   `ui.style` (scoped CSS so the pill matches the header) and `ui.frame`
   (the power graph, a sandboxed frame).
4. Install the host agent on the machines you want to measure (below).

## Configuration

**Settings → Plugins → wattage → Configure.** Every setting is in the dialog.
Hosts are detected automatically; the *Hosts* list is only for exceptions.

| Setting | Section | Default | Meaning |
|---|---|---|---|
| Host agent port | Detection | `9871` | Port the agent answers on; `1` switches detection off |
| Per-host settings | Hosts | empty | One row per host, see below |
| Shelly meters | Shelly | empty | One row per Shelly, see below |
| Show total in the header | Total | on | The `… W` pill next to the health pills |
| Add host readings to the total | Total | off | See *Total* below |
| Show GPU badge | Badges | on | Turn the GPU badge off everywhere |
| Hide GPU badge below (W) | Badges | `0.1` | Keeps idle integrated GPUs quiet |
| Warn / Bad above (W) | Badges | `0` | Badge colour limits; `0` = no colour change |
| Poll interval (s) | Polling | `15` | Applies live |
| Timeout (s) | Polling | `2` | Per request |
| Stale after (polls) | Polling | `3` | Older readings show a muted `— W` |

Columns of a *Per-host settings* row (all optional except the host id):

| Column | Meaning |
|---|---|
| Host id | Id of the host card the row belongs to |
| Source URL + Power JSON path | Another JSON source for this host (wins over the agent); dotted path, list indices allowed |
| GPU JSON path | Optional GPU watts path in the same response |
| Base load (W) | What the rest of the machine draws on top of the CPU; agent readings then show an estimate |
| Agent port | Overrides the default agent port for this host |

Columns of a *Shelly meters* row:

| Column | Meaning |
|---|---|
| Name | Panel title, e.g. `Homelab` (defaults to the address) |
| IP or URL | `192.0.2.10`, `shelly.lan` or `http://192.0.2.10`; Gen1 and Gen2+ are detected |
| Host id | Optional: also show the reading as this host's badge (wins over a source URL and the agent) |

Shelly support is read-only — the relay is never switched — and needs the
device's local password (*Restrict login*) to be off.

**Total.** A Shelly *without* a host id is a mains meter: it always counts
toward the total. Host readings (agents, source URLs, Shellys *with* a host
id) are added only when *Add host readings to the total* is on. Keep it off
when the hosts are powered through the mains Shelly — they are already in
its reading. Turn it on when there is no mains Shelly, or the hosts are on
other circuits.

Examples for a generic source row (Source URL, Power JSON path):

| Source | URL | Path |
|---|---|---|
| Tasmota | `http://<device-ip>/cm?cmnd=Status%2010` | `StatusSNS.ENERGY.Power` |

Upgrading from 1.2: the old *Extra sources* and *Base load* text settings are
converted into rows automatically.

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
- Source URLs are stored in the plugin's `config.json` in plain text,
  so keep tokens out of them.
- Every host with an IP is probed on the agent port each interval (1–2 s
  timeout, in parallel); hosts without an agent simply show nothing.

## Limitations

- RAPL measures the CPU package, not wall power: board, disks, fans and PSU
  losses are missing. Measure the real draw once with a metered plug and
  enter the difference as *Base load*, or use the plug as an extra source.
- A host that is off shows `— W` until it answers again.

## Development

```sh
PIHUB_CORE=<pi-hub checkout> python3 tests/test_plugin_wattage.py   # or pytest
```

The tests run the plugin against a local stub server (JSON path, redirect
refusal, GPU badge, thresholds, config migration, live settings, Shelly
Gen1/Gen2 parsing and panel, total pill and toggle, graph history).

## Release assets

Each GitHub release ships two assets:

- `pihub-plugin.json` — the Pi Hub plugin manifest
- `pi-hub-plugin-wattage-<version>.tar.gz` — the plugin code (top-level dir
  `pi_hub_plugins/wattage/`, including `static/frame/`)

## License

MIT
