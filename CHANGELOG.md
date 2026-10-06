# Changelog

All notable changes to this plugin are documented here. Releases follow
the repo-tag = version convention; each GitHub release ships the two
assets `pihub-plugin.json` and the versioned tarball.

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
