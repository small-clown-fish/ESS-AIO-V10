# PCS Profile Guide

ESS-AIO PCS control is now profile-driven. For a new PCS, add a JSON file under `pcs_profiles/` and select it when adding the PCS. Strategy and UI still call generic actions such as `set_active_power`, `start`, `stop`, and `set_reactive_power`; the profile decides the Modbus address, function code, scale, data type, address offset, and optional pre-actions.

## Minimum profile for Strategy

A PCS used by Cluster Strategy should at least define:

- `points.active_power` for reading actual active power, if available.
- `points.set_active_power` for writing target active power.
- `capabilities.active_power_control.point = "set_active_power"`.

## Function codes

Use:

- `register_type: "holding"` + `write_function: "single"` for FC06.
- `register_type: "holding"` + `write_function: "multiple"` for FC16.
- `register_type: "coil"` + `write_function: "fc05"` for FC05.
- `register_type: "input"` for FC04 reads.
- `register_type: "holding"` for FC03 reads.

## Address offset

If the vendor document gives 1-based register addresses but packet capture expects PDU address minus one, set either global:

```json
"address_offset": -1
```

or per point:

```json
"address_offset": -1
```

## Pre-actions

Some PCS require an enable register before a setpoint. For example, reactive power may require `7909 = 1` before writing `7812`:

```json
"set_reactive_power": {
  "register_type": "holding",
  "address": 7812,
  "write_function": "single",
  "pre_actions": [
    {"point": "reactive_power_remote_enable", "value": 1}
  ]
}
```

## Commands

Map generic commands to vendor-specific points:

```json
"commands": {
  "start": {"point": "start_cmd"},
  "stop": {"point": "stop_cmd"},
  "reset_fault": {"point": "reset_fault_cmd"}
}
```

With this structure, adding most PCS models does not require Python code changes.
