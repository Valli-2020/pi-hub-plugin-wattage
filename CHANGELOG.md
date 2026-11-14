# Changelog

All notable changes to this plugin are documented here. Releases follow
the repo-tag = version convention; each GitHub release ships the two
assets `pihub-plugin.json` and the versioned tarball.

## [1.5.0] - 2026-11-14

### Added
- **Total pill** in the header (`⚡ 69.3 W`): the Shelly meters without a
  host id; the tooltip shows the parts. A click opens the Power tab.
- **Toggle** *Add host readings to the total* (section *Total*, off by
  default): adds the host badges to the total. Leave it off when the hosts
  already hang behind the mains Shelly, or they are counted twice. Shellys
  with a host id count as host readings, not as mains.
- **Power graph**: a *Power* tab and a widget on the Hosts view, with 1 h,
  6 h and 24 h ranges, a line per mains Shelly and the hosts sum, the
  warn/bad limits and a hover readout. The last 24 h are kept in the
  plugin's `data/` folder and survive a restart.
- Setting *Show total in the header*.

### Changed
- New permissions `ui.header` (pill) and `ui.frame` (graph): the admin
  approves the plugin again after the update.

## [1.4.0] - 2026-11-14

### Added
- **Shelly meters**, read natively (Gen1 `/status`, Gen2+ `Shelly.GetStatus`;
  the generation is detected). New *Shelly meters* list in the Configure
  dialog: name, IP or URL, optional host id.
- A panel per Shelly above the host cards: power, voltage, current,
  lifetime energy (kWh), device temperature and relay state. Multi-channel
  devices (Plus 2PM) are summed; the Pro 3EM uses its total power.
- With a host id, the Shelly reading is also that host's card badge.
- Read-only: the plugin never switches a relay. Devices with a password
  show `HTTP 401` and are not supported yet.

## [1.3.0] - 2026-11-14

### Changed
- Every setting is now in the Configure dialog, grouped into Detection,
  Hosts, Badges and Polling. Needs Pi Hub 8.3.0.
- *Extra sources* and *Base load* (text settings) are replaced by one
  *Per-host settings* list: host id, source URL, JSON paths, base load and
  an optional agent port per host. Existing settings are converted
  automatically on upgrade.
- The poll interval now applies without a reload; saving settings polls again
  right away.

### Added
- Settings: show GPU badge, GPU badge threshold, stale-after polls.

## [1.2.0] - 2026-10-06

### Added
- **Base load** setting: per host, the watts the rest of the machine draws
  on top of the CPU. The badge then shows an estimate (`≈ 28 W`).

### Changed
- Host-agent readings without a base load are labelled `CPU 12.8 W` with a
  tooltip that they exclude board, disks, fans and PSU losses.

## [1.1.0] - 2026-10-06

### Changed
- **No per-host settings any more.** Hosts running the host agent are
  detected automatically on a fixed port; other JSON sources go into one
  *Extra sources* setting. The Configure form no longer lists host names.
- The GPU badge only appears when the GPU reports at least 0.1 W, so idle
  integrated GPUs stay quiet.

## [1.0.1] - 2026-10-06

### Changed
- Removed setup-specific names and the example IP from the config
  placeholder and docs; README, LICENSE and manifest follow the layout
  of the other Pi Hub plugins.

## [1.0.0] - 2026-10-06

### Added
- Power-draw badge (`42.5 W`) on every host card, read from a JSON URL
  per host (smart plug, Home Assistant, the bundled host agent, …).
- Optional GPU badge (`GPU 7.3 W`) from a second JSON path on the same URL.
- Warn / bad thresholds that colour the badge; stale or failed readings
  show a muted `— W` with the reason on hover.
- Read-only host agent (`agent/`) for Linux hosts: Intel RAPL (CPU, DRAM,
  integrated GPU), NVIDIA (`nvidia-smi`) and AMD (`amdgpu` hwmon).
