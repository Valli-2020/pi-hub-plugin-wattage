# pi-hub-plugin-wattage

Pi Hub plugin (API v2) that shows each host's current power draw
(and GPU power, if present) as badges on the host cards. Plugin docs: `pi_hub_plugins/wattage/README.md`.

- `pi_hub_plugins/wattage/`: the plugin
- `agent/`: optional read-only exporter for Linux hosts (RAPL, NVIDIA, AMD, integrated GPU)
- `tests/test_plugin_wattage.py`: run with `PYTHONPATH=<pi-hub checkout>`

MIT licensed.
