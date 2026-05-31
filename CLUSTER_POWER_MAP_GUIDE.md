# Cluster Power Map

`power_map` is an optional field in `site_config.json` for clusters where PCS and BMS/BESS are not one-to-one or simple equal-share.

If `power_map` is empty or missing, ESS-AIO keeps the existing behavior:

- Sum all BMS allowed power in the cluster.
- Clamp the cluster target by that total.
- Split the final power by the selected allocation mode, usually equal split.

If `power_map` is present, Strategy calculates a per-PCS allowed capacity from BMS limits and topology weights.

Example: 3 BESS / 2 PCS, where the middle BESS is shared by two PCS:

```json
{
  "name": "Cluster-A",
  "bms_devices": ["BMS-1", "BMS-2", "BMS-3"],
  "pcs_devices": ["PCS-1", "PCS-2"],
  "power_map": {
    "PCS-1": {"BMS-1": 1.0, "BMS-2": 0.5},
    "PCS-2": {"BMS-2": 0.5, "BMS-3": 1.0}
  }
}
```

If BMS limits are:

- BMS-1 = 500 kW
- BMS-2 = 600 kW
- BMS-3 = 500 kW

Then:

- PCS-1 allowed = 500 × 1.0 + 600 × 0.5 = 800 kW
- PCS-2 allowed = 600 × 0.5 + 500 × 1.0 = 800 kW

For a cluster target of -1200 kW, Strategy dispatches:

- PCS-1 = -600 kW
- PCS-2 = -600 kW

For a target of -1800 kW, total topology capacity is 1600 kW, so Strategy clamps and dispatches:

- PCS-1 = -800 kW
- PCS-2 = -800 kW

Notes:

- Weights are capacity shares, not percentages that must sum to 1 globally.
- Missing BMS names contribute 0 kW.
- Offline/timed-out/cutoff BMS contributes 0 kW.
- `power_map` only affects cluster strategy allocation; manual PCS set power is unchanged.
