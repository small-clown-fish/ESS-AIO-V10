from __future__ import annotations

import json
import os
import queue
import threading
import time
import gc
import sys
from dataclasses import dataclass
from typing import Any, Callable
from collections import deque
from pathlib import Path
from datetime import datetime

from fastapi import FastAPI, Request, UploadFile, File
from pydantic import BaseModel
from fastapi.responses import HTMLResponse, Response
from PySide6.QtCore import QObject, QTimer


API_SCHEMA_VERSION = "9.9.26-lts-dashboard-bms-status-view"


class ClusterRequest(BaseModel):
    cluster: str = ""


class ClusterPowerRequest(BaseModel):
    cluster: str
    power_kw: float


class ClusterStrategySettingsRequest(BaseModel):
    cluster: str
    mode: str | None = None
    target_power_kw: float | None = None
    ramp_step_kw: float | None = None
    ramp_interval_s: float | None = None
    bms_timeout_s: float | None = None
    charge_cutoff_mv: float | None = None
    discharge_cutoff_mv: float | None = None
    allocation_mode: str | None = None
    timeout_action: str | None = None


class RuntimeRestoreRequest(BaseModel):
    restore_bms: bool = False
    restore_pcs: bool = False
    restore_csv: bool = False
    restore_strategy: bool = False


class SoakStartRequest(BaseModel):
    label: str | None = None
    interval_s: float | None = 60.0


class DeviceRequest(BaseModel):
    device: str


class PcsRequest(BaseModel):
    pcs: str


class BmsCommandRequest(BaseModel):
    command: str
    scope: str = "single"
    device: str = ""


class BmsHvAllRequest(BaseModel):
    mode: str = "on"
    timeout: float = 30.0
    poll_interval: float = 1.0


class PcsCommandRequest(BaseModel):
    pcs: str = ""
    method: str
    value: float | None = None


class UiActionRequest(BaseModel):
    action: str
    params: dict[str, Any] = {}
    confirm_text: str = ""


class BmsRegisterWriteRequest(BaseModel):
    device: str = ""
    scope: str = "single"
    address: int | str
    value: int


class RegisterReadRequest(BaseModel):
    device_type: str = "bms"
    device: str = ""
    register_type: str = "holding"
    mode: str = "continuous"
    start: int | str
    count: int = 1
    step: int | str = "0x400"
    quantity: int = 1
    length: int = 1


class RegisterLookupRequest(BaseModel):
    device_type: str = "bms"
    device: str = ""
    address: int | str


class BmsRtcWriteRequest(BaseModel):
    device: str = ""
    scope: str = "single"
    year: int
    month: int
    day: int
    hour: int
    minute: int
    second: int


class StrategyAllRequest(BaseModel):
    confirm_text: str = ""


class PacketAnalyzeRequest(BaseModel):
    path: str
    timeout_seconds: float = 2.0
    limit: int = 200


class JointAnalyzeRequest(BaseModel):
    asc_path: str
    modbus_path: str
    dbc_path: str = ""
    mapping_path: str = ""
    tolerance_s: float = 0.5
    limit: int = 200


@dataclass
class RuntimeCommand:
    command_id: str
    name: str
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    done: threading.Event
    result: dict[str, Any] | None = None


class RuntimeApiBridge(QObject):
    """Thread-safe bridge between a REST API thread and the Qt runtime thread.

    The first runtime/UI split phase keeps the proven ESS-AIO runtime objects
    running in a hidden Qt application, but exposes snapshots and commands over
    localhost HTTP. HTTP handlers never call Qt/UI/runtime objects directly; they
    enqueue commands and a QTimer executes them on the Qt main thread.
    """

    def __init__(self, window: Any, *, command_timeout_s: float = 10.0) -> None:
        super().__init__()
        self.window = window
        self.command_timeout_s = float(command_timeout_s)
        self._commands: "queue.Queue[RuntimeCommand]" = queue.Queue()
        self._timer = QTimer(self)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._drain_commands)
        self._timer.start()
        self._started_ts = time.time()
        self._last_command_result: dict[str, Any] = {}
        self._command_history: list[dict[str, Any]] = []
        self._ack_lock = threading.RLock()
        self._command_seq = 0
        self._command_acks: dict[str, dict[str, Any]] = {}
        self._previous_runtime_state: dict[str, Any] = self._load_runtime_state()
        self._last_persist_ts = 0.0
        self._state_timer = QTimer(self)
        self._state_timer.setInterval(5000)
        self._state_timer.timeout.connect(lambda: self._persist_runtime_state("periodic"))
        self._state_timer.start()
        self._soak_running = False
        self._soak_started_ts = 0.0
        self._soak_label = ""
        self._soak_interval_s = 60.0
        self._soak_sample_count = 0
        self._soak_last_sample: dict[str, Any] = {}
        self._soak_timer = QTimer(self)
        self._soak_timer.setInterval(60000)
        self._soak_timer.timeout.connect(self._soak_sample)

        # v9.3: keep a bounded server-side curve cache so large Web dashboards
        # do not have to rebuild long histories from full runtime snapshots.
        self._curve_history_lock = threading.RLock()
        self._curve_history: dict[str, deque[dict[str, Any]]] = {}
        self._curve_history_max_samples = int(os.environ.get("ESS_AIO_CURVE_HISTORY_MAX", "1800"))
        self._curve_timer = QTimer(self)
        self._curve_timer.setInterval(max(500, int(os.environ.get("ESS_AIO_CURVE_SAMPLE_INTERVAL_MS", "2000"))))
        self._curve_timer.timeout.connect(self._sample_curve_history)
        self._curve_timer.start()


    def _soak_path(self) -> Path:
        try:
            if hasattr(self.window, "get_profile_path"):
                return self.window.get_profile_path("soak_test_runtime.jsonl")
        except Exception:
            pass
        try:
            return Path.cwd() / "soak_test_runtime.jsonl"
        except Exception:
            return Path("soak_test_runtime.jsonl")

    def _process_memory_mb(self) -> float | None:
        # Best-effort without adding psutil dependency.
        try:
            import resource  # type: ignore
            rss = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
            # macOS reports bytes, Linux reports KB. Normalize heuristically.
            if rss > 1024 * 1024 * 10:
                return round(rss / 1024 / 1024, 1)
            return round(rss / 1024, 1)
        except Exception:
            return None

    def _soak_build_sample(self, reason: str = "periodic") -> dict[str, Any]:
        snap = self.snapshot()
        commands = snap.get("command_acks", []) or []
        command_status_counts: dict[str, int] = {}
        for c in commands:
            st = str((c or {}).get("status") or "unknown")
            command_status_counts[st] = command_status_counts.get(st, 0) + 1
        summary = snap.get("summary", {}) or {}
        workers = snap.get("workers", {}) or {}
        sample = {
            "ts": time.time(),
            "iso": datetime.now().isoformat(timespec="seconds"),
            "reason": reason,
            "api_schema": API_SCHEMA_VERSION,
            "label": self._soak_label,
            "runtime_uptime_s": snap.get("uptime_s"),
            "soak_elapsed_s": round(time.time() - self._soak_started_ts, 1) if self._soak_started_ts else 0.0,
            "pid": os.getpid(),
            "memory_mb": self._process_memory_mb(),
            "summary": summary,
            "workers": {
                "bms_running_count": len(workers.get("bms_running", []) or []),
                "pcs_running_count": len(workers.get("pcs_running", []) or []),
                "strategy_count": len(workers.get("strategies", []) or []),
            },
            "recording": snap.get("recording", {}) or {},
            "logs": snap.get("logs", {}) or {},
            "command_status_counts": command_status_counts,
        }
        return sample

    def _soak_sample(self, reason: str = "periodic") -> dict[str, Any]:
        if not self._soak_running and reason == "periodic":
            return {"ok": False, "error": "soak test is not running"}
        try:
            sample = self._soak_build_sample(reason)
            path = self._soak_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(sample, ensure_ascii=False) + "\n")
            self._soak_sample_count += 1
            self._soak_last_sample = sample
            return {"ok": True, "path": str(path), "sample": sample}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "path": str(self._soak_path())}

    def soak_start(self, label: str = "", interval_s: float = 60.0) -> dict[str, Any]:
        self._soak_running = True
        self._soak_started_ts = time.time()
        self._soak_label = str(label or "soak-test")
        self._soak_interval_s = max(10.0, min(3600.0, float(interval_s or 60.0)))
        self._soak_sample_count = 0
        self._soak_last_sample = {}
        self._soak_timer.setInterval(int(self._soak_interval_s * 1000))
        self._soak_timer.start()
        first = self._soak_sample("start")
        return {
            "ok": bool(first.get("ok", False)),
            "running": True,
            "label": self._soak_label,
            "interval_s": self._soak_interval_s,
            "path": str(self._soak_path()),
            "first_sample": first,
        }

    def soak_stop(self) -> dict[str, Any]:
        final = self._soak_sample("stop") if self._soak_running else {"ok": True, "message": "not running"}
        self._soak_running = False
        self._soak_timer.stop()
        return {"ok": True, "running": False, "path": str(self._soak_path()), "final_sample": final, "status": self.soak_status()}

    def soak_status(self) -> dict[str, Any]:
        return {
            "ok": True,
            "running": bool(self._soak_running),
            "label": self._soak_label,
            "interval_s": self._soak_interval_s,
            "started_ts": self._soak_started_ts,
            "elapsed_s": round(time.time() - self._soak_started_ts, 1) if self._soak_started_ts else 0.0,
            "sample_count": int(self._soak_sample_count),
            "path": str(self._soak_path()),
            "last_sample": self._soak_last_sample,
        }

    def soak_report(self, limit: int = 5000) -> dict[str, Any]:
        path = self._soak_path()
        if not path.exists():
            return {"ok": True, "path": str(path), "exists": False, "samples": 0}
        samples = []
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
            for line in lines[-max(1, min(20000, int(limit))) :]:
                try:
                    samples.append(json.loads(line))
                except Exception:
                    pass
        except Exception as exc:
            return {"ok": False, "path": str(path), "error": str(exc)}
        if not samples:
            return {"ok": True, "path": str(path), "exists": True, "samples": 0}
        first, last = samples[0], samples[-1]
        mem_vals = [s.get("memory_mb") for s in samples if isinstance(s.get("memory_mb"), (int, float))]
        bms_online = [((s.get("summary") or {}).get("bms_online")) for s in samples if isinstance((s.get("summary") or {}).get("bms_online"), int)]
        pcs_online = [((s.get("summary") or {}).get("pcs_online")) for s in samples if isinstance((s.get("summary") or {}).get("pcs_online"), int)]
        return {
            "ok": True,
            "path": str(path),
            "exists": True,
            "samples": len(samples),
            "from": first.get("iso"),
            "to": last.get("iso"),
            "elapsed_s": round(float(last.get("ts", 0)) - float(first.get("ts", 0)), 1),
            "memory_mb_min": min(mem_vals) if mem_vals else None,
            "memory_mb_max": max(mem_vals) if mem_vals else None,
            "bms_online_min": min(bms_online) if bms_online else None,
            "bms_online_max": max(bms_online) if bms_online else None,
            "pcs_online_min": min(pcs_online) if pcs_online else None,
            "pcs_online_max": max(pcs_online) if pcs_online else None,
            "last_sample": last,
        }


    def _new_command_ack(self, name: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
        with self._ack_lock:
            self._command_seq += 1
            command_id = f"cmd-{int(time.time() * 1000)}-{self._command_seq}"
            self._command_acks[command_id] = {
                "command_id": command_id,
                "name": name,
                "status": "queued",
                "ok": None,
                "created_ts": time.time(),
                "updated_ts": time.time(),
                "args_preview": [str(x) for x in args[:5]],
                "kwargs_preview": {str(k): str(v) for k, v in list(kwargs.items())[:10] if not str(k).startswith("_")},
                "risk": str(kwargs.get("_risk") or "low"),
                "confirmed": bool(kwargs.get("_confirmed", False)),
                "devices": {},
                "message": "queued to runtime",
            }
            if len(self._command_acks) > 300:
                # Keep bounded memory in long Windows runs.
                keys = sorted(self._command_acks, key=lambda k: self._command_acks[k].get("created_ts", 0.0))
                for old in keys[:-250]:
                    self._command_acks.pop(old, None)
            return command_id

    def _update_command_ack(self, command_id: str | None, **updates: Any) -> None:
        if not command_id:
            return
        with self._ack_lock:
            ack = self._command_acks.get(command_id)
            if not ack:
                return
            ack.update(updates)
            ack["updated_ts"] = time.time()

    def _update_device_ack(self, command_id: str | None, device: str, *, status: str, ok: bool | None = None, message: str = "", result: Any = None) -> None:
        if not command_id or not device:
            return
        with self._ack_lock:
            ack = self._command_acks.get(command_id)
            if not ack:
                return
            devices = ack.setdefault("devices", {})
            devices[str(device)] = {
                "device": str(device),
                "status": status,
                "ok": ok,
                "message": message,
                "result": str(result)[:300] if result is not None else None,
                "updated_ts": time.time(),
            }
            ack["updated_ts"] = time.time()
            vals = list(devices.values())
            if vals and all(v.get("status") in {"write_success", "skipped"} for v in vals):
                ack.update({"status": "device_write_success", "ok": True, "message": "all queued device writes completed"})
            elif any(v.get("status") == "write_failed" for v in vals):
                ack.update({"status": "device_write_failed", "ok": False, "message": "one or more device writes failed"})

    def command_ack(self, command_id: str) -> dict[str, Any]:
        with self._ack_lock:
            ack = dict(self._command_acks.get(str(command_id), {}) or {})
            if ack and isinstance(ack.get("devices"), dict):
                ack["devices"] = dict(ack["devices"])
        return ack or {"ok": False, "error": f"command_id not found: {command_id}"}

    def command_acks_recent(self, limit: int = 50) -> dict[str, Any]:
        with self._ack_lock:
            vals = list(self._command_acks.values())
            vals.sort(key=lambda x: float(x.get("created_ts", 0.0)), reverse=True)
            items = []
            for v in vals[:max(1, min(200, int(limit)) )]:
                item = dict(v)
                if isinstance(item.get("devices"), dict):
                    item["devices"] = dict(item["devices"])
                items.append(item)
        return {"ok": True, "commands": items}


    def _command_audit_path(self) -> Path:
        try:
            if hasattr(self.window, "get_profile_path"):
                return self.window.get_profile_path("command_audit.jsonl")
        except Exception:
            pass
        return Path("command_audit.jsonl")

    def _append_command_audit(self, command_id: str, name: str, result: dict[str, Any] | None = None) -> None:
        """Persist a compact command audit record for field traceability.

        The in-memory ACK list is intentionally bounded for long Windows runs.
        This JSONL audit file keeps the operator-visible command trail on disk
        without adding a database dependency.
        """
        try:
            ack = self.command_ack(command_id)
            result = result or {}
            record = {
                "ts": time.time(),
                "iso": datetime.now().isoformat(timespec="seconds"),
                "api_schema": API_SCHEMA_VERSION,
                "command_id": command_id,
                "name": name,
                "risk": ack.get("risk"),
                "confirmed": ack.get("confirmed"),
                "status": ack.get("status") or result.get("status"),
                "ok": ack.get("ok") if ack.get("ok") is not None else result.get("ok"),
                "message": ack.get("message") or result.get("message") or result.get("error") or "",
                "args_preview": ack.get("args_preview", []),
                "kwargs_preview": ack.get("kwargs_preview", {}),
                "devices": ack.get("devices", {}),
                "result_error": result.get("error", "") if isinstance(result, dict) else "",
            }
            path = self._command_audit_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def command_audit_recent(self, limit: int = 200) -> dict[str, Any]:
        path = self._command_audit_path()
        if not path.exists():
            return {"ok": True, "path": str(path), "exists": False, "commands": []}
        commands: list[dict[str, Any]] = []
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
            for line in lines[-max(1, min(5000, int(limit))) :]:
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict):
                        commands.append(obj)
                except Exception:
                    pass
            commands.sort(key=lambda x: float(x.get("ts", 0.0)), reverse=True)
            return {"ok": True, "path": str(path), "exists": True, "count": len(commands), "commands": commands}
        except Exception as exc:
            return {"ok": False, "path": str(path), "error": str(exc), "commands": commands}

    def command_audit_summary(self, limit: int = 5000) -> dict[str, Any]:
        data = self.command_audit_recent(limit)
        if not data.get("ok"):
            return data
        commands = data.get("commands", []) or []
        by_status: dict[str, int] = {}
        by_risk: dict[str, int] = {}
        by_name: dict[str, int] = {}
        failures: list[dict[str, Any]] = []
        for c in commands:
            st = str((c or {}).get("status") or "unknown")
            rk = str((c or {}).get("risk") or "unknown")
            nm = str((c or {}).get("name") or "unknown")
            by_status[st] = by_status.get(st, 0) + 1
            by_risk[rk] = by_risk.get(rk, 0) + 1
            by_name[nm] = by_name.get(nm, 0) + 1
            if (c or {}).get("ok") is False or "failed" in st or "timeout" in st:
                failures.append(c)
        return {
            "ok": True,
            "path": data.get("path"),
            "exists": data.get("exists", False),
            "count": len(commands),
            "by_status": by_status,
            "by_risk": by_risk,
            "by_name": dict(sorted(by_name.items(), key=lambda kv: kv[1], reverse=True)[:30]),
            "recent_failures": failures[:20],
            "commands": commands[:50],
        }

    def _runtime_state_path(self) -> Path:
        try:
            if hasattr(self.window, "get_profile_path"):
                return self.window.get_profile_path("runtime_state.json")
        except Exception:
            pass
        return Path("runtime_state.json")

    def _load_runtime_state(self) -> dict[str, Any]:
        path = self._runtime_state_path()
        try:
            if path.exists():
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    data.setdefault("path", str(path))
                    return data
        except Exception as exc:
            return {"ok": False, "path": str(path), "error": str(exc)}
        return {"ok": True, "path": str(path), "empty": True, "version": API_SCHEMA_VERSION}

    def _capture_runtime_state(self, reason: str = "snapshot") -> dict[str, Any]:
        """Capture runtime-owned operational state for diagnostics/restart planning.

        This intentionally does not auto-restore power or strategy.  It records
        what was running so the UI/engineer can see the previous runtime state
        after a restart and decide what to re-enable.
        """
        snap_workers: dict[str, Any] = {}
        try:
            w = self.window
            snap_workers = {
                "bms_running": sorted([name for name, worker in (getattr(w, "device_workers", {}) or {}).items() if getattr(worker, "running", False)]),
                "pcs_running": sorted([name for name, worker in (getattr(w, "pcs_workers", {}) or {}).items() if getattr(worker, "running", False)]),
                "strategies": sorted(list(getattr(w, "cluster_strategy_workers", {}) or {})),
            }
        except Exception:
            snap_workers = {"bms_running": [], "pcs_running": [], "strategies": []}
        try:
            recording = self._recording_status()
        except Exception:
            recording = {}
        state = {
            "ok": True,
            "version": API_SCHEMA_VERSION,
            "owner": "runtime",
            "saved_at": time.time(),
            "saved_at_iso": datetime.now().isoformat(timespec="seconds"),
            "reason": reason,
            "workers": snap_workers,
            "recording": recording,
            "last_command": dict(self._last_command_result or {}),
            "command_history": list(self._command_history[-20:]),
            "note": "State is diagnostic only. Runtime will not automatically reconnect devices or restart strategy on boot.",
        }
        return state

    def _persist_runtime_state(self, reason: str = "periodic") -> dict[str, Any]:
        now = time.time()
        if reason == "periodic" and now - float(getattr(self, "_last_persist_ts", 0.0) or 0.0) < 4.0:
            return {"ok": True, "skipped": True, "reason": "throttled"}
        state = self._capture_runtime_state(reason)
        path = self._runtime_state_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
            tmp.replace(path)
            self._last_persist_ts = now
            self._previous_runtime_state = dict(state)
            state["path"] = str(path)
            return state
        except Exception as exc:
            return {"ok": False, "path": str(path), "error": str(exc), "state": state}

    def runtime_state_status(self) -> dict[str, Any]:
        path = self._runtime_state_path()
        current = self._capture_runtime_state("status")
        previous = self._previous_runtime_state if isinstance(self._previous_runtime_state, dict) else {}
        return {
            "ok": True,
            "api_schema": API_SCHEMA_VERSION,
            "path": str(path),
            "current": current,
            "previous": previous,
            "auto_restore_enabled": False,
            "message": "Runtime state is persisted for visibility/restart planning only. No automatic power/strategy restoration is performed.",
        }

    def clear_runtime_state(self) -> dict[str, Any]:
        path = self._runtime_state_path()
        try:
            if path.exists():
                path.unlink()
            self._previous_runtime_state = {"ok": True, "path": str(path), "empty": True, "cleared_at": time.time(), "version": API_SCHEMA_VERSION}
            return {"ok": True, "path": str(path), "cleared": True}
        except Exception as exc:
            return {"ok": False, "path": str(path), "error": str(exc)}


    def _curve_value_from_latest(self, latest: dict[str, Any], signal: str) -> float | None:
        aliases = {
            "soc": ["soc", "SOC", "system_soc", "mbmu_soc", "soc_percent", "soc_value"],
            "voltage": ["voltage", "system_voltage", "total_voltage", "pack_voltage", "dc_voltage", "voltage_value"],
            "current": ["current", "system_current", "pack_current", "dc_current", "current_value"],
            "power": ["power", "power_kw", "actual_power", "active_power", "active_power_kw", "p_kw"],
            "actual_power": ["actual_power", "active_power", "power_kw", "power"],
            "reactive_power": ["reactive_power", "q", "q_kvar", "reactive_power_kvar"],
            "temperature": ["temperature", "max_temperature", "temp", "max_temp"],
            "bms_status": ["bms_status", "status", "state", "system_status"],
            "rack_count": ["number_of_racks", "rack_count", "online_rack_count", "rack_online_count", "racks_online"],
        }
        for key in aliases.get(str(signal), [str(signal)]):
            try:
                if key in latest:
                    value = float(latest.get(key))
                    if value == value:
                        return value
            except Exception:
                continue
        return None

    def _sample_curve_history(self) -> None:
        """Sample compact live curve values from runtime-owned snapshots.

        This timer runs in the Qt runtime thread.  It stores only timestamp/value
        pairs and keeps a bounded deque per device/signal, which is much cheaper
        for browser curves than shipping the full /api/snapshot payload every
        refresh on large sites.
        """
        try:
            task_rows = self._task_status_rows()
            states = self._build_device_states(task_rows)
            now = int(time.time() * 1000)
            signals = ["soc", "voltage", "current", "power", "actual_power", "reactive_power", "temperature", "bms_status", "rack_count"]
            with self._curve_history_lock:
                for kind in ("bms", "pcs"):
                    for name, state in ((states.get(kind) or {}).items() if isinstance(states, dict) else []):
                        if not isinstance(state, dict):
                            continue
                        latest = state.get("latest_values") or state.get("snapshot") or state.get("data") or state
                        if not isinstance(latest, dict):
                            continue
                        for sig in signals:
                            val = self._curve_value_from_latest(latest, sig)
                            if val is None:
                                continue
                            key = f"{kind}:{name}:{sig}"
                            dq = self._curve_history.get(key)
                            if dq is None:
                                dq = deque(maxlen=max(60, self._curve_history_max_samples))
                                self._curve_history[key] = dq
                            if dq and dq[-1].get("y") == val and now - int(dq[-1].get("t") or 0) < 3500:
                                continue
                            dq.append({"t": now, "y": val})
        except Exception:
            # Curve cache must never affect polling/control runtime.
            return

    def compact_snapshot(self) -> dict[str, Any]:
        """Return the light Web polling payload for v9.3 large-site mode."""
        w = self.window
        task_rows = self._task_status_rows()
        device_states = self._build_device_states(task_rows)
        workers = {
            "bms_running": sorted([name for name, worker in (getattr(w, "device_workers", {}) or {}).items() if getattr(worker, "running", False)]),
            "pcs_running": sorted([name for name, worker in (getattr(w, "pcs_workers", {}) or {}).items() if getattr(worker, "running", False)]),
            "strategies": sorted(list(getattr(w, "cluster_strategy_workers", {}) or {})),
        }
        clusters = []
        try:
            site = getattr(w, "site", None)
            for c in getattr(site, "clusters", []) or []:
                clusters.append({
                    "name": getattr(c, "name", ""),
                    "bms_devices": [getattr(d, "name", "") for d in getattr(c, "bms_devices", []) or []],
                    "pcs_devices": [getattr(d, "name", "") for d in getattr(c, "pcs_devices", []) or []],
                    "allocation_mode": getattr(c, "allocation_mode", ""),
                    "power_map": getattr(c, "power_map", {}) or {},
                })
        except Exception as exc:
            clusters = [{"_error": str(exc)}]
        bms_state_map = (device_states or {}).get("bms", {}) if isinstance(device_states, dict) else {}
        pcs_state_map = (device_states or {}).get("pcs", {}) if isinstance(device_states, dict) else {}
        summary = {
            "bms_total": len(bms_state_map),
            "bms_online": sum(1 for v in bms_state_map.values() if isinstance(v, dict) and v.get("online")),
            "bms_error": sum(1 for v in bms_state_map.values() if isinstance(v, dict) and v.get("error")),
            "pcs_total": len(pcs_state_map),
            "pcs_online": sum(1 for v in pcs_state_map.values() if isinstance(v, dict) and v.get("online")),
            "pcs_error": sum(1 for v in pcs_state_map.values() if isinstance(v, dict) and v.get("error")),
            "strategy_count": len(workers.get("strategies", []) or []),
            "bms_running_count": len(workers.get("bms_running", []) or []),
            "pcs_running_count": len(workers.get("pcs_running", []) or []),
        }
        generated_at = time.time()
        return {
            "ok": True,
            "runtime": "ESS-AIO Web EMS Runtime Center phase-9.3 compact",
            "api_schema": API_SCHEMA_VERSION,
            "snapshot_owner": "runtime",
            "compact": True,
            "snapshot_id": f"{int(generated_at * 1000)}:{summary['bms_total']}:{summary['pcs_total']}:{len(self._command_history)}",
            "generated_at": generated_at,
            "uptime_s": round(generated_at - self._started_ts, 1),
            "summary": summary,
            "clusters": clusters,
            "workers": workers,
            "task_status": task_rows,
            "device_states": device_states,
            "recording": self._recording_status(),
            "logs": self._log_status(),
            "soak_test": self.soak_status(),
            "metrics": self.runtime_metrics(),
            "last_command": self._last_command_result,
            "command_history": list(self._command_history[-20:]),
            "command_acks": self.command_acks_recent(20).get("commands", []),
        }

    def curve_history(self, *, signal: str = "soc", device_type: str = "all", device: str = "", multi: bool = True, limit: int = 600) -> dict[str, Any]:
        limit = max(1, min(10000, int(limit or 600)))
        signal = str(signal or "soc")
        kind_filter = str(device_type or "all").lower()
        device_filter = str(device or "")
        series: list[dict[str, Any]] = []
        with self._curve_history_lock:
            for key, dq in sorted(self._curve_history.items()):
                parts = key.split(":", 2)
                if len(parts) != 3:
                    continue
                kind, name, sig = parts
                if sig != signal:
                    continue
                if kind_filter not in {"all", ""} and kind != kind_filter:
                    continue
                if not multi and device_filter and name != device_filter:
                    continue
                if device_filter and multi is False and name != device_filter:
                    continue
                label = f"{name}:{sig}"
                series.append({"name": label, "kind": kind, "device": name, "signal": sig, "data": list(dq)[-limit:]})
        return {"ok": True, "api_schema": API_SCHEMA_VERSION, "series": series, "limit": limit, "cache_keys": len(self._curve_history)}

    def performance_status(self) -> dict[str, Any]:
        metrics = self.runtime_metrics()
        with self._curve_history_lock:
            curve_samples = sum(len(v) for v in self._curve_history.values())
        perf = {
            "ok": True,
            "api_schema": API_SCHEMA_VERSION,
            "large_site_mode": True,
            "web_poll_endpoint": "/api/snapshot/compact",
            "full_snapshot_endpoint": "/api/snapshot",
            "curve_history_endpoint": "/api/curves/live",
            "curve_cache": {
                "series": len(self._curve_history),
                "samples": curve_samples,
                "max_samples_per_series": self._curve_history_max_samples,
                "sample_interval_ms": self._curve_timer.interval() if hasattr(self, "_curve_timer") else None,
            },
            "metrics": metrics,
            "recommendations": [
                "Use /api/snapshot/compact for Web auto-refresh on large sites.",
                "Use /api/device/{kind}/{name}/snapshot only when opening one device detail.",
                "Keep /api/snapshot for export/debug rather than high-frequency polling.",
            ],
        }
        return perf

    def health_monitor(self) -> dict[str, Any]:
        """v9.4: operator-facing runtime health/self-check summary.

        This is intentionally read-only. It aggregates compact snapshot, worker
        status, command queue, curve cache, process metrics and data freshness so
        Web/UI users can tell whether the site is healthy without opening a full
        snapshot or reading logs.
        """
        now = time.time()
        snap = self.compact_snapshot()
        metrics = self.runtime_metrics()
        summary = snap.get("summary", {}) or {}
        workers = metrics.get("workers", {}) or {}
        queues = metrics.get("queues", {}) or {}
        process = metrics.get("process", {}) or {}
        command_counts = ((metrics.get("commands", {}) or {}).get("status_counts", {}) or {})

        device_states = snap.get("device_states", {}) or {}
        stale_devices: list[dict[str, Any]] = []
        offline_devices: list[dict[str, Any]] = []
        error_devices: list[dict[str, Any]] = []

        def _scan(kind: str, rows: Any) -> None:
            if not isinstance(rows, dict):
                return
            for name, state in rows.items():
                if not isinstance(state, dict):
                    continue
                online = bool(state.get("online"))
                err = state.get("error") or state.get("last_error") or ""
                ts = state.get("timestamp") or state.get("updated_ts") or state.get("last_update_ts") or state.get("last_seen_ts")
                age = None
                try:
                    age = round(now - float(ts), 1) if ts else None
                except Exception:
                    age = None
                row = {"kind": kind, "device": str(name), "online": online, "age_s": age, "error": str(err or "")}
                if not online:
                    offline_devices.append(row)
                if err:
                    error_devices.append(row)
                if age is not None and age > 10:
                    stale_devices.append(row)

        _scan("bms", device_states.get("bms"))
        _scan("pcs", device_states.get("pcs"))

        issues: list[dict[str, Any]] = []
        if int(summary.get("bms_total") or 0) and int(summary.get("bms_online") or 0) < int(summary.get("bms_total") or 0):
            issues.append({"severity": "warning", "area": "bms", "message": "Some BMS devices are offline"})
        if int(summary.get("pcs_total") or 0) and int(summary.get("pcs_online") or 0) < int(summary.get("pcs_total") or 0):
            issues.append({"severity": "warning", "area": "pcs", "message": "Some PCS devices are offline"})
        if stale_devices:
            issues.append({"severity": "warning", "area": "freshness", "message": f"{len(stale_devices)} device snapshots look stale (>10s)"})
        if error_devices:
            issues.append({"severity": "error", "area": "device", "message": f"{len(error_devices)} devices report errors"})
        if int(queues.get("runtime_command_queue") or 0) > 0:
            issues.append({"severity": "warning", "area": "commands", "message": "Runtime command queue is not empty"})
        if int(queues.get("pending_command_acks") or 0) > 0:
            issues.append({"severity": "warning", "area": "commands", "message": "There are pending runtime command acknowledgements"})
        failed_cmds = sum(int(v or 0) for k, v in command_counts.items() if str(k).lower() in {"failed", "runtime_timeout", "error"})
        if failed_cmds:
            issues.append({"severity": "warning", "area": "commands", "message": f"{failed_cmds} command acknowledgements are failed/timeout"})

        status = "healthy"
        if any(i.get("severity") == "error" for i in issues):
            status = "fault"
        elif issues:
            status = "warning"

        score = 100
        score -= 15 * len([i for i in issues if i.get("severity") == "error"])
        score -= 7 * len([i for i in issues if i.get("severity") == "warning"])
        score = max(0, min(100, score))

        with self._curve_history_lock:
            curve_series = len(self._curve_history)
            curve_samples = sum(len(v) for v in self._curve_history.values())

        return {
            "ok": status != "fault",
            "api_schema": API_SCHEMA_VERSION,
            "status": status,
            "score": score,
            "generated_at": now,
            "uptime_s": round(now - self._started_ts, 1),
            "process": process,
            "summary": summary,
            "workers": workers,
            "queues": queues,
            "commands": {"status_counts": command_counts, "failed_or_timeout": failed_cmds},
            "freshness": {"stale_devices": stale_devices[:100], "offline_devices": offline_devices[:100], "error_devices": error_devices[:100]},
            "curve_cache": {"series": curve_series, "samples": curve_samples, "max_samples_per_series": self._curve_history_max_samples},
            "issues": issues,
            "recommendations": [
                "If stale_devices grows while workers are running, check polling interval, Modbus timeouts and network latency.",
                "If command queue stays non-empty, avoid repeated button clicks and inspect command audit.",
                "For large sites, keep Web auto-refresh on compact snapshot and use Curves live cache instead of full snapshot polling.",
            ],
        }

    def health_monitor_csv(self) -> str:
        data = self.health_monitor()
        rows = [["section", "key", "value"]]
        rows.append(["runtime", "api_schema", data.get("api_schema", "")])
        rows.append(["runtime", "status", data.get("status", "")])
        rows.append(["runtime", "score", data.get("score", "")])
        rows.append(["runtime", "uptime_s", data.get("uptime_s", "")])
        for k, v in (data.get("summary", {}) or {}).items():
            rows.append(["summary", k, v])
        for k, v in (data.get("workers", {}) or {}).items():
            rows.append(["workers", k, v])
        for k, v in (data.get("queues", {}) or {}).items():
            rows.append(["queues", k, v])
        for i, issue in enumerate(data.get("issues", []) or [], 1):
            rows.append(["issue", str(i), json.dumps(issue, ensure_ascii=False)])
        import csv, io
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerows(rows)
        return buf.getvalue()

    def runtime_metrics(self) -> dict[str, Any]:
        """Return lightweight runtime metrics for long-running Windows sites."""
        try:
            command_status_counts: dict[str, int] = {}
            with self._ack_lock:
                for c in self._command_acks.values():
                    st = str(c.get("status") or "unknown")
                    command_status_counts[st] = command_status_counts.get(st, 0) + 1
                pending_commands = sum(1 for c in self._command_acks.values() if str(c.get("status") or "").startswith("queued") or str(c.get("status") or "") == "executing")
        except Exception:
            command_status_counts = {}
            pending_commands = 0
        w = self.window
        try:
            bms_workers = getattr(w, "device_workers", {}) or {}
            pcs_workers = getattr(w, "pcs_workers", {}) or {}
            strategy_workers = getattr(w, "cluster_strategy_workers", {}) or {}
        except Exception:
            bms_workers, pcs_workers, strategy_workers = {}, {}, {}
        return {
            "ok": True,
            "api_schema": API_SCHEMA_VERSION,
            "pid": os.getpid(),
            "uptime_s": round(time.time() - self._started_ts, 1),
            "process": {
                "python": sys.version.split()[0],
                "thread_count": threading.active_count(),
                "memory_mb": self._process_memory_mb(),
                "gc_counts": list(gc.get_count()),
            },
            "queues": {
                "runtime_command_queue": self._commands.qsize(),
                "pending_command_acks": pending_commands,
            },
            "workers": {
                "bms_total": len(bms_workers),
                "bms_running": sum(1 for x in bms_workers.values() if getattr(x, "running", False)),
                "pcs_total": len(pcs_workers),
                "pcs_running": sum(1 for x in pcs_workers.values() if getattr(x, "running", False)),
                "strategy_running": len(strategy_workers),
            },
            "commands": {
                "history_len": len(self._command_history),
                "ack_count": len(self._command_acks),
                "status_counts": command_status_counts,
                "audit_path": str(self._command_audit_path()),
            },
            "soak_test": self.soak_status(),
        }

    def restore_plan(self) -> dict[str, Any]:
        """Return a safe restore plan based on persisted state.

        The plan is diagnostic by default. Operators can call /api/runtime/restore
        with explicit flags to restart polling/CSV. Strategy restore is disabled by
        default because it can cause power dispatch.
        """
        prev = self._previous_runtime_state if isinstance(self._previous_runtime_state, dict) else {}
        workers = prev.get("workers", {}) if isinstance(prev.get("workers", {}), dict) else {}
        rec = prev.get("recording", {}) if isinstance(prev.get("recording", {}), dict) else {}
        return {
            "ok": True,
            "api_schema": API_SCHEMA_VERSION,
            "previous_state_path": str(self._runtime_state_path()),
            "previous_saved_at_iso": prev.get("saved_at_iso"),
            "restartable": {
                "bms": list(workers.get("bms_running", []) or []),
                "pcs": list(workers.get("pcs_running", []) or []),
                "strategies": list(workers.get("strategies", []) or []),
                "bms_csv": list(rec.get("bms_csv", []) or []),
                "pcs_csv": list(rec.get("pcs_csv", []) or []),
            },
            "defaults": {
                "restore_bms": False,
                "restore_pcs": False,
                "restore_csv": False,
                "restore_strategy": False,
            },
            "safety_note": "No restore is automatic. Strategy restore remains opt-in and should be used only after device state is verified.",
        }

    def restore_runtime_state(self, *, restore_bms: bool = False, restore_pcs: bool = False, restore_csv: bool = False, restore_strategy: bool = False) -> dict[str, Any]:
        plan = self.restore_plan().get("restartable", {}) or {}
        actions: list[dict[str, Any]] = []
        errors: list[str] = []
        w = self.window
        if restore_bms:
            for name in plan.get("bms", []) or []:
                try:
                    w.start_device_by_name(str(name))
                    actions.append({"type": "bms_start", "name": str(name), "ok": True})
                except Exception as exc:
                    errors.append(f"BMS {name}: {exc}")
                    actions.append({"type": "bms_start", "name": str(name), "ok": False, "error": str(exc)})
        if restore_pcs:
            for name in plan.get("pcs", []) or []:
                try:
                    w.start_pcs_polling_by_name(str(name))
                    actions.append({"type": "pcs_connect", "name": str(name), "ok": True})
                except Exception as exc:
                    errors.append(f"PCS {name}: {exc}")
                    actions.append({"type": "pcs_connect", "name": str(name), "ok": False, "error": str(exc)})
        if restore_csv:
            try:
                bms_res = self._start_bms_csv_runtime([str(x) for x in (plan.get("bms_csv", []) or [])]) if plan.get("bms_csv") else {"ok": True, "started": []}
                pcs_res = self._start_pcs_csv_runtime([str(x) for x in (plan.get("pcs_csv", []) or [])]) if plan.get("pcs_csv") else {"ok": True, "started": []}
                actions.append({"type": "csv_restore", "bms": bms_res, "pcs": pcs_res, "ok": bool(bms_res.get("ok") and pcs_res.get("ok"))})
            except Exception as exc:
                errors.append(f"CSV: {exc}")
        if restore_strategy:
            # Strategy restore is deliberately conservative.  It selects and starts
            # the saved strategies, but does not change target power or reconnect devices.
            for cluster in plan.get("strategies", []) or []:
                try:
                    if hasattr(w, "cluster_strategy_combo"):
                        idx = w.cluster_strategy_combo.findText(str(cluster))
                        if idx >= 0:
                            w.cluster_strategy_combo.setCurrentIndex(idx)
                    w.start_cluster_strategy()
                    actions.append({"type": "strategy_start", "cluster": str(cluster), "ok": True})
                except Exception as exc:
                    errors.append(f"Strategy {cluster}: {exc}")
                    actions.append({"type": "strategy_start", "cluster": str(cluster), "ok": False, "error": str(exc)})
        self._persist_runtime_state("restore")
        return {"ok": not errors, "actions": actions, "errors": errors, "restore_flags": {"bms": restore_bms, "pcs": restore_pcs, "csv": restore_csv, "strategy": restore_strategy}}

    def verify_command_ack(self, command_id: str) -> dict[str, Any]:
        """Best-effort command verification from current Runtime snapshots.

        This does not replace device protocol readback, but it makes queued-vs-
        observed state explicit and is safe for all profiles.
        """
        ack = self.command_ack(command_id)
        if not ack.get("command_id"):
            return ack
        devices = ack.get("devices", {}) if isinstance(ack.get("devices"), dict) else {}
        snap = self.snapshot()
        states = snap.get("device_states", {}) or {}
        bms_states = states.get("bms", {}) or {}
        pcs_states = states.get("pcs", {}) or {}
        verification = {}
        for name, d in devices.items():
            st = bms_states.get(name) or pcs_states.get(name) or {}
            verification[name] = {
                "device": name,
                "ack_status": (d or {}).get("status"),
                "ack_ok": (d or {}).get("ok"),
                "observed_connection": st.get("connection", "unknown"),
                "observed_online": bool(st.get("online", False)),
                "last_message": st.get("last_message", ""),
                "verified": bool((d or {}).get("ok") is True and st.get("online", False)),
                "note": "Best-effort snapshot verification. Protocol-specific readback verification can be added per PCS/BMS profile.",
            }
        ack["verification"] = verification
        ack["verified_ok"] = bool(verification) and all(v.get("verified") for v in verification.values())
        ack["verified_at"] = time.time()
        return ack

    def snapshot(self) -> dict[str, Any]:
        w = self.window
        # Copy only lightweight serializable state. This must not touch widgets.
        bms = dict(getattr(w, "latest_snapshots", {}) or {})
        pcs = dict(getattr(w, "latest_pcs_snapshots", {}) or {})
        fleet = {}
        try:
            fleet_manager = getattr(w, "fleet_manager", None)
            if fleet_manager is not None:
                fleet = fleet_manager.snapshots()
        except Exception as exc:
            fleet = {"_error": str(exc)}

        clusters = []
        try:
            site = getattr(w, "site", None)
            for c in getattr(site, "clusters", []) or []:
                clusters.append({
                    "name": getattr(c, "name", ""),
                    "bms_devices": [getattr(d, "name", "") for d in getattr(c, "bms_devices", []) or []],
                    "pcs_devices": [getattr(d, "name", "") for d in getattr(c, "pcs_devices", []) or []],
                    "allocation_mode": getattr(c, "allocation_mode", ""),
                    "power_map": getattr(c, "power_map", {}) or {},
                })
        except Exception as exc:
            clusters = [{"_error": str(exc)}]

        task_rows = self._task_status_rows()
        device_states = self._build_device_states(task_rows)
        workers = {
            "bms_running": sorted([name for name, worker in (getattr(w, "device_workers", {}) or {}).items() if getattr(worker, "running", False)]),
            "pcs_running": sorted([name for name, worker in (getattr(w, "pcs_workers", {}) or {}).items() if getattr(worker, "running", False)]),
            "strategies": sorted(list(getattr(w, "cluster_strategy_workers", {}) or {})),
        }
        recording = self._recording_status()
        logs = self._log_status()
        try:
            bms_state_map = device_states.get("bms", {}) if isinstance(device_states, dict) else {}
            pcs_state_map = device_states.get("pcs", {}) if isinstance(device_states, dict) else {}
            summary = {
                "bms_total": len(bms_state_map),
                "bms_online": sum(1 for v in bms_state_map.values() if isinstance(v, dict) and v.get("online")),
                "bms_error": sum(1 for v in bms_state_map.values() if isinstance(v, dict) and v.get("error")),
                "pcs_total": len(pcs_state_map),
                "pcs_online": sum(1 for v in pcs_state_map.values() if isinstance(v, dict) and v.get("online")),
                "pcs_error": sum(1 for v in pcs_state_map.values() if isinstance(v, dict) and v.get("error")),
                "strategy_count": len(workers.get("strategies", []) or []),
                "bms_running_count": len(workers.get("bms_running", []) or []),
                "pcs_running_count": len(workers.get("pcs_running", []) or []),
            }
        except Exception:
            summary = {}
        generated_at = time.time()
        snapshot_id = f"{int(generated_at * 1000)}:{len(bms)}:{len(pcs)}:{len(self._command_history)}"
        return {
            "ok": True,
            "runtime": "ESS-AIO Web EMS Runtime Center phase-8.5",
            "api_schema": API_SCHEMA_VERSION,
            "snapshot_owner": "runtime",
            "snapshot_id": snapshot_id,
            "generated_at": generated_at,
            "uptime_s": round(generated_at - self._started_ts, 1),
            "summary": summary,
            "bms": bms,
            "pcs": pcs,
            "fleet": fleet,
            "clusters": clusters,
            "workers": workers,
            "task_status": task_rows,
            "device_states": device_states,
            "recording": recording,
            "logs": logs,
            "soak_test": self.soak_status(),
            "metrics": self.runtime_metrics(),
            "runtime_state": {
                "path": str(self._runtime_state_path()),
                "previous": self._previous_runtime_state,
                "current": self._capture_runtime_state("snapshot"),
                "auto_restore_enabled": False,
            },
            "last_command": self._last_command_result,
            "command_history": list(self._command_history[-20:]),
            "command_acks": self.command_acks_recent(20).get("commands", []),
        }


    def runtime_center(self) -> dict[str, Any]:
        """Build a site-level EMS runtime view for the Web Runtime Center."""
        snap = self.snapshot()
        summary = dict(snap.get("summary") or {})
        bms_states = ((snap.get("device_states") or {}).get("bms") or {})
        pcs_states = ((snap.get("device_states") or {}).get("pcs") or {})
        clusters = snap.get("clusters") or []
        workers = snap.get("workers") or {}
        commands = snap.get("command_acks") or []
        def count_online(rows):
            return sum(1 for v in rows.values() if isinstance(v, dict) and v.get("online"))
        def count_error(rows):
            return sum(1 for v in rows.values() if isinstance(v, dict) and v.get("error"))
        bms_total, pcs_total = len(bms_states), len(pcs_states)
        bms_online, pcs_online = count_online(bms_states), count_online(pcs_states)
        bms_error, pcs_error = count_error(bms_states), count_error(pcs_states)
        strategy_running = len(workers.get("strategies", []) or [])
        running_bms = len(workers.get("bms_running", []) or [])
        running_pcs = len(workers.get("pcs_running", []) or [])
        failed_commands = [c for c in commands if isinstance(c, dict) and str(c.get("status", "")).lower() in {"failed", "runtime_timeout", "device_write_failed"}]
        site_state = "Running"
        if bms_error or pcs_error or failed_commands:
            site_state = "Fault"
        elif (bms_total and bms_online < bms_total) or (pcs_total and pcs_online < pcs_total):
            site_state = "Warning"
        elif not (running_bms or running_pcs or strategy_running):
            site_state = "Standby"
        return {
            "ok": True,
            "api_schema": API_SCHEMA_VERSION,
            "generated_at": snap.get("generated_at"),
            "uptime_s": snap.get("uptime_s"),
            "site_state": site_state,
            "site": {
                "bms_total": bms_total,
                "bms_online": bms_online,
                "bms_error": bms_error,
                "pcs_total": pcs_total,
                "pcs_online": pcs_online,
                "pcs_error": pcs_error,
                "clusters": len(clusters),
                "strategies_running": strategy_running,
                "commands_failed_recent": len(failed_commands),
            },
            "workers": workers,
            "recording": snap.get("recording") or {},
            "soak_test": snap.get("soak_test") or {},
            "clusters": clusters,
            "recent_commands": commands[:20],
            "summary": summary,
        }

    def _alarm_ack_path(self) -> Path:
        try:
            if hasattr(self.window, "get_profile_path"):
                return self.window.get_profile_path("alarm_ack.json")
        except Exception:
            pass
        return Path("alarm_ack.json")

    def _load_alarm_ack(self) -> dict[str, Any]:
        path = self._alarm_ack_path()
        try:
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                return data if isinstance(data, dict) else {}
        except Exception:
            pass
        return {}

    def _save_alarm_ack(self, data: dict[str, Any]) -> None:
        path = self._alarm_ack_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _alarm_key(self, item: dict[str, Any]) -> str:
        device = str(item.get("device") or "")
        severity = str(item.get("severity") or "")
        signal = str(item.get("key") or item.get("message") or "")
        return f"{severity}|{device}|{signal}"

    def alarm_center(self, limit: int = 200, severity: str = "", query: str = "", include_ack: bool = True) -> dict[str, Any]:
        """Best-effort alarm center from active BMS alarm snapshots and runtime errors."""
        snap = self.snapshot()
        bms_states = ((snap.get("device_states") or {}).get("bms") or {})
        active: list[dict[str, Any]] = []
        by_device: dict[str, int] = {}
        by_text: dict[str, int] = {}
        ack_map = self._load_alarm_ack()
        for name, state in bms_states.items():
            latest = {}
            if isinstance(state, dict):
                latest = state.get("latest_values") or state.get("snapshot") or {}
            for k, v in (latest or {}).items() if isinstance(latest, dict) else []:
                key_l = str(k).lower()
                if "alarm" not in key_l and "fault" not in key_l and "warning" not in key_l:
                    continue
                try:
                    is_active = bool(v) and str(v) not in {"0", "0.0", "False", "false", "None", ""}
                except Exception:
                    is_active = False
                if is_active:
                    sev = "alarm" if "alarm" in key_l or "fault" in key_l else "warning"
                    item = {"device": name, "kind": "bms", "key": str(k), "value": v, "severity": sev}
                    active.append(item)
                    by_device[name] = by_device.get(name, 0) + 1
                    by_text[str(k)] = by_text.get(str(k), 0) + 1
        for kind in ("bms", "pcs"):
            for name, state in (((snap.get("device_states") or {}).get(kind) or {}).items()):
                if isinstance(state, dict) and state.get("error"):
                    msg = state.get("last_message") or state.get("status") or "device error"
                    active.append({"device": name, "kind": kind, "message": msg, "severity": "error", "value": state.get("status") or "error"})
                    by_device[name] = by_device.get(name, 0) + 1
                    by_text[str(msg)] = by_text.get(str(msg), 0) + 1
        for item in active:
            key = self._alarm_key(item)
            item["alarm_id"] = key
            item["acknowledged"] = key in ack_map
            if key in ack_map and isinstance(ack_map.get(key), dict):
                item["ack"] = ack_map.get(key)
        sev_filter = str(severity or "").strip().lower()
        q = str(query or "").strip().lower()
        filtered = []
        for item in active:
            if sev_filter and str(item.get("severity", "")).lower() != sev_filter:
                continue
            text = json.dumps(item, ensure_ascii=False).lower()
            if q and q not in text:
                continue
            if not include_ack and item.get("acknowledged"):
                continue
            filtered.append(item)
        filtered = filtered[:max(1, int(limit or 200))]
        return {
            "ok": True,
            "api_schema": API_SCHEMA_VERSION,
            "generated_at": snap.get("generated_at"),
            "active_count": len(active),
            "filtered_count": len(filtered),
            "ack_count": sum(1 for x in active if x.get("acknowledged")),
            "active": filtered,
            "top_by_device": sorted(by_device.items(), key=lambda x: x[1], reverse=True)[:20],
            "top_by_signal": sorted(by_text.items(), key=lambda x: x[1], reverse=True)[:20],
            "logs": snap.get("logs") or {},
            "ack_path": str(self._alarm_ack_path()),
            "note": "Alarm Center v8.7 supports filters, acknowledge records, and CSV export from current runtime snapshot.",
        }

    def alarm_acknowledge(self, alarm_id: str, note: str = "") -> dict[str, Any]:
        alarm_id = str(alarm_id or "").strip()
        if not alarm_id:
            return {"ok": False, "error": "alarm_id is required"}
        data = self._load_alarm_ack()
        data[alarm_id] = {"ts": time.time(), "iso": datetime.now().isoformat(timespec="seconds"), "note": str(note or "")}
        self._save_alarm_ack(data)
        return {"ok": True, "alarm_id": alarm_id, "ack": data[alarm_id], "path": str(self._alarm_ack_path())}

    def alarm_clear_ack(self, alarm_id: str = "") -> dict[str, Any]:
        data = self._load_alarm_ack()
        alarm_id = str(alarm_id or "").strip()
        if alarm_id:
            removed = data.pop(alarm_id, None) is not None
        else:
            removed = bool(data); data = {}
        self._save_alarm_ack(data)
        return {"ok": True, "removed": removed, "remaining": len(data), "path": str(self._alarm_ack_path())}

    def alarm_center_csv(self, limit: int = 1000) -> str:
        import csv
        import io
        data = self.alarm_center(limit=limit, include_ack=True)
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["generated_at", "severity", "kind", "device", "signal_or_message", "value", "acknowledged", "ack_iso", "ack_note", "alarm_id"])
        for a in data.get("active", []) or []:
            ack = a.get("ack") if isinstance(a.get("ack"), dict) else {}
            w.writerow([data.get("generated_at"), a.get("severity"), a.get("kind"), a.get("device"), a.get("key") or a.get("message"), a.get("value"), a.get("acknowledged"), ack.get("iso", ""), ack.get("note", ""), a.get("alarm_id")])
        return out.getvalue()

    def _strategy_config_path(self):
        w = self.window
        if hasattr(w, "get_profile_path"):
            return w.get_profile_path("strategy.json")
        return Path("strategy.json")

    def _read_strategy_config_payload(self) -> dict[str, Any]:
        path = self._strategy_config_path()
        try:
            if path.exists():
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            else:
                data = {
                    "name": "Default Safety Strategy",
                    "version": "1.0",
                    "enabled": True,
                    "overrides": {},
                    "runtime_overrides": {},
                    "rules": [],
                }
            if not isinstance(data, dict):
                return {"ok": False, "path": str(path), "error": "strategy.json must be a JSON object", "config": {}}
            return {"ok": True, "path": str(path), "config": data}
        except Exception as exc:
            return {"ok": False, "path": str(path), "error": str(exc), "config": {}}

    def _write_strategy_config_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        path = self._strategy_config_path()
        data = payload.get("config", payload)
        if not isinstance(data, dict):
            return {"ok": False, "error": "strategy config payload must be a JSON object"}
        data.setdefault("name", "Default Safety Strategy")
        data.setdefault("version", "1.0")
        data.setdefault("enabled", True)
        data.setdefault("overrides", {})
        data.setdefault("runtime_overrides", {})
        data.setdefault("rules", [])
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            return {"ok": True, "path": str(path), "enabled": bool(data.get("enabled", True)), "rule_count": len(data.get("rules") or [])}
        except Exception as exc:
            return {"ok": False, "path": str(path), "error": str(exc)}

    def strategy_center(self) -> dict[str, Any]:
        snap = self.snapshot()
        workers = snap.get("workers") or {}
        running = set(workers.get("strategies", []) or [])
        strategy_cfg = self._read_strategy_config_payload()
        cfg = strategy_cfg.get("config") or {}
        overrides = cfg.get("overrides") or {}
        rows = []
        issues = []
        for c in snap.get("clusters") or []:
            if not isinstance(c, dict):
                continue
            bms = c.get("bms_devices", []) or []
            pcs = c.get("pcs_devices", []) or []
            cluster_name = c.get("name", "")
            health = "ready"
            if not bms:
                health = "missing_bms"
                issues.append({"cluster": cluster_name, "severity": "error", "message": "Cluster has no BMS bound"})
            if not pcs:
                health = "missing_pcs" if health == "ready" else "incomplete"
                issues.append({"cluster": cluster_name, "severity": "warning", "message": "Cluster has no PCS bound"})
            rows.append({
                "cluster": cluster_name,
                "bms_devices": bms,
                "pcs_devices": pcs,
                "allocation_mode": c.get("allocation_mode", ""),
                "power_map": c.get("power_map", {}),
                "running": cluster_name in running,
                "health": health,
            })
        if not cfg.get("enabled", True):
            issues.append({"cluster": "site", "severity": "warning", "message": "Profile strategy.json is disabled"})
        recent_strategy_commands = [
            c for c in (snap.get("command_acks") or [])
            if isinstance(c, dict) and ("strategy" in str(c.get("name", "")).lower() or "cluster" in str(c.get("name", "")).lower())
        ][:20]
        return {
            "ok": True,
            "api_schema": API_SCHEMA_VERSION,
            "generated_at": snap.get("generated_at"),
            "strategies_running": len(running),
            "clusters_total": len(rows),
            "clusters_ready": sum(1 for r in rows if r.get("health") == "ready"),
            "clusters": rows,
            "workers": workers,
            "profile_strategy": {
                "ok": strategy_cfg.get("ok"),
                "path": strategy_cfg.get("path"),
                "enabled": bool(cfg.get("enabled", True)),
                "name": cfg.get("name", ""),
                "version": cfg.get("version", ""),
                "description": cfg.get("description", ""),
                "overrides": overrides,
                "runtime_overrides": cfg.get("runtime_overrides") or {},
                "rules": cfg.get("rules") or [],
                "fake_tests": cfg.get("fake_tests") or [],
            },
            "issues": issues,
            "recent_strategy_commands": recent_strategy_commands,
            "safety_note": "Start/target-power operations require browser confirmation. Strategy config changes do not auto-start any cluster.",
        }

    def enqueue(self, name: str, *args: Any, timeout_s: float | None = None, **kwargs: Any) -> dict[str, Any]:
        command_id = self._new_command_ack(name, args, kwargs)
        cmd = RuntimeCommand(command_id=command_id, name=name, args=args, kwargs=kwargs, done=threading.Event())
        self._commands.put(cmd)
        self._update_command_ack(command_id, status="queued_to_runtime", message="queued to runtime command loop")
        if not cmd.done.wait(timeout=float(timeout_s or self.command_timeout_s)):
            self._update_command_ack(command_id, status="runtime_timeout", ok=False, message=f"Runtime command timed out: {name}")
            return {"ok": False, "command_id": command_id, "status": "runtime_timeout", "error": f"Command timed out: {name}"}
        result = cmd.result or {"ok": False, "error": f"Command returned no result: {name}"}
        result.setdefault("command_id", command_id)
        result.setdefault("status", self.command_ack(command_id).get("status", "unknown"))
        return result

    def _drain_commands(self) -> None:
        # Limit per tick so API command bursts cannot freeze the runtime thread.
        for _ in range(10):
            try:
                cmd = self._commands.get_nowait()
            except queue.Empty:
                return
            try:
                self._update_command_ack(cmd.command_id, status="executing", message="executing in runtime")
                cmd.result = self._execute(cmd.name, *cmd.args, _command_id=cmd.command_id, **cmd.kwargs)
                if isinstance(cmd.result, dict):
                    cmd.result.setdefault("command_id", cmd.command_id)
                status = (cmd.result or {}).get("status") or ("completed" if (cmd.result or {}).get("ok") else "failed")
                # Do not overwrite a later device_write_success/device_write_failed status.
                current_status = self.command_ack(cmd.command_id).get("status")
                if current_status not in {"device_write_success", "device_write_failed"}:
                    self._update_command_ack(cmd.command_id, status=status, ok=bool((cmd.result or {}).get("ok")), message=(cmd.result or {}).get("error") or (cmd.result or {}).get("message") or status)
            except Exception as exc:
                cmd.result = {"ok": False, "command": cmd.name, "command_id": cmd.command_id, "status": "failed", "error": str(exc)}
                self._update_command_ack(cmd.command_id, status="failed", ok=False, message=str(exc))
            finally:
                try:
                    rec = {
                        "ts": time.time(),
                        "command_id": cmd.command_id,
                        "name": cmd.name,
                        "status": (cmd.result or {}).get("status", ""),
                        "ok": bool((cmd.result or {}).get("ok")),
                        "error": (cmd.result or {}).get("error", ""),
                    }
                    self._last_command_result = dict(cmd.result or {})
                    self._command_history.append(rec)
                    if len(self._command_history) > 50:
                        self._command_history = self._command_history[-50:]
                    self._append_command_audit(cmd.command_id, cmd.name, cmd.result if isinstance(cmd.result, dict) else {})
                    # Persist runtime state after every command. This records the
                    # latest operational state without auto-restarting anything.
                    self._persist_runtime_state(f"command:{cmd.name}")
                except Exception:
                    pass
                cmd.done.set()

    def _bms_worker_names(self) -> list[str]:
        w = self.window
        if hasattr(w, "_online_bms_worker_names"):
            try:
                return list(w._online_bms_worker_names())
            except Exception:
                pass
        names = []
        for dev_name, worker in getattr(w, "device_workers", {}).items():
            try:
                if getattr(worker, "running", False) and hasattr(worker, "enqueue_command"):
                    names.append(str(dev_name))
            except Exception:
                pass
        return names



    def _task_status_rows(self) -> dict[str, dict[str, Any]]:
        """Return latest task/status rows keyed by device name."""
        try:
            store = getattr(self.window, "task_status_store", None)
            if store is None or not hasattr(store, "rows"):
                return {}
            rows = store.rows()
            return {str(row.get("device_name", "")): dict(row) for row in rows if row.get("device_name")}
        except Exception:
            return {}

    def _build_device_states(self, task_rows: dict[str, dict[str, Any]]) -> dict[str, dict[str, dict[str, Any]]]:
        """Build serializable BMS/PCS online/offline/error state for the API client UI.

        Runtime phase 5 must explicitly report failed/offline devices.  A device
        can be present in a worker dict even after its thread stopped, so we use
        the worker.running flag, task status, and latest snapshot together.
        """
        w = self.window
        now = time.time()

        def _num_from_snapshot(snapshot: dict[str, Any], keys: list[str]) -> float | None:
            for key in keys:
                if key in snapshot:
                    try:
                        value = snapshot.get(key)
                        if isinstance(value, str):
                            value = value.strip().replace("%", "")
                        return float(value)
                    except Exception:
                        continue
            return None

        def _first_present(snapshot: dict[str, Any], keys: list[str]) -> Any:
            for key in keys:
                try:
                    value = snapshot.get(key)
                    if value is not None and value != "":
                        return value
                except Exception:
                    continue
            return None

        def _latest_values(snapshot: dict[str, Any]) -> dict[str, Any]:
            snapshot = snapshot or {}
            charge_p = _num_from_snapshot(snapshot, ["charge_active_power", "charging_active_power", "active_charge_power", "active_power_charge"])
            if charge_p is None and "discharge_active_power" in snapshot:
                # Kehua profiles expose charge P as active_power (7022) and discharge P as discharge_active_power (7024).
                charge_p = _num_from_snapshot(snapshot, ["active_power"])
            discharge_p = _num_from_snapshot(snapshot, ["discharge_active_power", "discharging_active_power", "active_discharge_power", "active_power_discharge"])
            running_p = _num_from_snapshot(snapshot, ["actual_power", "running_active_power", "running_active_power_kw", "p", "p_kw", "power_kw", "power"])
            if discharge_p is not None or charge_p is not None:
                if discharge_p is not None and abs(float(discharge_p)) > 1e-9:
                    running_p = float(discharge_p)
                elif charge_p is not None and abs(float(charge_p)) > 1e-9:
                    running_p = -float(charge_p)
                else:
                    running_p = float(discharge_p if discharge_p is not None else -(charge_p or 0.0))

            capacitive_q = _num_from_snapshot(snapshot, ["capacitive_reactive_power", "reactive_power", "q_cap", "q_capacitive"])
            inductive_q = _num_from_snapshot(snapshot, ["inductive_reactive_power", "q_ind", "q_inductive"])
            running_q = _num_from_snapshot(snapshot, ["actual_reactive_power", "running_reactive_power", "running_reactive_power_kvar", "q", "q_kvar", "reactive_power_kvar"])
            if capacitive_q is not None or inductive_q is not None:
                if capacitive_q is not None and abs(float(capacitive_q)) > 1e-9:
                    running_q = float(capacitive_q)
                elif inductive_q is not None and abs(float(inductive_q)) > 1e-9:
                    running_q = -float(inductive_q)
                else:
                    running_q = float(capacitive_q if capacitive_q is not None else -(inductive_q or 0.0))

            values = {
                "soc": _num_from_snapshot(snapshot, ["soc", "SOC", "system_soc", "mbmu_soc", "soc_percent", "soc_value"]),
                "voltage": _num_from_snapshot(snapshot, ["voltage", "system_voltage", "total_voltage", "pack_voltage", "dc_voltage", "voltage_value"]),
                "current": _num_from_snapshot(snapshot, ["current", "system_current", "pack_current", "dc_current", "current_value"]),
                "power": _num_from_snapshot(snapshot, ["power", "system_power", "power_kw", "actual_power", "active_power", "active_power_kw", "dc_power", "p_kw"]),
                "active_power": running_p,
                "charge_active_power": charge_p,
                "discharge_active_power": discharge_p,
                "reactive_power": running_q,
                "capacitive_reactive_power": capacitive_q,
                "inductive_reactive_power": inductive_q,
                "bms_status": _first_present(snapshot, ["bms_status", "system_status", "status", "state", "work_status", "running_status"]),
                "bms_power_on": _first_present(snapshot, ["bms_power_on", "power_on", "power_on_status"]),
                "hv_online_racks": _num_from_snapshot(snapshot, ["number_of_hv_connected_racks", "hv_online_racks", "online_rack_count", "rack_online_count", "racks_online"]),
                "number_of_racks": _num_from_snapshot(snapshot, ["number_of_racks", "rack_count", "total_racks"]),
                "ac_breaker_status": _first_present(snapshot, ["ac_breaker_status", "ac_contactor_status", "grid_contactor_status", "ac_relay_status"]),
                "dc_breaker_status": _first_present(snapshot, ["dc_breaker_status", "dc_contactor_status", "dc_relay_status", "dc_breaker_closed"]),
                "run_status": _first_present(snapshot, ["run_status", "work_status", "running_status", "pcs_running", "power_on_status"]),
            }
            if values.get("power") is None:
                voltage = values.get("voltage")
                current = values.get("current")
                if voltage is not None and current is not None:
                    try:
                        values["power"] = float(voltage) * float(current) / 1000.0
                    except Exception:
                        pass
            return {k: v for k, v in values.items() if v is not None}

        def classify(name: str, running: bool, has_snapshot: bool, row: dict[str, Any] | None, snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
            row = row or {}
            snapshot = snapshot or {}
            status = str(row.get("status") or ("Running" if running else "Stopped"))
            msg = str(row.get("last_message") or "-")
            error_count = int(row.get("errors") or 0)
            low = status.lower()
            is_error = bool(error_count) or low in {"error", "offline", "retrywait", "timeout"}
            # Prefer fresh successful data over stale error counters.  After a
            # temporary communication loss, the task row may still contain an
            # old error count/message even though polling has already recovered.
            if running and has_snapshot:
                connection = "online"
                is_error = False
            elif running and is_error:
                connection = "error"
            elif running:
                connection = "connecting"
            else:
                connection = "offline" if is_error or status.lower() == "offline" else "stopped"
            return {
                "name": name,
                "status": status,
                "connection": connection,
                "online": connection == "online",
                "running": bool(running),
                "error": bool(is_error),
                "errors": error_count,
                "last_message": msg,
                "last_update": row.get("last_update", "-"),
                "last_latency_ms": row.get("last_latency_ms", 0.0),
                "latest_values": _latest_values(snapshot),
                "snapshot_keys": sorted(list(snapshot.keys()))[:80] if isinstance(snapshot, dict) else [],
                "api_ts": now,
            }

        bms_states: dict[str, dict[str, Any]] = {}
        bms_running = getattr(w, "device_workers", {}) or {}
        latest_bms = getattr(w, "latest_snapshots", {}) or {}
        for dev in getattr(w, "devices", []) or []:
            name = str(dev.get("name", "")).strip()
            if not name:
                continue
            worker = bms_running.get(name)
            running = bool(getattr(worker, "running", False)) if worker is not None else False
            bms_states[name] = classify(name, running, name in latest_bms, task_rows.get(name), dict(latest_bms.get(name, {}) or {}))

        pcs_states: dict[str, dict[str, Any]] = {}
        pcs_running = getattr(w, "pcs_workers", {}) or {}
        latest_pcs = getattr(w, "latest_pcs_snapshots", {}) or {}
        pcs_names = []
        try:
            pcs_names = list(getattr(w, "pcs_configs", {}) or {})
        except Exception:
            pcs_names = []
        for name in pcs_names:
            name = str(name).strip()
            worker = pcs_running.get(name)
            running = bool(getattr(worker, "running", False)) if worker is not None else False
            pcs_states[name] = classify(name, running, name in latest_pcs, task_rows.get(name), dict(latest_pcs.get(name, {}) or {}))

        # Merge fleet command-worker state for PCS that may not have polling workers.
        try:
            fm = getattr(w, "fleet_manager", None)
            fleet = fm.snapshots() if fm is not None else {}
            for name, fs in (fleet or {}).items():
                name = str(name)
                if not name:
                    continue
                target = pcs_states.setdefault(name, classify(name, False, name in latest_pcs, task_rows.get(name), dict(latest_pcs.get(name, {}) or {})))
                if isinstance(fs, dict):
                    if fs.get("online"):
                        target.update({"connection": "online", "online": True, "running": True, "status": "FleetOnline", "last_message": fs.get("last_error") or "Fleet online"})
                    elif fs.get("last_error"):
                        target.update({"connection": "error", "online": False, "error": True, "status": "FleetError", "last_message": fs.get("last_error")})
        except Exception:
            pass
        return {"bms": bms_states, "pcs": pcs_states}

    def _queue_bms_command(self, names: list[str], method_name: str, *cmd_args: Any, label: str = "", command_id: str | None = None) -> dict[str, Any]:
        w = self.window
        queued = 0
        skipped: list[str] = []
        for dev_name in names:
            worker = None
            try:
                worker = getattr(w, "device_workers", {}).get(dev_name)
                if worker is None or not getattr(worker, "running", False) or not hasattr(worker, "enqueue_command"):
                    skipped.append(dev_name)
                    continue
                self._update_device_ack(command_id, dev_name, status="queued_to_worker", ok=None, message=label or method_name)
                def _ok_cb(device: str, result: Any, _cid: str | None = command_id) -> None:
                    self._update_device_ack(_cid, device, status="write_success", ok=True, message="device command executed", result=result)
                def _err_cb(device: str, message: str, _cid: str | None = command_id) -> None:
                    self._update_device_ack(_cid, device, status="write_failed", ok=False, message=message)
                ok = worker.enqueue_command(method_name, *cmd_args, label=label or method_name, callback=_ok_cb, error_callback=_err_cb)
                if ok:
                    queued += 1
                else:
                    self._update_device_ack(command_id, dev_name, status="skipped", ok=False, message="worker rejected command")
                    skipped.append(dev_name)
            except Exception:
                skipped.append(dev_name)
        status = "queued_to_device_worker" if queued > 0 else "no_device_queued"
        self._update_command_ack(command_id, status=status, ok=(queued > 0 or not names), message=f"queued {queued}/{len(names)} BMS command(s)")
        return {"ok": queued > 0 or not names, "status": status, "queued": queued, "total": len(names), "skipped": skipped, "pending_device_acks": queued > 0}

    def _pcs_factory(self, pcs_name: str):
        w = self.window
        if hasattr(w, "pcs_controller") and hasattr(w.pcs_controller, "create_client_for_pcs_name"):
            return w.pcs_controller.create_client_for_pcs_name(pcs_name)
        if hasattr(w, "create_pcs_client_for_pcs_name"):
            return w.create_pcs_client_for_pcs_name(pcs_name)
        raise RuntimeError("PCS client factory unavailable")

    def _pcs_names(self) -> list[str]:
        w = self.window
        if hasattr(w, "_fleet_pcs_names"):
            try:
                return list(w._fleet_pcs_names())
            except Exception:
                pass
        return [str(k) for k in getattr(w, "pcs_configs", {}).keys()]

    def _queue_pcs_command(self, names: list[str], method_name: str, *cmd_args: Any, label: str = "", command_id: str | None = None) -> dict[str, Any]:
        w = self.window
        names = [str(n).strip() for n in names if str(n).strip()]
        if not names:
            return {"ok": False, "queued": 0, "total": 0, "error": "No PCS selected"}
        fm = getattr(w, "fleet_manager", None)
        if fm is None:
            return {"ok": False, "queued": 0, "total": len(names), "error": "fleet_manager unavailable"}
        fm.start_pcs_command_workers(names, lambda n: self._pcs_factory(n), interval_s=float(getattr(w, "heartbeat_interval", 1.0)))
        for n in names:
            self._update_device_ack(command_id, n, status="queued_to_worker", ok=None, message=label or method_name)
        def _ok_cb(device: str, result: Any, _cid: str | None = command_id) -> None:
            self._update_device_ack(_cid, device, status="write_success", ok=True, message="PCS command executed", result=result)
        def _err_cb(device: str, message: str, _cid: str | None = command_id) -> None:
            self._update_device_ack(_cid, device, status="write_failed", ok=False, message=message)
        count = fm.enqueue_pcs_command(names, method_name, *cmd_args, label=label or method_name, callback=_ok_cb, error_callback=_err_cb)
        status = "queued_to_device_worker" if count > 0 else "no_device_queued"
        self._update_command_ack(command_id, status=status, ok=count > 0, message=f"queued {count}/{len(names)} PCS command(s)")
        return {"ok": count > 0, "status": status, "queued": int(count), "total": len(names), "pending_device_acks": count > 0}

    def _site_config_path(self):
        w = self.window
        if hasattr(w, "get_profile_path"):
            return w.get_profile_path("site_config.json")
        from pathlib import Path
        return Path("site_config.json")


    def _runtime_config_path(self) -> Path:
        """Return active runtime_config.json path for Web Settings.

        This is intentionally separate from site/project config. Most values are
        operator-visible but require runtime restart to take full effect.
        """
        w = self.window
        try:
            if hasattr(w, "get_profile_path"):
                return w.get_profile_path("runtime_config.json")
        except Exception:
            pass
        return Path("runtime_config.json")

    def _runtime_settings_metadata(self) -> dict[str, dict[str, Any]]:
        return {
            "heartbeat_interval": {"type": "number", "editable": True, "restart_required": False, "description": "EMS/BMS heartbeat interval in seconds."},
            "hv_step_timeout": {"type": "number", "editable": True, "restart_required": False, "description": "HV workflow step timeout in seconds."},
            "hv_poll_interval": {"type": "number", "editable": True, "restart_required": False, "description": "HV readiness polling interval in seconds."},
            "pcs_zero_power_threshold": {"type": "number", "editable": True, "restart_required": False, "description": "PCS zero-power threshold used by close/open workflows."},
            "charge_cutoff_max_cell_voltage": {"type": "number", "editable": True, "restart_required": False, "description": "Charge cutoff max-cell voltage threshold in mV."},
            "discharge_cutoff_min_cell_voltage": {"type": "number", "editable": True, "restart_required": False, "description": "Discharge cutoff min-cell voltage threshold in mV."},
            "cutoff_mode": {"type": "string", "editable": True, "restart_required": False, "description": "Cutoff protection mode, for example Alarm Only or Active Control."},
            "cutoff_trigger_confirm_count": {"type": "integer", "editable": True, "restart_required": False, "description": "Consecutive confirmations before cutoff trigger."},
            "cutoff_recover_confirm_count": {"type": "integer", "editable": True, "restart_required": False, "description": "Consecutive confirmations before cutoff recovery."},
            "alarm_history_window_before_minutes": {"type": "number", "editable": True, "restart_required": False, "description": "Alarm evidence window before event."},
            "alarm_history_window_after_minutes": {"type": "number", "editable": True, "restart_required": False, "description": "Alarm evidence window after event."},
            "power_tracking_enabled": {"type": "boolean", "editable": True, "restart_required": False, "description": "Enable PCS power tracking checks."},
            "power_tracking_tolerance_kw": {"type": "number", "editable": True, "restart_required": False, "description": "Allowed PCS power tracking error."},
            "power_tracking_confirm_count": {"type": "integer", "editable": True, "restart_required": False, "description": "Confirm count before power tracking issue is latched."},
            "pcs_fault_protection_mode": {"type": "string", "editable": True, "restart_required": False, "description": "PCS fault protection behavior."},
            "pcs_fault_confirm_count": {"type": "integer", "editable": True, "restart_required": False, "description": "PCS fault confirmation count."},
            "pcs_control_ui_enabled": {"type": "boolean", "editable": True, "restart_required": True, "description": "Enable PCS control page/features in classic UI."},
            "web_port": {"type": "integer", "editable": False, "restart_required": True, "description": "Runtime API/Web port. Usually selected by app_runtime/app_web."},
            "snapshot_interval_s": {"type": "number", "editable": False, "restart_required": False, "description": "Current runtime snapshot/update cadence if configured."},
            "max_curve_points": {"type": "integer", "editable": True, "restart_required": False, "description": "Runtime-side live curve cache length if supported."},
        }

    def _read_runtime_settings(self) -> dict[str, Any]:
        path = self._runtime_config_path()
        data: dict[str, Any] = {}
        try:
            if path.exists():
                with open(path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    data.update(loaded)
        except Exception as exc:
            return {"ok": False, "api_schema": API_SCHEMA_VERSION, "path": str(path), "error": str(exc), "settings": {}, "rows": []}
        w = self.window
        meta = self._runtime_settings_metadata()
        rows: list[dict[str, Any]] = []
        # Add configured keys first, then known metadata keys not yet present.
        keys = list(data.keys()) + [k for k in meta.keys() if k not in data]
        for key in keys:
            m = dict(meta.get(key, {"type": "unknown", "editable": False, "restart_required": True, "description": "Unclassified runtime setting."}))
            current = data.get(key, getattr(w, key, None))
            rows.append({
                "key": key,
                "value": current,
                "configured_value": data.get(key, None),
                "runtime_value": getattr(w, key, None),
                "source": "runtime_config.json" if key in data else "runtime/default",
                **m,
            })
        return {"ok": True, "api_schema": API_SCHEMA_VERSION, "path": str(path), "settings": data, "rows": rows, "note": "Editable values are saved to runtime_config.json. Some values only fully apply after Runtime restart."}

    def _write_runtime_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        current = self._read_runtime_settings()
        if not current.get("ok"):
            return current
        meta = self._runtime_settings_metadata()
        data = dict(current.get("settings") or {})
        updates = payload.get("settings", payload)
        if not isinstance(updates, dict):
            return {"ok": False, "error": "runtime settings payload must be a JSON object"}
        changed: dict[str, Any] = {}
        ignored: dict[str, str] = {}
        for key, value in updates.items():
            k = str(key)
            m = meta.get(k, {"editable": False, "type": "unknown"})
            if not m.get("editable"):
                ignored[k] = "not editable from Web"
                continue
            typ = str(m.get("type") or "string")
            try:
                if typ == "number":
                    value = float(value)
                elif typ == "integer":
                    value = int(value)
                elif typ == "boolean":
                    value = bool(value) if isinstance(value, bool) else str(value).strip().lower() in {"1", "true", "yes", "on"}
                else:
                    value = str(value)
            except Exception as exc:
                ignored[k] = f"invalid value: {exc}"
                continue
            data[k] = value
            changed[k] = value
            # Apply selected live attributes where the classic runtime already reads them dynamically.
            try:
                setattr(self.window, k, value)
            except Exception:
                pass
        path = self._runtime_config_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as exc:
            return {"ok": False, "path": str(path), "error": str(exc)}
        return {"ok": True, "api_schema": API_SCHEMA_VERSION, "path": str(path), "changed": changed, "ignored": ignored, "restart_required": [k for k in changed if meta.get(k, {}).get("restart_required")], "message": "Runtime settings saved. Restart Runtime if restart_required is not empty."}

    @staticmethod
    def _normalize_power_map_runtime(raw: Any, bms_names: list[str], pcs_names: list[str]) -> dict[str, dict[str, float]]:
        """Normalize supported power_map shapes to runtime-native PCS -> BMS weight."""
        bset = {str(x) for x in bms_names if str(x)}
        pset = {str(x) for x in pcs_names if str(x)}
        if not isinstance(raw, dict):
            return {}
        out: dict[str, dict[str, float]] = {}
        # Native format: {PCS: {BMS: weight}}
        for pc, sub in raw.items():
            pc_s = str(pc)
            if pc_s in pset and isinstance(sub, dict):
                row: dict[str, float] = {}
                for b, w in sub.items():
                    b_s = str(b)
                    if b_s in bset:
                        try:
                            row[b_s] = float(w)
                        except Exception:
                            pass
                if row:
                    out[pc_s] = row
        if out:
            return out
        # Legacy display format: {BMS: weight}; apply same BMS weights to every PCS.
        legacy: dict[str, float] = {}
        for b, w in raw.items():
            b_s = str(b)
            if b_s in bset:
                try:
                    legacy[b_s] = float(w)
                except Exception:
                    pass
        if legacy:
            for pc in pset:
                out[pc] = dict(legacy)
        return out

    def _power_map_status(self) -> dict[str, Any]:
        site = self._read_site_config_payload()
        cfg = site.get("config") if isinstance(site, dict) else {}
        clusters = (cfg or {}).get("clusters", []) or []
        rows: list[dict[str, Any]] = []
        issues: list[dict[str, Any]] = []
        for c in clusters:
            if not isinstance(c, dict):
                continue
            name = str(c.get("name") or c.get("id") or "")
            bms = [str(x) for x in (c.get("bms_devices") or c.get("bms") or []) if str(x)]
            pcs = [str(x) for x in (c.get("pcs_devices") or c.get("pcs") or ([c.get("pcs_device")] if c.get("pcs_device") else [])) if str(x)]
            pmap = self._normalize_power_map_runtime(c.get("power_map") or {}, bms, pcs)
            if not bms:
                issues.append({"cluster": name, "severity": "warning", "message": "No BMS bound; power map cannot be used."})
            if not pcs:
                issues.append({"cluster": name, "severity": "warning", "message": "No PCS bound; power map cannot be used."})
            missing_pcs = [pc for pc in pcs if pc not in pmap]
            missing_bms: dict[str, list[str]] = {}
            row_sums: dict[str, float] = {}
            for pc in pcs:
                weights = pmap.get(pc, {}) or {}
                missing = [b for b in bms if b not in weights]
                if missing:
                    missing_bms[pc] = missing
                row_sums[pc] = round(sum(float(v or 0) for v in weights.values()), 6)
                if weights and abs(row_sums[pc] - 1.0) > 0.001:
                    issues.append({"cluster": name, "pcs": pc, "severity": "warning", "message": f"Power map weights sum to {row_sums[pc]}, expected 1.0 for normalized dispatch."})
            if missing_pcs:
                issues.append({"cluster": name, "severity": "warning", "message": f"Power map missing PCS rows: {', '.join(missing_pcs)}"})
            if missing_bms:
                issues.append({"cluster": name, "severity": "warning", "message": "Power map missing BMS weights for some PCS", "missing_bms": missing_bms})
            rows.append({"cluster": name, "bms_devices": bms, "pcs_devices": pcs, "power_map": pmap, "pcs_weight_sums": row_sums, "ready_for_dispatch": bool(bms and pcs and pmap and not missing_pcs and not missing_bms)})
        return {"ok": True, "api_schema": API_SCHEMA_VERSION, "path": site.get("path"), "clusters": rows, "issues": issues, "runtime_use": "Cluster strategy runtime reads cluster.power_map when strategy starts. Full site-level Fleet EMS dispatch is planned for v10."}

    def _read_site_config_payload(self) -> dict[str, Any]:
        path = self._site_config_path()
        try:
            if path.exists():
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            else:
                data = {"site": getattr(getattr(self.window, "site", None), "name", "ESS Site"), "clusters": []}
            return {"ok": True, "path": str(path), "config": data}
        except Exception as exc:
            return {"ok": False, "path": str(path), "error": str(exc), "config": {}}

    def _write_site_config_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        w = self.window
        data = payload.get("config", payload)
        if not isinstance(data, dict):
            return {"ok": False, "error": "site_config payload must be a JSON object"}
        if "clusters" not in data or not isinstance(data.get("clusters"), list):
            return {"ok": False, "error": "Invalid site_config: missing clusters list"}
        path = self._site_config_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            if hasattr(w, "load_site_config"):
                w.load_site_config()
            return {"ok": True, "path": str(path), "cluster_count": len(data.get("clusters") or [])}
        except Exception as exc:
            return {"ok": False, "path": str(path), "error": str(exc)}

    def _remove_device_from_site_config(self, *, kind: str, name: str) -> dict[str, Any]:
        """Remove a BMS/PCS reference from cluster bindings and power_map safely."""
        name = str(name or "").strip()
        if not name:
            return {"ok": False, "error": "Missing device name"}
        current = self._read_site_config_payload()
        if not current.get("ok"):
            return current
        data = dict(current.get("config") or {})
        clusters = list(data.get("clusters") or [])
        changed = False
        for c in clusters:
            if not isinstance(c, dict):
                continue
            if kind == "bms":
                for key in ("bms_devices", "bms"):
                    arr = [str(x) for x in (c.get(key) or [])]
                    new_arr = [x for x in arr if x != name]
                    if new_arr != arr:
                        c[key] = new_arr
                        changed = True
                pmap = c.get("power_map") if isinstance(c.get("power_map"), dict) else {}
                new_pmap = {}
                for pc, weights in pmap.items():
                    if isinstance(weights, dict):
                        row = {str(bm): val for bm, val in weights.items() if str(bm) != name}
                        if row:
                            new_pmap[str(pc)] = row
                        if len(row) != len(weights):
                            changed = True
                    else:
                        new_pmap[str(pc)] = weights
                c["power_map"] = new_pmap
            elif kind == "pcs":
                for key in ("pcs_devices", "pcs"):
                    arr = [str(x) for x in (c.get(key) or [])]
                    new_arr = [x for x in arr if x != name]
                    if new_arr != arr:
                        c[key] = new_arr
                        changed = True
                if str(c.get("pcs_device") or "") == name:
                    pcs_list = [str(x) for x in (c.get("pcs_devices") or c.get("pcs") or [])]
                    c["pcs_device"] = pcs_list[0] if pcs_list else ""
                    changed = True
                pmap = c.get("power_map") if isinstance(c.get("power_map"), dict) else {}
                if name in {str(x) for x in pmap.keys()}:
                    pmap = {str(pc): weights for pc, weights in pmap.items() if str(pc) != name}
                    c["power_map"] = pmap
                    changed = True
        if not changed:
            return {"ok": True, "changed": False, "message": "No site_config references to clean."}
        data["clusters"] = clusters
        res = self._write_site_config_payload(data)
        res["changed"] = True
        res["cleaned_device"] = name
        res["kind"] = kind
        return res

    def _delete_cluster_from_site_config(self, cluster_name: str) -> dict[str, Any]:
        cluster_name = str(cluster_name or "").strip()
        if not cluster_name:
            return {"ok": False, "error": "Missing cluster name"}
        current = self._read_site_config_payload()
        if not current.get("ok"):
            return current
        data = dict(current.get("config") or {})
        clusters = list(data.get("clusters") or [])
        if len(clusters) <= 1:
            return {"ok": False, "error": "At least one cluster must remain"}
        kept = [c for c in clusters if str(c.get("name", "")) != cluster_name]
        if len(kept) == len(clusters):
            return {"ok": False, "error": f"Cluster not found: {cluster_name}"}
        data["clusters"] = kept
        result = self._write_site_config_payload(data)
        result.update({"deleted": cluster_name})
        return result


    def _project_profile_options(self) -> dict[str, Any]:
        """List available BMS/PCS profiles for Web Project dropdowns.

        This is read-only and safe to call from Web. It checks both packaged
        resource folders and the active profile folder so PyInstaller builds can
        still expose profiles after relocation.
        """
        roots: list[Path] = []
        try:
            roots.append(Path.cwd())
        except Exception:
            pass
        try:
            roots.append(Path(__file__).resolve().parents[1])
        except Exception:
            pass
        try:
            if hasattr(self.window, "get_profile_path"):
                roots.append(Path(self.window.get_profile_path(".")))
        except Exception:
            pass

        def _dedup(seq: list[str]) -> list[str]:
            out: list[str] = []
            seen: set[str] = set()
            for item in seq:
                key = str(item or "").strip()
                if key and key not in seen:
                    seen.add(key)
                    out.append(key)
            return sorted(out)

        bms: list[str] = []
        pcs: list[str] = []
        for root in roots:
            try:
                # BMS profiles are usually directories containing profile.json.
                for base in [root / "bms_profiles", root / "profiles" / "default" / "bms_profiles"]:
                    if base.exists():
                        for child in base.iterdir():
                            if child.is_dir() and ((child / "profile.json").exists() or (child / "bms_register_map.json").exists()):
                                bms.append(child.name)
                # PCS profiles are usually JSON files.
                for base in [root / "pcs_profiles", root / "profiles" / "default" / "pcs_profiles"]:
                    if base.exists():
                        for child in base.glob("*.json"):
                            if child.name.startswith("_"):
                                continue
                            pcs.append(child.stem)
            except Exception:
                pass
        if not bms:
            bms = ["catl_v22", "catl_v17"]
        if not pcs:
            pcs = ["kehua_bcs1250", "sineng_template", "sma_template", "nr_pcs9567an_gfl"]
        return {"ok": True, "api_schema": API_SCHEMA_VERSION, "bms_profiles": _dedup(bms), "pcs_profiles": _dedup(pcs)}

    def _project_config_summary(self) -> dict[str, Any]:
        """Return runtime-owned project/device configuration without starting I/O."""
        w = self.window
        devices = [dict(d) for d in (getattr(w, "devices", []) or []) if isinstance(d, dict)]
        pcs_configs = {str(k): dict(v) for k, v in (getattr(w, "pcs_configs", {}) or {}).items() if isinstance(v, dict)}
        site_payload = self._read_site_config_payload()
        try:
            devices_path = str(w.get_profile_path("devices.json"))
        except Exception:
            devices_path = "devices.json"
        try:
            pcs_path = str(w.get_profile_path("pcs_configs.json"))
        except Exception:
            pcs_path = "pcs_configs.json"
        return {
            "ok": True,
            "api_schema": API_SCHEMA_VERSION,
            "paths": {"devices": devices_path, "pcs_configs": pcs_path, "site_config": site_payload.get("path")},
            "bms_devices": devices,
            "pcs_configs": pcs_configs,
            "site_config": site_payload.get("config", {}),
            "counts": {"bms": len(devices), "pcs": len(pcs_configs), "clusters": len((site_payload.get("config", {}) or {}).get("clusters", []) or [])},
            "safety_note": "Configuration changes do not auto-connect BMS/PCS and do not start strategy.",
        }

    def _save_bms_devices_runtime(self) -> None:
        w = self.window
        if hasattr(w, "save_devices_to_default"):
            w.save_devices_to_default()
            return
        path = w.get_profile_path("devices.json") if hasattr(w, "get_profile_path") else Path("devices.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(getattr(w, "devices", []) or [], f, ensure_ascii=False, indent=2)

    def _save_pcs_configs_runtime(self) -> None:
        w = self.window
        if hasattr(w, "save_pcs_config"):
            w.save_pcs_config()
            return
        path = w.get_profile_path("pcs_configs.json") if hasattr(w, "get_profile_path") else Path("pcs_configs.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(getattr(w, "pcs_configs", {}) or {}, f, ensure_ascii=False, indent=2)

    def _refresh_project_views_runtime(self) -> None:
        w = self.window
        for method in ("refresh_device_table", "refresh_pcs_view", "refresh_site_view", "refresh_overview", "refresh_global_status_bar"):
            try:
                fn = getattr(w, method, None)
                if callable(fn):
                    fn()
            except Exception:
                pass

    def _upsert_bms_config_runtime(self, payload: dict[str, Any]) -> dict[str, Any]:
        w = self.window
        name = str(payload.get("name") or "").strip()
        host = str(payload.get("host") or "").strip()
        if not name or not host:
            return {"ok": False, "error": "BMS name and host are required"}
        cfg = {
            "name": name,
            "host": host,
            "port": int(payload.get("port") or 502),
            "unit_id": int(payload.get("unit_id") or 1),
            "interval": float(payload.get("interval") or payload.get("poll_interval") or 2.0),
            "output_dir": str(payload.get("output_dir") or ""),
            "fake_scenario": str(payload.get("fake_scenario") or "normal"),
            "profile": str(payload.get("profile") or "catl_v22"),
        }
        if not cfg["output_dir"]:
            try:
                cfg["output_dir"] = str(w.get_profile_path("output"))
            except Exception:
                cfg["output_dir"] = "output"
        devices = [dict(d) for d in (getattr(w, "devices", []) or []) if isinstance(d, dict) and str(d.get("name", "")) != name]
        existed = len(devices) != len(getattr(w, "devices", []) or [])
        devices.append(cfg)
        devices.sort(key=lambda x: str(x.get("name", "")))
        w.devices = devices
        self._save_bms_devices_runtime()
        self._refresh_project_views_runtime()
        return {"ok": True, "action": "updated" if existed else "added", "device": cfg, "message": "BMS config saved; polling was not auto-started."}

    def _remove_bms_config_runtime(self, name: str) -> dict[str, Any]:
        w = self.window
        name = str(name or "").strip()
        if not name:
            return {"ok": False, "error": "Missing BMS name"}
        try:
            if hasattr(w, "stop_device_by_name"):
                w.stop_device_by_name(name)
        except Exception:
            pass
        old = list(getattr(w, "devices", []) or [])
        w.devices = [dict(d) for d in old if isinstance(d, dict) and str(d.get("name", "")) != name]
        if len(w.devices) == len(old):
            return {"ok": False, "error": f"BMS not found: {name}"}
        self._save_bms_devices_runtime()
        site_cleanup = self._remove_device_from_site_config(kind="bms", name=name)
        self._refresh_project_views_runtime()
        return {"ok": True, "removed": name, "site_cleanup": site_cleanup, "message": "BMS removed, polling stopped if it was running, and cluster/power-map references were cleaned."}

    def _upsert_pcs_config_runtime(self, payload: dict[str, Any]) -> dict[str, Any]:
        w = self.window
        name = str(payload.get("name") or payload.get("pcs") or "").strip()
        host = str(payload.get("host") or "").strip()
        if not name or not host:
            return {"ok": False, "error": "PCS name and host are required"}
        pcs_configs = dict(getattr(w, "pcs_configs", {}) or {})
        previous = dict(pcs_configs.get(name, {}) or {})
        cfg = dict(previous)
        cfg.update({
            "name": name,
            "enabled": bool(payload.get("enabled", previous.get("enabled", True))),
            "host": host,
            "port": int(payload.get("port") or previous.get("port") or 502),
            "unit_id": int(payload.get("unit_id") or previous.get("unit_id") or 1),
            "timeout": float(payload.get("timeout") or previous.get("timeout") or 3.0),
            "driver": str(payload.get("driver") or previous.get("driver") or "generic_modbus_pcs"),
            "profile": str(payload.get("profile") or previous.get("profile") or "kehua_bcs1250"),
            "fake_scenario": str(payload.get("fake_scenario") or previous.get("fake_scenario") or "normal"),
            "output_dir": str(payload.get("output_dir") or previous.get("output_dir") or ""),
        })
        if not cfg["output_dir"]:
            try:
                cfg["output_dir"] = str(w.get_profile_path("output") / "pcs")
            except Exception:
                cfg["output_dir"] = "output/pcs"
        existed = name in pcs_configs
        pcs_configs[name] = cfg
        w.pcs_configs = pcs_configs
        try:
            w.current_pcs_name = name
            w.pcs_config = cfg
        except Exception:
            pass
        self._save_pcs_configs_runtime()
        self._refresh_project_views_runtime()
        return {"ok": True, "action": "updated" if existed else "added", "pcs": cfg, "message": "PCS config saved; connection was not auto-started."}

    def _remove_pcs_config_runtime(self, name: str) -> dict[str, Any]:
        w = self.window
        name = str(name or "").strip()
        if not name:
            return {"ok": False, "error": "Missing PCS name"}
        try:
            if hasattr(w, "stop_pcs_polling_by_name"):
                w.stop_pcs_polling_by_name(name)
        except Exception:
            pass
        pcs_configs = dict(getattr(w, "pcs_configs", {}) or {})
        if name not in pcs_configs:
            return {"ok": False, "error": f"PCS not found: {name}"}
        pcs_configs.pop(name, None)
        w.pcs_configs = pcs_configs
        self._save_pcs_configs_runtime()
        site_cleanup = self._remove_device_from_site_config(kind="pcs", name=name)
        self._refresh_project_views_runtime()
        return {"ok": True, "removed": name, "site_cleanup": site_cleanup, "message": "PCS removed, polling stopped if it was running, and cluster/power-map references were cleaned."}




    def _validate_project_config_runtime(self) -> dict[str, Any]:
        """Validate project/device/site config without connecting to equipment."""
        summary = self._project_config_summary()
        bms_devices = summary.get("bms_devices", []) or []
        pcs_configs = summary.get("pcs_configs", {}) or {}
        site_config = summary.get("site_config", {}) or {}
        issues: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []

        def add(level: str, code: str, message: str, **extra: Any) -> None:
            row = {"level": level, "code": code, "message": message}
            row.update(extra)
            (issues if level == "error" else warnings).append(row)

        bms_names: list[str] = []
        for idx, dev in enumerate(bms_devices):
            name = str((dev or {}).get("name") or "").strip()
            host = str((dev or {}).get("host") or "").strip()
            bms_names.append(name)
            if not name:
                add("error", "bms_missing_name", f"BMS row {idx + 1} has no name", index=idx)
            if not host:
                add("error", "bms_missing_host", f"BMS {name or idx + 1} has no host/IP", device=name)
            try:
                port = int((dev or {}).get("port") or 0)
                if port <= 0 or port > 65535:
                    add("error", "bms_invalid_port", f"BMS {name} has invalid port {port}", device=name, port=port)
            except Exception:
                add("error", "bms_invalid_port", f"BMS {name} port is not an integer", device=name)
            try:
                interval = float((dev or {}).get("interval") or (dev or {}).get("poll_interval") or 0)
                if interval < 0.2:
                    add("warning", "bms_poll_fast", f"BMS {name} polling interval is very fast: {interval}s", device=name, interval_s=interval)
            except Exception:
                add("warning", "bms_interval_parse", f"BMS {name} interval cannot be parsed", device=name)
        for name in sorted({x for x in bms_names if x and bms_names.count(x) > 1}):
            add("error", "bms_duplicate_name", f"Duplicate BMS name: {name}", device=name)

        pcs_names = list(pcs_configs.keys())
        for name, cfg in pcs_configs.items():
            host = str((cfg or {}).get("host") or "").strip()
            if not host:
                add("error", "pcs_missing_host", f"PCS {name} has no host/IP", pcs=name)
            try:
                port = int((cfg or {}).get("port") or 0)
                if port <= 0 or port > 65535:
                    add("error", "pcs_invalid_port", f"PCS {name} has invalid port {port}", pcs=name, port=port)
            except Exception:
                add("error", "pcs_invalid_port", f"PCS {name} port is not an integer", pcs=name)
            if str((cfg or {}).get("profile") or "").strip() == "":
                add("warning", "pcs_missing_profile", f"PCS {name} has no profile set", pcs=name)
        for name in sorted({x for x in pcs_names if x and pcs_names.count(x) > 1}):
            add("error", "pcs_duplicate_name", f"Duplicate PCS name: {name}", pcs=name)

        bms_set = {x for x in bms_names if x}
        pcs_set = set(pcs_names)
        clusters = (site_config or {}).get("clusters", []) or []
        if not clusters:
            add("warning", "site_no_clusters", "site_config has no clusters")
        for c in clusters:
            cname = str((c or {}).get("name") or "").strip()
            for b in (c or {}).get("bms_devices", []) or []:
                if str(b) not in bms_set:
                    add("error", "cluster_missing_bms", f"Cluster {cname} references missing BMS {b}", cluster=cname, device=str(b))
            for pc in (c or {}).get("pcs_devices", []) or []:
                if str(pc) not in pcs_set:
                    add("error", "cluster_missing_pcs", f"Cluster {cname} references missing PCS {pc}", cluster=cname, pcs=str(pc))
            try:
                pmap = (c or {}).get("power_map", {}) or {}
                for pc in pmap.keys():
                    if str(pc) not in pcs_set:
                        add("warning", "power_map_unknown_pcs", f"Cluster {cname} power_map contains unknown PCS {pc}", cluster=cname, pcs=str(pc))
            except Exception:
                add("warning", "power_map_invalid", f"Cluster {cname} power_map is not readable", cluster=cname)

        return {
            "ok": len(issues) == 0,
            "api_schema": API_SCHEMA_VERSION,
            "checked_at": time.time(),
            "counts": summary.get("counts", {}),
            "issues": issues,
            "warnings": warnings,
            "summary": f"{len(issues)} error(s), {len(warnings)} warning(s)",
            "safety_note": "Validation only. It does not connect to BMS/PCS and does not write Modbus registers.",
        }

    def _recording_status(self) -> dict[str, Any]:
        w = self.window
        def _dropped(mapping: Any) -> dict[str, int]:
            result: dict[str, int] = {}
            for name, rec in (mapping or {}).items():
                try:
                    result[str(name)] = int(getattr(rec, "dropped_rows", 0) or 0)
                except Exception:
                    result[str(name)] = 0
            return result
        def _rec_paths(mapping: Any) -> dict[str, str]:
            out: dict[str, str] = {}
            for name, rec in (mapping or {}).items():
                try:
                    inner = getattr(rec, "recorder", rec)
                    path = getattr(inner, "output_dir", None) or getattr(inner, "path", None) or getattr(inner, "filepath", None)
                    if path:
                        out[str(name)] = str(path)
                except Exception:
                    pass
            return out
        bms_dirs = _rec_paths(getattr(w, "recorders", {}) or {})
        pcs_dirs = _rec_paths(getattr(w, "pcs_recorders", {}) or {})
        try:
            default_bms_dir = str(w.get_profile_path("records"))
        except Exception:
            default_bms_dir = str(Path("records"))
        try:
            default_pcs_dir = str(w.get_profile_path("pcs_records"))
        except Exception:
            default_pcs_dir = str(Path("pcs_records"))
        return {
            "owner": "runtime",
            "bms_csv": sorted(list(getattr(w, "bms_csv_recording_devices", set()) or [])),
            "pcs_csv": sorted(list(getattr(w, "pcs_csv_recording_devices", set()) or [])),
            "bms_output_dirs": bms_dirs,
            "pcs_output_dirs": pcs_dirs,
            "default_bms_output_dir": default_bms_dir,
            "default_pcs_output_dir": default_pcs_dir,
            "bms_dropped_rows": _dropped(getattr(w, "recorders", {}) or {}),
            "alarm_dropped_rows": _dropped(getattr(w, "alarm_recorders", {}) or {}),
            "pcs_dropped_rows": _dropped(getattr(w, "pcs_recorders", {}) or {}),
        }

    def _log_status(self) -> dict[str, Any]:
        w = self.window
        try:
            log_dir = w.get_profile_path("logs")
        except Exception:
            log_dir = Path("logs")
        return {
            "owner": "runtime",
            "log_dir": str(log_dir),
            "operation_log_today": str(Path(log_dir) / f"operation_{datetime.now().strftime('%Y%m%d')}.log"),
        }

    def _start_bms_csv_runtime(self, names: list[str]) -> dict[str, Any]:
        w = self.window
        configured = {str(d.get("name", "")): d for d in getattr(w, "devices", []) or [] if d.get("name")}
        if not names:
            names = sorted(configured)
        started: list[str] = []
        for name in names:
            dev = configured.get(str(name))
            if not dev:
                continue
            try:
                if hasattr(w, "_ensure_bms_recorders"):
                    w._ensure_bms_recorders(dev)
                getattr(w, "bms_csv_recording_devices", set()).add(str(name))
                started.append(str(name))
            except Exception:
                pass
        try:
            if hasattr(w, "update_bms_csv_status_label"):
                w.update_bms_csv_status_label()
        except Exception:
            pass
        return {"ok": True, "started": started, "recording": self._recording_status()}

    def _stop_bms_csv_runtime(self, names: list[str]) -> dict[str, Any]:
        w = self.window
        if not names:
            names = sorted(list(getattr(w, "bms_csv_recording_devices", set()) or []))
        stopped: list[str] = []
        for name in names:
            try:
                if hasattr(w, "_stop_bms_csv_for_device"):
                    w._stop_bms_csv_for_device(str(name))
                else:
                    getattr(w, "bms_csv_recording_devices", set()).discard(str(name))
                stopped.append(str(name))
            except Exception:
                pass
        return {"ok": True, "stopped": stopped, "recording": self._recording_status()}

    def _start_pcs_csv_runtime(self, names: list[str]) -> dict[str, Any]:
        w = self.window
        configured = sorted(list(getattr(w, "pcs_configs", {}) or {}))
        if not names:
            names = configured
        started: list[str] = []
        for name in names:
            name = str(name)
            if name not in configured:
                continue
            try:
                if not hasattr(w, "pcs_recorders"):
                    w.pcs_recorders = {}
                if name not in w.pcs_recorders:
                    from .async_recorder import AsyncRecorderProxy
                    from .recorder import CsvRecorder
                    w.pcs_recorders[name] = AsyncRecorderProxy(
                        CsvRecorder(output_dir=w._pcs_default_output_dir(name), device_name=f"pcs_{name}")
                    )
                getattr(w, "pcs_csv_recording_devices", set()).add(name)
                started.append(name)
            except Exception:
                pass
        try:
            if hasattr(w, "update_pcs_csv_status_label"):
                w.update_pcs_csv_status_label()
        except Exception:
            pass
        return {"ok": True, "started": started, "recording": self._recording_status()}

    def _stop_pcs_csv_runtime(self, names: list[str]) -> dict[str, Any]:
        w = self.window
        if not names:
            names = sorted(list(getattr(w, "pcs_csv_recording_devices", set()) or []))
        stopped: list[str] = []
        for name in names:
            try:
                if hasattr(w, "_stop_pcs_csv_for_device"):
                    w._stop_pcs_csv_for_device(str(name))
                else:
                    getattr(w, "pcs_csv_recording_devices", set()).discard(str(name))
                stopped.append(str(name))
            except Exception:
                pass
        return {"ok": True, "stopped": stopped, "recording": self._recording_status()}

    def _read_bms_version_runtime(self, device: str, sbmu_count: int = 1) -> dict[str, Any]:
        dev_name = str(device or "").strip()
        if not dev_name:
            return {"ok": False, "error": "Missing BMS device"}
        try:
            cfg = next((d for d in getattr(self.window, "devices", []) or [] if str(d.get("name", "")) == dev_name), None)
            if not cfg:
                return {"ok": False, "device": dev_name, "error": f"BMS config not found: {dev_name}"}
            from .client_factory import create_bms_client
            client = create_bms_client(cfg, fake_mode=bool(getattr(self.window, "fake_mode", False)))
            try:
                if hasattr(client, "connect"):
                    client.connect()
                data = client.read_software_version(max(0, int(sbmu_count or 0)))
            finally:
                try:
                    if hasattr(client, "close"):
                        client.close()
                    elif hasattr(client, "disconnect"):
                        client.disconnect()
                except Exception:
                    pass
            return {"ok": True, "device": dev_name, "sbmu_count": int(sbmu_count or 0), "version": data}
        except Exception as exc:
            return {"ok": False, "device": dev_name, "error": str(exc)}

    def _bms_client_for_device(self, dev_name: str):
        cfg = next((d for d in getattr(self.window, "devices", []) or [] if str(d.get("name", "")) == dev_name), None)
        if not cfg:
            raise RuntimeError(f"BMS config not found: {dev_name}")
        from .client_factory import create_bms_client
        return create_bms_client(cfg, fake_mode=bool(getattr(self.window, "fake_mode", False)))

    def _read_bms_racks_runtime(self, device: str, count: int = 16) -> dict[str, Any]:
        dev_name = str(device or "").strip()
        if not dev_name:
            return {"ok": False, "error": "Missing BMS device"}
        count = max(1, min(int(count or 16), 48))
        client = None
        try:
            client = self._bms_client_for_device(dev_name)
            if hasattr(client, "connect"):
                client.connect()
            rows: list[dict[str, Any]] = []
            for idx in range(1, count + 1):
                raw = client.read_sbmu_summary(idx) if hasattr(client, "read_sbmu_summary") else None
                raw = raw or {}
                def pick(*keys):
                    for k in keys:
                        if k in raw and raw.get(k) is not None:
                            return raw.get(k)
                    return None
                base = idx * 0x400
                v_out = pick("battery_subsystem_external_voltage", "rack_voltage_outside", "rack_voltage", "battery_subsystem_voltage", "external_voltage")
                cur = pick("battery_subsystem_current", "rack_current", "current")
                power = pick("battery_subsystem_power", "rack_power", "power")
                if power is None and v_out is not None and cur is not None:
                    try:
                        power = float(v_out) * float(cur) / 1000.0
                    except Exception:
                        pass
                pos = pick("master_positive_relay_status", "positive_relay_status")
                neg = pick("master_negative_relay_status", "negative_relay_status")
                online = pick("high_voltage_online_status", "online", "hv_online_status")
                ready = bool(str(online) in {"1", "1.0", "true", "True"} and str(pos) in {"1", "1.0", "true", "True"} and str(neg) in {"1", "1.0", "true", "True"})
                rows.append({
                    "rack": idx,
                    "base_address": f"0x{base:04X}",
                    "online": online,
                    "power_on_ready": ready,
                    "precharge_relay": pick("precharge_relay_status"),
                    "positive_relay": pos,
                    "negative_relay": neg,
                    "voltage_outside_v": v_out,
                    "voltage_inside_v": pick("battery_subsystem_internal_voltage", "rack_voltage_inside", "internal_voltage"),
                    "current_a": cur,
                    "power_kw": power,
                    "soc_percent": pick("rack_soc", "soc_2", "soc"),
                    "soh_percent": pick("rack_soh", "soh_2", "soh"),
                    "max_cell_mv": pick("max_cell_voltage", "max_single_cell_voltage", "maximum_cell_voltage"),
                    "min_cell_mv": pick("min_cell_voltage", "min_single_cell_voltage", "minimum_cell_voltage"),
                    "avg_cell_mv": pick("average_cell_voltage", "avg_cell_voltage"),
                    "cell_voltage_sum_v": pick("cell_voltage_sum", "sum_of_cell_voltage", "sum_cell_voltage", "total_cell_voltage"),
                    "max_temp_c": pick("max_temperature", "max_single_cell_temperature", "maximum_temperature"),
                    "min_temp_c": pick("min_temperature", "min_single_cell_temperature", "minimum_temperature"),
                    "avg_temp_c": pick("average_temperature", "avg_temperature"),
                    "raw": raw,
                })
            masks = {}
            for addr in (0x038D, 0x038E, 0x038F):
                try:
                    if hasattr(client, "_read_single_register"):
                        masks[f"0x{addr:04X}"] = client._read_single_register(addr)
                except Exception:
                    masks[f"0x{addr:04X}"] = None
            return {"ok": True, "device": dev_name, "count": count, "racks": rows, "disable_masks": masks}
        except Exception as exc:
            return {"ok": False, "device": dev_name, "error": str(exc)}
        finally:
            try:
                if client is not None:
                    if hasattr(client, "close"):
                        client.close()
                    elif hasattr(client, "disconnect"):
                        client.disconnect()
            except Exception:
                pass

    def _apply_bms_rack_mask_runtime(self, device: str, changes: list[dict[str, Any]]) -> dict[str, Any]:
        dev_name = str(device or "").strip()
        if not dev_name:
            return {"ok": False, "error": "Missing BMS device"}
        if not isinstance(changes, list) or not changes:
            return {"ok": False, "device": dev_name, "error": "No rack enable/disable changes selected"}
        client = None
        try:
            client = self._bms_client_for_device(dev_name)
            if hasattr(client, "connect"):
                client.connect()
            target: dict[int, int] = {}
            current: dict[int, int] = {}
            for addr in (0x038D, 0x038E, 0x038F):
                val = 0
                if hasattr(client, "_read_single_register"):
                    got = client._read_single_register(addr)
                    val = int(got or 0)
                current[addr] = val & 0xFFFF
                target[addr] = val & 0xFFFF
            normalized=[]
            for ch in changes:
                rack = int(ch.get("rack") or 0)
                action = str(ch.get("action") or "").lower()
                if rack < 1 or rack > 48 or action not in {"enable", "disable"}:
                    continue
                addr = 0x038D + ((rack - 1) // 16)
                bit = (rack - 1) % 16
                if action == "disable":
                    target[addr] |= (1 << bit)
                else:
                    target[addr] &= ~(1 << bit)
                normalized.append({"rack": rack, "action": action, "address": f"0x{addr:04X}", "bit": bit})
            if not normalized:
                return {"ok": False, "device": dev_name, "error": "No valid rack changes"}
            written=[]
            readback={}
            for addr, val in target.items():
                if val == current.get(addr):
                    continue
                ok = client.write_single_register(addr, int(val)) if hasattr(client, "write_single_register") else False
                written.append({"address": f"0x{addr:04X}", "current": current.get(addr), "target": val, "ok": bool(ok)})
                try:
                    if hasattr(client, "_read_single_register"):
                        readback[f"0x{addr:04X}"] = client._read_single_register(addr)
                except Exception:
                    readback[f"0x{addr:04X}"] = None
            return {"ok": bool(written) and all(x.get("ok") for x in written), "device": dev_name, "changes": normalized, "current": {f"0x{k:04X}": v for k,v in current.items()}, "target": {f"0x{k:04X}": v for k,v in target.items()}, "written": written, "readback": readback, "safety_note": "0=Enable, 1=Disable. Command writes full bitmask after reading current mask."}
        except Exception as exc:
            return {"ok": False, "device": dev_name, "error": str(exc)}
        finally:
            try:
                if client is not None:
                    if hasattr(client, "close"):
                        client.close()
                    elif hasattr(client, "disconnect"):
                        client.disconnect()
            except Exception:
                pass

    def _read_recent_operation_log(self, max_lines: int = 300) -> dict[str, Any]:
        path = Path(self._log_status().get("operation_log_today", ""))
        if not path.exists():
            return {"ok": True, "path": str(path), "lines": []}
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-max(1, int(max_lines)):]
            return {"ok": True, "path": str(path), "lines": lines}
        except Exception as exc:
            return {"ok": False, "path": str(path), "error": str(exc), "lines": []}


    def device_snapshot(self, kind: str, name: str) -> dict[str, Any]:
        """Return one device snapshot owned by Runtime.

        This endpoint is used by the UI client for details/alarms/curves without
        touching local UI worker objects.
        """
        kind = str(kind or "bms").lower().strip()
        name = str(name or "").strip()
        source = getattr(self.window, "latest_pcs_snapshots", {}) if kind == "pcs" else getattr(self.window, "latest_snapshots", {})
        snap = dict((source or {}).get(name, {}) or {})
        return {"ok": True, "kind": kind, "device": name, "snapshot": snap, "has_snapshot": bool(snap)}

    def device_alarms(self, name: str) -> dict[str, Any]:
        """Return parsed BMS alarm rows from Runtime-owned snapshots."""
        name = str(name or "").strip()
        snap = dict((getattr(self.window, "latest_snapshots", {}) or {}).get(name, {}) or {})
        alarms: list[dict[str, Any]] = []
        parsed: dict[str, Any] = {}
        try:
            parser = self.window.get_alarm_parser_for_device(name) if hasattr(self.window, "get_alarm_parser_for_device") else getattr(self.window, "alarm_parser", None)
            if parser is not None:
                parsed = parser.parse_snapshot(snap) or {}
                for addr in range(0x0000, 0x0020):
                    key = f"alarm_0x{addr:04x}"
                    val = snap.get(key, "-")
                    active_bits: list[str] = []
                    if isinstance(val, int):
                        addr_key = f"0x{addr:04x}"
                        for bit in range(16):
                            if val & (1 << bit):
                                bit_key = f"bit{bit}"
                                name_text = getattr(parser, "alarm_map", {}).get(addr_key, {}).get(bit_key, "Unknown")
                                active_bits.append(f"Bit{bit}: {name_text}")
                    alarms.append({"address": f"0x{addr:04x}", "raw": val, "active": active_bits})
        except Exception as exc:
            return {"ok": False, "device": name, "error": str(exc), "alarms": alarms, "parsed": parsed}
        return {"ok": True, "device": name, "alarms": alarms, "parsed": parsed}

    def _resolve_user_path(self, value: str) -> Path:
        """Resolve analyzer input paths safely for field laptops.

        Absolute paths are allowed for local engineering use. Relative paths are
        resolved from cwd first, then current profile path when available.
        """
        raw = Path(str(value or "").strip()).expanduser()
        if raw.is_absolute():
            return raw
        candidates = [Path.cwd() / raw]
        try:
            if hasattr(self.window, "get_profile_path"):
                candidates.append(self.window.get_profile_path(str(raw)))
        except Exception:
            pass
        for c in candidates:
            if c.exists():
                return c
        return candidates[0]

    def _analyzer_upload_dir(self) -> Path:
        """Folder used by Web Analyzer uploads.

        Uploaded packet/DBC/mapping files are stored beside the active profile
        when possible so field diagnosis evidence travels with the project. It
        is intentionally offline-only and never opens BMS/PCS connections.
        """
        try:
            if hasattr(self.window, "get_profile_path"):
                base = self.window.get_profile_path("analyzer_uploads")
                base.mkdir(parents=True, exist_ok=True)
                return base
        except Exception:
            pass
        base = Path.cwd() / "analyzer_uploads"
        base.mkdir(parents=True, exist_ok=True)
        return base

    def _safe_upload_name(self, filename: str) -> str:
        import re
        raw = Path(str(filename or "upload.bin")).name.strip() or "upload.bin"
        raw = re.sub(r"[^A-Za-z0-9._ -]+", "_", raw).strip(" .") or "upload.bin"
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return f"{stamp}_{raw}"

    def save_analyzer_upload(self, filename: str, data: bytes, kind: str = "") -> dict[str, Any]:
        allowed = {".asc", ".dbc", ".pcap", ".pcapng", ".csv", ".json"}
        suffix = Path(filename or "").suffix.lower()
        if suffix not in allowed:
            return {"ok": False, "error": f"unsupported file type: {suffix or '(none)'}", "allowed": sorted(allowed)}
        if suffix == ".json" and kind not in {"mapping", "any", ""}:
            return {"ok": False, "error": "json uploads are only allowed for mapping.json"}
        if len(data or b"") <= 0:
            return {"ok": False, "error": "empty upload"}
        # Keep uploads bounded for field laptops. Large captures can still be
        # referenced by path instead of uploaded through the browser.
        max_bytes = 256 * 1024 * 1024
        if len(data) > max_bytes:
            return {"ok": False, "error": "file too large for browser upload", "max_bytes": max_bytes}
        folder = self._analyzer_upload_dir()
        out = folder / self._safe_upload_name(filename)
        out.write_bytes(data)
        return {"ok": True, "filename": Path(filename).name, "kind": kind or self._infer_analyzer_kind(out), "path": str(out), "size_bytes": len(data)}

    def _infer_analyzer_kind(self, path: Path) -> str:
        ext = path.suffix.lower()
        if ext == ".asc":
            return "asc"
        if ext == ".dbc":
            return "dbc"
        if ext in {".pcap", ".pcapng"}:
            return "modbus"
        if ext == ".json":
            return "mapping"
        if ext == ".csv":
            return "csv"
        return "file"

    def analyzer_files(self) -> dict[str, Any]:
        folder = self._analyzer_upload_dir()
        files = []
        for p in sorted(folder.glob("*"), key=lambda x: x.stat().st_mtime if x.exists() else 0, reverse=True):
            if not p.is_file():
                continue
            try:
                st = p.stat()
                files.append({
                    "name": p.name,
                    "path": str(p),
                    "kind": self._infer_analyzer_kind(p),
                    "size_bytes": st.st_size,
                    "modified_at": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
                })
            except Exception:
                continue
        return {"ok": True, "dir": str(folder), "files": files[:200]}

    def analyze_modbus_capture(self, path: str, timeout_seconds: float = 2.0, limit: int = 200) -> dict[str, Any]:
        """Analyze .pcap/.pcapng Modbus TCP captures from the Web client.

        This is intentionally offline-only: no device connection is opened and no
        register is written. It reuses the existing lightweight PacketAnalyzer.
        """
        try:
            from .packet_analyzer import PacketAnalyzer
            src = self._resolve_user_path(path)
            if not src.exists():
                return {"ok": False, "error": "capture file not found", "path": str(src)}
            records = PacketAnalyzer().analyze(src, timeout_seconds=float(timeout_seconds or 2.0))
            rows = [r.to_row() for r in records]
            by_fc: dict[str, int] = {}
            by_status: dict[str, int] = {}
            exceptions: list[dict[str, Any]] = []
            timeouts: list[dict[str, Any]] = []
            for row in rows:
                fc = str(row.get("function_code", ""))
                st = str(row.get("status", ""))
                by_fc[fc] = by_fc.get(fc, 0) + 1
                by_status[st] = by_status.get(st, 0) + 1
                if st.lower() == "exception":
                    exceptions.append(row)
                if "timeout" in st.lower() or "timeout" in str(row.get("summary", "")).lower():
                    timeouts.append(row)
            return {
                "ok": True,
                "path": str(src),
                "count": len(rows),
                "summary": {"by_function_code": by_fc, "by_status": by_status, "exceptions": len(exceptions), "timeouts": len(timeouts)},
                "exceptions": exceptions[:50],
                "timeouts": timeouts[:50],
                "records": rows[:max(1, min(int(limit or 200), 1000))],
            }
        except Exception as exc:
            return {"ok": False, "path": str(path), "error": str(exc)}

    def analyze_can_modbus_joint(self, asc_path: str, modbus_path: str, dbc_path: str = "", mapping_path: str = "", tolerance_s: float = 0.5, limit: int = 200) -> dict[str, Any]:
        """Correlate CAN ASC + Modbus capture/CSV using DBC and mapping JSON."""
        try:
            from .joint_analyzer import correlate
            if not str(dbc_path or "").strip() or not str(mapping_path or "").strip():
                return {"ok": False, "error": "DBC and mapping.json must be uploaded or selected explicitly. No default DBC is loaded."}
            asc = self._resolve_user_path(asc_path)
            modbus = self._resolve_user_path(modbus_path)
            dbc = self._resolve_user_path(dbc_path)
            mapping = self._resolve_user_path(mapping_path)
            missing = [str(x) for x in (asc, modbus, dbc, mapping) if not x.exists()]
            if missing:
                return {"ok": False, "error": "input file not found", "missing": missing, "paths": {"asc": str(asc), "modbus": str(modbus), "dbc": str(dbc), "mapping": str(mapping)}}
            rows = correlate(asc, modbus, dbc, mapping, tolerance_s=float(tolerance_s or 0.5))
            diffs = [float(r.get("abs_diff")) for r in rows if isinstance(r.get("abs_diff"), (int, float))]
            return {
                "ok": True,
                "paths": {"asc": str(asc), "modbus": str(modbus), "dbc": str(dbc), "mapping": str(mapping)},
                "count": len(rows),
                "summary": {
                    "tolerance_s": float(tolerance_s or 0.5),
                    "max_abs_diff": max(diffs) if diffs else None,
                    "avg_abs_diff": round(sum(diffs) / len(diffs), 6) if diffs else None,
                },
                "rows": rows[:max(1, min(int(limit or 200), 1000))],
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _diagnosis_history_path(self) -> Path:
        """Location for Web Diagnosis Center job history.

        Stored in the active profile when possible, otherwise beside runtime cwd.
        This keeps field diagnosis evidence together with the project profile.
        """
        try:
            if hasattr(self.window, "get_profile_path"):
                return Path(self.window.get_profile_path("diagnosis_history.jsonl"))
        except Exception:
            pass
        return Path.cwd() / "diagnosis_history.jsonl"

    def _diagnosis_export_dir(self) -> Path:
        try:
            if hasattr(self.window, "get_profile_path"):
                path = Path(self.window.get_profile_path("diagnosis_exports"))
            else:
                path = Path.cwd() / "diagnosis_exports"
        except Exception:
            path = Path.cwd() / "diagnosis_exports"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _compact_diagnosis_result(self, result: dict[str, Any]) -> dict[str, Any]:
        result = dict(result or {})
        compact: dict[str, Any] = {
            "ok": bool(result.get("ok")),
            "error": result.get("error"),
            "count": result.get("count"),
            "summary": result.get("summary") or {},
        }
        if "path" in result:
            compact["path"] = result.get("path")
        if "paths" in result:
            compact["paths"] = result.get("paths")
        # Evidence is intentionally small so the history file remains stable.
        if isinstance(result.get("exceptions"), list):
            compact["exceptions_preview"] = result.get("exceptions", [])[:10]
        if isinstance(result.get("timeouts"), list):
            compact["timeouts_preview"] = result.get("timeouts", [])[:10]
        if isinstance(result.get("records"), list):
            compact["records_preview"] = result.get("records", [])[:10]
        if isinstance(result.get("rows"), list):
            compact["rows_preview"] = result.get("rows", [])[:10]
        return compact

    def _build_diagnosis_evidence(self, kind: str, result: dict[str, Any]) -> list[dict[str, Any]]:
        evidence: list[dict[str, Any]] = []
        if not isinstance(result, dict):
            return evidence
        if not result.get("ok"):
            evidence.append({"severity": "error", "type": "analysis_failed", "message": str(result.get("error") or "analysis failed")})
            return evidence
        summary = result.get("summary") or {}
        if kind == "modbus":
            exceptions = int(summary.get("exceptions") or 0)
            timeouts = int(summary.get("timeouts") or 0)
            if exceptions:
                evidence.append({"severity": "warning", "type": "modbus_exception", "message": f"{exceptions} Modbus exception packet(s) detected"})
            if timeouts:
                evidence.append({"severity": "warning", "type": "modbus_timeout", "message": f"{timeouts} possible timeout event(s) detected"})
            if not evidence:
                evidence.append({"severity": "info", "type": "capture_summary", "message": f"{int(result.get('count') or 0)} packet record(s) parsed"})
        elif kind == "joint":
            max_diff = summary.get("max_abs_diff")
            avg_diff = summary.get("avg_abs_diff")
            if max_diff is not None:
                severity = "warning" if float(max_diff) > float(summary.get("tolerance_s") or 0.5) else "info"
                evidence.append({"severity": severity, "type": "can_modbus_time_delta", "message": f"max abs diff={max_diff}, avg abs diff={avg_diff}"})
            else:
                evidence.append({"severity": "info", "type": "joint_summary", "message": f"{int(result.get('count') or 0)} correlated row(s)"})
        return evidence

    def _save_diagnosis_job(self, kind: str, inputs: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        now = datetime.now().isoformat(timespec="seconds")
        job_id = f"diag-{int(time.time()*1000)}"
        compact = self._compact_diagnosis_result(result)
        evidence = self._build_diagnosis_evidence(kind, result)
        status = "success" if compact.get("ok") else "failed"
        if compact.get("ok") and any(e.get("severity") in {"warning", "error"} for e in evidence):
            status = "warning"
        item = {
            "job_id": job_id,
            "created_at": now,
            "kind": kind,
            "status": status,
            "inputs": inputs,
            "summary": compact.get("summary") or {},
            "count": compact.get("count"),
            "ok": compact.get("ok"),
            "error": compact.get("error"),
            "evidence": evidence,
            "result_preview": compact,
        }
        path = self._diagnosis_history_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")
        return item

    def diagnosis_history(self, limit: int = 100, kind: str = "") -> dict[str, Any]:
        path = self._diagnosis_history_path()
        if not path.exists():
            return {"ok": True, "path": str(path), "jobs": [], "count": 0}
        jobs: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                item = json.loads(line)
                if kind and str(item.get("kind")) != kind:
                    continue
                jobs.append(item)
            except Exception:
                continue
        jobs = jobs[-max(1, min(int(limit or 100), 1000)):]
        jobs.reverse()
        return {"ok": True, "path": str(path), "jobs": jobs, "count": len(jobs)}

    def diagnosis_history_clear(self) -> dict[str, Any]:
        path = self._diagnosis_history_path()
        try:
            if path.exists():
                path.unlink()
            return {"ok": True, "path": str(path), "message": "Diagnosis history cleared"}
        except Exception as exc:
            return {"ok": False, "path": str(path), "error": str(exc)}

    def diagnosis_history_csv(self, limit: int = 1000) -> str:
        data = self.diagnosis_history(limit=limit)
        rows = data.get("jobs") or []
        import csv, io
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["created_at", "job_id", "kind", "status", "ok", "count", "summary", "inputs", "error", "evidence"])
        for job in rows:
            writer.writerow([
                job.get("created_at", ""), job.get("job_id", ""), job.get("kind", ""), job.get("status", ""),
                job.get("ok", ""), job.get("count", ""), json.dumps(job.get("summary") or {}, ensure_ascii=False),
                json.dumps(job.get("inputs") or {}, ensure_ascii=False), job.get("error", ""),
                json.dumps(job.get("evidence") or [], ensure_ascii=False),
            ])
        return buf.getvalue()

    def analyze_modbus_capture_job(self, path: str, timeout_seconds: float = 2.0, limit: int = 200) -> dict[str, Any]:
        inputs = {"path": path, "timeout_seconds": timeout_seconds, "limit": limit}
        result = self.analyze_modbus_capture(path, timeout_seconds=timeout_seconds, limit=limit)
        job = self._save_diagnosis_job("modbus", inputs, result)
        result = dict(result)
        result["job"] = job
        return result

    def analyze_can_modbus_joint_job(self, asc_path: str, modbus_path: str, dbc_path: str = "", mapping_path: str = "", tolerance_s: float = 0.5, limit: int = 200) -> dict[str, Any]:
        inputs = {"asc_path": asc_path, "modbus_path": modbus_path, "dbc_path": dbc_path, "mapping_path": mapping_path, "tolerance_s": tolerance_s, "limit": limit}
        result = self.analyze_can_modbus_joint(asc_path, modbus_path, dbc_path, mapping_path, tolerance_s=tolerance_s, limit=limit)
        job = self._save_diagnosis_job("joint", inputs, result)
        result = dict(result)
        result["job"] = job
        return result



    def _profile_file_path(self, filename: str) -> Path:
        try:
            if hasattr(self.window, "get_profile_path"):
                return self.window.get_profile_path(filename)
        except Exception:
            pass
        return Path(filename)


    def packaging_check(self) -> dict[str, Any]:
        """v9.9 LTS: verify the files PyInstaller/Web-only mode needs.

        This does not execute external programs and does not touch devices.  It
        helps catch the common Windows packaging failure where the Runtime works
        but profiles/protocols/templates are missing from the dist folder.
        """
        base = Path.cwd()
        try:
            module_root = Path(__file__).resolve().parents[1]
        except Exception:
            module_root = base
        candidates = [
            ("root_alarm_map", "alarm_map.json"),
            ("root_pcs_config", "pcs_config.json"),
            ("root_runtime_config", "runtime_config.json"),
            ("root_site_config", "site_config.json"),
            ("profiles_default", "profiles/default"),
            ("bms_profiles", "bms_profiles"),
            ("pcs_profiles", "pcs_profiles"),
            ("bms_protocols", "bms_logger/protocols"),
            ("pcs_protocols", "protocols/pcs"),
            ("templates", "templates"),
            ("config_templates", "config_templates"),
            ("app_runtime", "app_runtime.py"),
            ("app_web", "app_web.py"),
            ("app_shutdown", "app_shutdown.py"),
            ("build_pyinstaller", "build_pyinstaller.py"),
        ]
        files: list[dict[str, Any]] = []
        for kind, rel in candidates:
            checked = []
            found_path = None
            for root in (base, module_root):
                path = root / rel
                checked.append(str(path))
                if path.exists():
                    found_path = path
                    break
            row = {"kind": kind, "relative_path": rel, "exists": found_path is not None, "checked": checked}
            if found_path is not None:
                try:
                    row.update({"path": str(found_path), "is_dir": found_path.is_dir(), "size_bytes": int(found_path.stat().st_size) if found_path.is_file() else None})
                except Exception as exc:
                    row["error"] = str(exc)
            files.append(row)
        missing = [x for x in files if not x.get("exists")]
        exe_dir = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else None
        return {
            "ok": not missing,
            "api_schema": API_SCHEMA_VERSION,
            "generated_at_iso": datetime.now().isoformat(timespec="seconds"),
            "frozen": bool(getattr(sys, "frozen", False)),
            "cwd": str(base),
            "module_root": str(module_root),
            "executable": sys.executable,
            "executable_dir": str(exe_dir) if exe_dir else "",
            "missing_count": len(missing),
            "files": files,
            "recommendations": [
                "Start Windows field work with ESS-AIO-Web.exe for Web-only mode.",
                "Keep ESS-AIO-Launcher.exe as Classic UI + Web fallback.",
                "If Web opens but profiles/templates are missing, rebuild with build_pyinstaller.py and check --add-data items.",
                "ESS-AIO-Shutdown.exe should be included in the Windows artifact as the emergency Runtime stop tool.",
            ],
        }

    def control_closure_audit(self) -> dict[str, Any]:
        """v9.9 LTS: operator-facing checklist for BMS/PCS control closure.

        It is intentionally conservative: it proves the Runtime has API routes,
        command audit, and recent feedback channels.  Actual energized device
        validation still has to be performed on site with safe conditions.
        """
        recent = self.command_audit(limit=80).get("commands", []) if hasattr(self, "command_audit") else []
        def _recent_for(names: set[str]) -> list[dict[str, Any]]:
            out=[]
            for c in recent:
                if str(c.get("name") or c.get("command") or "") in names:
                    out.append(c)
            return out[:10]
        checklist = [
            {"area":"BMS", "feature":"Heartbeat start/stop", "api":["/api/bms/heartbeat/start-all","/api/bms/heartbeat/stop-all"], "runtime_commands":["start_bms_heartbeats","stop_bms_heartbeats"], "status":"ready"},
            {"area":"BMS", "feature":"038B periodic status", "api":["/api/bms/038b/start","/api/bms/038b/stop"], "runtime_commands":["start_bms_038b_cycle","stop_bms_038b_cycle"], "status":"ready"},
            {"area":"BMS", "feature":"HV ON/OFF workflow", "api":["/api/bms/hv","/api/bms/hv-all"], "runtime_commands":["bms_hv","bms_hv_all"], "status":"ready", "safety":"browser confirmation required"},
            {"area":"BMS", "feature":"RTC write", "api":["/api/bms/rtc-write"], "runtime_commands":["bms_rtc_write"], "status":"ready", "safety":"browser confirmation required"},
            {"area":"BMS", "feature":"Register write", "api":["/api/bms/register-write"], "runtime_commands":["bms_register_write"], "status":"ready", "safety":"browser confirmation required"},
            {"area":"PCS", "feature":"Connect / Stop", "api":["/api/pcs/connect","/api/pcs/stop","/api/pcs/connect-all","/api/pcs/stop-all"], "runtime_commands":["connect_pcs","stop_pcs","connect_all_pcs","stop_all_pcs"], "status":"ready"},
            {"area":"PCS", "feature":"Start / DC breaker / power / Q / PF profile commands", "api":["/api/pcs/command","/api/pcs/fleet-command"], "runtime_commands":["pcs_command","pcs_fleet_command"], "status":"ready", "safety":"browser confirmation required"},
            {"area":"Register Debug", "feature":"Continuous and +0x400 strided read", "api":["/api/register/read","/api/register/lookup"], "runtime_commands":["register_read","register_lookup"], "status":"ready"},
        ]
        for row in checklist:
            row["recent_evidence"] = _recent_for(set(row.get("runtime_commands") or []))
        return {
            "ok": True,
            "api_schema": API_SCHEMA_VERSION,
            "generated_at_iso": datetime.now().isoformat(timespec="seconds"),
            "checklist": checklist,
            "note": "This is a software closure audit. On-site energized validation still requires safe test procedure and device feedback confirmation.",
        }

    def data_consistency_check(self) -> dict[str, Any]:
        """v9.9 LTS: compare compact/full snapshot and worker summary."""
        full = self.snapshot()
        compact = self.compact_snapshot()
        issues: list[dict[str, Any]] = []
        fs = full.get("summary", {}) if isinstance(full, dict) else {}
        cs = compact.get("summary", {}) if isinstance(compact, dict) else {}
        for key in ("bms_total", "bms_online", "bms_error", "pcs_total", "pcs_online", "pcs_error"):
            if fs.get(key) != cs.get(key):
                issues.append({"severity":"warning", "area":"snapshot", "message":f"summary.{key} differs between full and compact snapshot", "full":fs.get(key), "compact":cs.get(key)})
        fw = full.get("workers", {}) if isinstance(full, dict) else {}
        bms_total = int(fs.get("bms_total") or 0)
        pcs_total = int(fs.get("pcs_total") or 0)
        if bms_total and len(fw.get("bms_running", []) or []) > bms_total:
            issues.append({"severity":"warning", "area":"workers", "message":"BMS running worker count is larger than configured BMS total"})
        if pcs_total and len(fw.get("pcs_running", []) or []) > pcs_total:
            issues.append({"severity":"warning", "area":"workers", "message":"PCS running worker count is larger than configured PCS total"})
        return {
            "ok": not any(i.get("severity") == "error" for i in issues),
            "api_schema": API_SCHEMA_VERSION,
            "generated_at_iso": datetime.now().isoformat(timespec="seconds"),
            "full_snapshot_id": full.get("snapshot_id"),
            "compact_snapshot_id": compact.get("snapshot_id"),
            "summary": fs,
            "issues": issues,
            "recommendations": [
                "Use compact snapshot for Web auto-refresh and full snapshot only for export/debug.",
                "If UI and Web disagree, compare their displayed snapshot_id/timestamp first.",
            ],
        }

    def lts_final_audit(self) -> dict[str, Any]:
        parity = self.ui_web_parity_audit()
        health = self.health_monitor()
        packaging = self.packaging_check()
        consistency = self.data_consistency_check()
        control = self.control_closure_audit()
        blocking = []
        if not parity.get("ok"):
            blocking.append("ui_web_parity")
        if str(health.get("status")) == "fault":
            blocking.append("runtime_health")
        if not packaging.get("ok"):
            blocking.append("packaging_resources")
        if any(i.get("severity") == "error" for i in consistency.get("issues", [])):
            blocking.append("data_consistency")
        readiness = "lts_ready" if not blocking else "review_required"
        return {
            "ok": not blocking,
            "api_schema": API_SCHEMA_VERSION,
            "generated_at_iso": datetime.now().isoformat(timespec="seconds"),
            "readiness": readiness,
            "blocking": blocking,
            "summary": {
                "parity_percent": (parity.get("coverage") or {}).get("percent"),
                "health_status": health.get("status"),
                "health_score": health.get("score"),
                "missing_packaging_items": packaging.get("missing_count"),
                "consistency_issues": len(consistency.get("issues") or []),
                "control_rows": len(control.get("checklist") or []),
            },
            "parity": parity,
            "health": health,
            "packaging": packaging,
            "consistency": consistency,
            "control_closure": control,
        }

    def lts_final_csv(self) -> str:
        import csv, io
        data = self.lts_final_audit()
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["section", "item", "status", "details"])
        w.writerow(["lts", "readiness", data.get("readiness"), ";".join(data.get("blocking") or [])])
        for row in (data.get("control_closure") or {}).get("checklist", []) or []:
            w.writerow(["control", row.get("feature"), row.get("status"), "; ".join(row.get("api") or [])])
        for row in (data.get("packaging") or {}).get("files", []) or []:
            w.writerow(["packaging", row.get("relative_path"), "ok" if row.get("exists") else "missing", row.get("path", "")])
        for row in (data.get("consistency") or {}).get("issues", []) or []:
            w.writerow(["consistency", row.get("area"), row.get("severity"), row.get("message")])
        return buf.getvalue()

    def ui_web_parity_audit(self) -> dict[str, Any]:
        """Map legacy PySide UI features to Web EMS pages/APIs."""
        snap = self.snapshot()
        workers = snap.get("workers", {}) if isinstance(snap, dict) else {}
        summary = snap.get("summary", {}) if isinstance(snap, dict) else {}
        pages = {
            "Runtime Center": "/runtime-center", "Overview": "/overview", "Devices": "/devices", "Project": "/project",
            "BMS Control": "/ops", "PCS Control": "/pcs", "Strategy": "/strategy", "Analyzer": "/analyzer",
            "Register Debug": "/registerdebug", "Curves": "/curves", "Alarm Center": "/alarm-center", "BMS Alarms": "/alarms",
            "Release": "/release", "Health Monitor": "/health-monitor", "Runtime Lifecycle": "/runtime", "UI-Web Parity": "/parity",
        }
        checklist = [
            {"area":"Devices", "ui_feature":"BMS add/remove/start/stop/start all/stop all", "web_page":"Devices / Project / BMS Control", "api":["/api/project/bms/upsert","/api/project/bms/remove","/api/bms/start","/api/bms/stop","/api/bms/start-all","/api/bms/stop-all"], "status":"covered"},
            {"area":"Devices", "ui_feature":"PCS add/remove/manual connect/stop/connect all/stop all", "web_page":"Project / PCS Control", "api":["/api/project/pcs/upsert","/api/project/pcs/remove","/api/pcs/connect","/api/pcs/stop","/api/pcs/connect-all","/api/pcs/stop-all"], "status":"covered"},
            {"area":"BMS Control", "ui_feature":"Heartbeat start/stop", "web_page":"BMS Control", "api":["/api/bms/heartbeat/start-all","/api/bms/heartbeat/stop-all"], "status":"covered"},
            {"area":"BMS Control", "ui_feature":"0381 / HV ON / HV OFF workflow", "web_page":"BMS Control", "api":["/api/bms/hv","/api/bms/hv-all","/api/bms/command","/api/bms/register-write"], "status":"covered", "safety":"browser confirmation required for HV/write operations"},
            {"area":"BMS Control", "ui_feature":"038B periodic status", "web_page":"BMS Control", "api":["/api/bms/038b/start","/api/bms/038b/stop"], "status":"covered"},
            {"area":"BMS Control", "ui_feature":"RTC write", "web_page":"BMS Control", "api":["/api/bms/rtc-write"], "status":"covered", "safety":"browser confirmation required"},
            {"area":"BMS Control", "ui_feature":"Control-register write / quick preset", "web_page":"BMS Control / Register Debug", "api":["/api/bms/register-write","/api/register/lookup"], "status":"covered", "safety":"browser confirmation required"},
            {"area":"Register Debug", "ui_feature":"Single/continuous read", "web_page":"Register Debug", "api":["/api/register/read"], "status":"covered"},
            {"area":"Register Debug", "ui_feature":"Stride read every +0x400 for SBMU same point", "web_page":"Register Debug", "api":["/api/register/read"], "status":"covered"},
            {"area":"PCS Control", "ui_feature":"PCS start/stop/DC breaker/power/Q/PF/profile-driven commands", "web_page":"PCS Control", "api":["/api/pcs/command","/api/pcs/fleet-command"], "status":"covered", "safety":"browser confirmation required for command/fleet-command"},
            {"area":"CSV / Logs", "ui_feature":"BMS/PCS CSV start/stop/log status", "web_page":"BMS Control / Settings / Release", "api":["/api/csv/status","/api/csv/bms/start","/api/csv/bms/stop","/api/csv/pcs/start","/api/csv/pcs/stop","/api/logs/status","/api/logs/operation/recent"], "status":"covered"},
            {"area":"Curves", "ui_feature":"Real-time curves", "web_page":"Curves", "api":["/api/curves/live","/api/snapshot/compact"], "status":"covered", "note":"Runtime-side curve cache is used for large-site mode."},
            {"area":"Curves", "ui_feature":"CSV playback", "web_page":"Curves", "api":["browser-side CSV playback"], "status":"covered"},
            {"area":"Alarms", "ui_feature":"Alarm decode/list/export/ack", "web_page":"Alarm Center / BMS Alarms", "api":["/api/alarm-center","/api/alarm-center/ack","/api/alarm-center/export.csv","/api/device/bms/{name}/alarms"], "status":"covered"},
            {"area":"Strategy", "ui_feature":"Strategy config/start/stop/cluster settings/target power", "web_page":"Strategy", "api":["/api/strategy/config","/api/strategy/start","/api/strategy/stop","/api/strategy/start-all","/api/strategy/stop-all","/api/cluster/strategy-settings","/api/cluster/target-power"], "status":"covered", "safety":"browser confirmation required for high-risk dispatch actions"},
            {"area":"Packet Analyzer", "ui_feature":"Modbus pcap/pcapng analysis", "web_page":"Analyzer", "api":["/api/analyzer/modbus"], "status":"covered"},
            {"area":"Joint Analysis", "ui_feature":"CAN ASC + DBC + Modbus joint analysis", "web_page":"Analyzer", "api":["/api/analyzer/joint","/api/diagnosis/history"], "status":"covered"},
            {"area":"Release", "ui_feature":"Release/export/snapshot evidence", "web_page":"Release", "api":["/api/release/manifest","/api/release/snapshot.json","/api/release/export.zip"], "status":"covered"},
            {"area":"Runtime", "ui_feature":"Health, watchdog, shutdown lifecycle", "web_page":"Health Monitor / Runtime", "api":["/api/health","/api/runtime/watchdog","/api/health-monitor","/api/runtime/shutdown"], "status":"covered"},
        ]
        covered = sum(1 for x in checklist if x.get("status") == "covered")
        partial = sum(1 for x in checklist if x.get("status") == "partial")
        missing = [x for x in checklist if x.get("status") not in ("covered", "partial")]
        consistency = []
        bms_total = int(summary.get("bms_total") or 0)
        pcs_total = int(summary.get("pcs_total") or 0)
        if bms_total and len(workers.get("bms_running", []) or []) > bms_total:
            consistency.append({"severity":"warning","message":"BMS running worker count is larger than configured BMS count."})
        if pcs_total and len(workers.get("pcs_running", []) or []) > pcs_total:
            consistency.append({"severity":"warning","message":"PCS running worker count is larger than configured PCS count."})
        readiness = "ready" if not missing and not consistency else "review"
        return {
            "ok": not missing, "api_schema": API_SCHEMA_VERSION, "generated_at_iso": datetime.now().isoformat(timespec="seconds"),
            "readiness": readiness,
            "coverage": {"total": len(checklist), "covered": covered, "partial": partial, "missing": len(missing), "percent": round((covered + partial * 0.5) * 100.0 / max(1, len(checklist)), 1)},
            "pages": pages, "checklist": checklist, "missing": missing, "consistency_issues": consistency,
            "runtime_single_source": {"runtime_owns_device_io": True, "ui_should_not_poll_separately": True, "web_should_use_snapshot_or_runtime_api": True, "compact_snapshot_for_auto_refresh": "/api/snapshot/compact", "full_snapshot_for_export_debug": "/api/snapshot"},
            "next_recommendations": ["Use this Parity page after each PySide-side feature change.", "Open UI and Web together and confirm values come from the same snapshot timestamp.", "Keep high-risk Web writes behind browser confirmation and command_audit.jsonl."],
        }

    def ui_web_parity_csv(self) -> str:
        import csv, io
        data = self.ui_web_parity_audit()
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["area", "ui_feature", "web_page", "status", "api", "safety", "note"])
        for row in data.get("checklist", []) or []:
            writer.writerow([row.get("area", ""), row.get("ui_feature", ""), row.get("web_page", ""), row.get("status", ""), "; ".join(row.get("api", []) or []), row.get("safety", ""), row.get("note", "")])
        return buf.getvalue()

    def release_manifest(self) -> dict[str, Any]:
        """Return a handover/export manifest for the current Web EMS runtime.

        This is intentionally read-only. It gathers runtime snapshot plus files
        useful for customer handover, support, and rollback review.
        """
        snap = self.snapshot()
        candidates = [
            ("project_bms_devices", self._profile_file_path("devices.json")),
            ("project_pcs_configs", self._profile_file_path("pcs_configs.json")),
            ("site_config", self._site_config_path()),
            ("strategy_config", self._strategy_config_path()),
            ("runtime_state", self._runtime_state_path()),
            ("command_audit", self._command_audit_path()),
            ("alarm_ack", self._alarm_ack_path()),
            ("diagnosis_history", self._diagnosis_history_path()),
            ("readme", Path("README.md")),
            ("changelog", Path("CHANGELOG.md")),
            ("roadmap", Path("ROADMAP.md")),
            ("release_notes", Path("RELEASE_NOTES.md")),
        ]
        files = []
        for kind, path in candidates:
            try:
                exists = path.exists()
                files.append({
                    "kind": kind,
                    "path": str(path),
                    "exists": bool(exists),
                    "size_bytes": int(path.stat().st_size) if exists else 0,
                    "modified_iso": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds") if exists else "",
                })
            except Exception as exc:
                files.append({"kind": kind, "path": str(path), "exists": False, "size_bytes": 0, "error": str(exc)})
        summary = snap.get("summary", {}) if isinstance(snap, dict) else {}
        workers = snap.get("workers", {}) if isinstance(snap, dict) else {}
        notes = [
            "ESS-AIO Web EMS Release Snapshot",
            f"Generated: {datetime.now().isoformat(timespec='seconds')}",
            f"API schema: {API_SCHEMA_VERSION}",
            "",
            "Runtime summary:",
            f"- BMS total/online: {summary.get('bms_total', 0)}/{summary.get('bms_online', 0)}",
            f"- PCS total/online: {summary.get('pcs_total', 0)}/{summary.get('pcs_online', 0)}",
            f"- Strategies running: {len(workers.get('strategies', []) or [])}",
            f"- Commands tracked: {len(snap.get('command_acks', []) or [])}",
            "",
            "Safety:",
            "- Export is read-only.",
            "- Import/restore is not automatic.",
            "- High-risk runtime actions use browser confirmation and are still written to command audit.",
        ]
        for doc_name in ("RELEASE_NOTES.md", "CHANGELOG.md"):
            try:
                doc_path = Path(doc_name)
                if doc_path.exists():
                    doc_text = doc_path.read_text(encoding="utf-8", errors="replace").strip()
                    if doc_text:
                        notes.extend(["", f"--- {doc_name} ---", doc_text[:6000]])
            except Exception:
                pass
        return {
            "ok": True,
            "api_schema": API_SCHEMA_VERSION,
            "generated_at": time.time(),
            "generated_at_iso": datetime.now().isoformat(timespec="seconds"),
            "snapshot": snap,
            "files": files,
            "release_notes": "\n".join(notes),
            "safety_note": "Release Center only reads runtime/config/log files. It does not connect devices and does not write registers.",
        }

    def release_snapshot_json(self) -> str:
        return json.dumps(self.release_manifest(), ensure_ascii=False, indent=2)

    def release_export_zip_bytes(self) -> bytes:
        import io
        import zipfile
        manifest = self.release_manifest()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            zf.writestr("runtime_snapshot.json", json.dumps(manifest.get("snapshot") or {}, ensure_ascii=False, indent=2))
            zf.writestr("release_notes.txt", manifest.get("release_notes") or "")
            for f in manifest.get("files", []) or []:
                if not f.get("exists"):
                    continue
                path = Path(str(f.get("path") or ""))
                if not path.exists() or not path.is_file():
                    continue
                arcname = "profile/" + str(f.get("kind") or path.name) + "__" + path.name
                try:
                    zf.write(path, arcname)
                except Exception as exc:
                    zf.writestr(f"errors/{path.name}.txt", str(exc))
        return buf.getvalue()

    def separation_audit(self) -> dict[str, Any]:
        """Report remaining local/runtime ownership so the UI split is testable."""
        w = self.window
        return {
            "ok": True,
            "api_schema": API_SCHEMA_VERSION,
            "runtime_process": bool(getattr(w, "is_runtime_process", False)),
            "local_worker_counts": {
                "bms": len(getattr(w, "device_workers", {}) or {}),
                "pcs": len(getattr(w, "pcs_workers", {}) or {}),
                "strategy": len(getattr(w, "cluster_strategy_workers", {}) or {}),
                "heartbeat": len(getattr(w, "heartbeat_workers", {}) or {}),
            },
            "runtime_owned": {
                "device_io": True,
                "strategy": True,
                "site_config": True,
                "csv": True,
                "logs": True,
                "snapshot": True,
            },
            "ui_should_be_client_only": True,
        }


    def _parse_register_address(self, value: Any) -> int:
        text = str(value).strip().lower()
        if text.startswith("0x"):
            return int(text, 16)
        return int(text)

    def _bms_names_for_scope(self, scope: str, device: str = "") -> list[str]:
        scope = str(scope or "single").strip().lower()
        if scope in {"all", "all_online"}:
            return self._bms_worker_names()
        return [str(device or "").strip()]

    def _cluster_names(self) -> list[str]:
        names: list[str] = []
        try:
            site = getattr(self.window, "site", None)
            for c in getattr(site, "clusters", []) or []:
                n = str(getattr(c, "name", "") or "").strip()
                if n:
                    names.append(n)
        except Exception:
            pass
        if not names:
            try:
                payload = self._read_site_config_payload().get("config", {}) or {}
                for c in payload.get("clusters", []) or []:
                    n = str((c or {}).get("name") or "").strip()
                    if n:
                        names.append(n)
            except Exception:
                pass
        return sorted(set(names))

    def _register_point_lookup(self, device_type: str, device: str, address: Any) -> dict[str, Any]:
        addr = self._parse_register_address(address)
        if device_type == "pcs":
            try:
                cfg = (getattr(self.window, "pcs_configs", {}) or {}).get(str(device), {}) or {}
                points = cfg.get("points", {}) if isinstance(cfg, dict) else {}
                matches = []
                for key, p in (points or {}).items():
                    try:
                        pa = self._parse_register_address((p or {}).get("address"))
                    except Exception:
                        continue
                    if pa == addr:
                        matches.append({"key": key, **dict(p or {})})
                return {"ok": True, "device_type": "pcs", "device": device, "address": addr, "address_hex": f"0x{addr:04X}", "matches": matches}
            except Exception as exc:
                return {"ok": False, "error": str(exc), "address": addr, "address_hex": f"0x{addr:04X}"}
        try:
            from .point_table import PointTable, resolve_point_table_path
            profile = ""
            for d in getattr(self.window, "devices", []) or []:
                if str((d or {}).get("name") or "") == str(device):
                    profile = str((d or {}).get("profile") or (d or {}).get("driver") or "")
                    break
            path = resolve_point_table_path(profile=profile or None)
            table = PointTable(path)
            p = table.get_by_address(addr)
            if not p:
                return {"ok": True, "device_type": "bms", "device": device, "address": addr, "address_hex": f"0x{addr:04X}", "point": None, "path": str(path)}
            raw = p.raw or {}
            return {"ok": True, "device_type": "bms", "device": device, "address": addr, "address_hex": f"0x{addr:04X}", "path": str(path), "point": {"key": p.key, "name": p.description, "scale": p.scale, "offset": p.offset, "access": p.access, "section": p.section, "unit": raw.get("unit") or raw.get("Unit") or "", "raw": raw}}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "address": addr, "address_hex": f"0x{addr:04X}"}

    def _read_bms_register_block(self, device: str, register_type: str, start: int, count: int) -> list[int]:
        w = self.window
        if not hasattr(w, "_build_bms_client_for_device"):
            raise RuntimeError("BMS client factory unavailable")
        client = w._build_bms_client_for_device(device)
        if client is None:
            raise RuntimeError(f"BMS device not found: {device}")
        try:
            if not client.connect():
                raise RuntimeError(f"BMS connect failed: {device}")
            rt = str(register_type or "holding").lower()
            if rt == "input" and hasattr(client, "client") and hasattr(client.client, "read_input_registers"):
                rr = client.client.read_input_registers(address=start, count=count, device_id=getattr(client, "unit_id", 1))
                if getattr(rr, "isError", lambda: False)():
                    raise RuntimeError(f"Modbus error: {rr}")
                return [int(x) for x in getattr(rr, "registers", [])]
            if hasattr(client, "_read_holding_block"):
                regs = client._read_holding_block(start, count)
                if regs is None:
                    raise RuntimeError("empty response")
                return [int(x) for x in regs]
            raise RuntimeError("BMS client has no raw register read method")
        finally:
            try: client.close()
            except Exception: pass

    def _read_pcs_register_block(self, device: str, register_type: str, start: int, count: int) -> list[int]:
        client = self._pcs_factory(device)
        try:
            if not client.connect():
                raise RuntimeError(f"PCS connect failed: {device}")
            rt = str(register_type or "holding").lower()
            unit = getattr(client, "unit_id", 1)
            if not hasattr(client, "client"):
                raise RuntimeError("PCS low-level client unavailable")
            if rt == "input":
                rr = client.client.read_input_registers(address=start, count=count, device_id=unit)
            else:
                rr = client.client.read_holding_registers(address=start, count=count, device_id=unit)
            if getattr(rr, "isError", lambda: False)():
                raise RuntimeError(f"Modbus error: {rr}")
            return [int(x) for x in getattr(rr, "registers", [])]
        finally:
            try: client.close()
            except Exception: pass

    def _register_read_runtime(self, **kwargs: Any) -> dict[str, Any]:
        device_type = str(kwargs.get("device_type") or "bms").strip().lower()
        device = str(kwargs.get("device") or "").strip()
        register_type = str(kwargs.get("register_type") or "holding").strip().lower()
        mode = str(kwargs.get("mode") or "continuous").strip().lower()
        start = self._parse_register_address(kwargs.get("start"))
        count = max(1, min(125, int(kwargs.get("count") or 1)))
        step = self._parse_register_address(kwargs.get("step", "0x400"))
        quantity = max(1, min(128, int(kwargs.get("quantity") or 1)))
        length = max(1, min(32, int(kwargs.get("length") or 1)))
        if not device:
            return {"ok": False, "error": "Missing device"}
        blocks = []
        if mode in {"strided", "stride", "step"}:
            for i in range(quantity):
                blocks.append((start + i * step, length))
        else:
            blocks.append((start, count))
        rows = []
        errors = []
        reader = self._read_pcs_register_block if device_type == "pcs" else self._read_bms_register_block
        for block_start, block_count in blocks:
            try:
                regs = reader(device, register_type, block_start, block_count)
                for j, raw in enumerate(regs):
                    addr = block_start + j
                    lookup = self._register_point_lookup(device_type, device, addr)
                    point = lookup.get("point") or None
                    if point is None and lookup.get("matches"):
                        m = lookup.get("matches", [{}])[0]
                        point = {"key": m.get("key"), "name": m.get("name") or m.get("description") or m.get("key"), "scale": m.get("scale", 1), "offset": m.get("offset", 0), "access": m.get("access") or m.get("writable") or "", "unit": m.get("unit", "")}
                    scale = float((point or {}).get("scale", 1) or 1)
                    offset = float((point or {}).get("offset", 0) or 0)
                    rows.append({"address": addr, "address_hex": f"0x{addr:04X}", "raw": int(raw), "scaled": int(raw) * scale + offset, "point": point})
            except Exception as exc:
                errors.append({"address": block_start, "address_hex": f"0x{block_start:04X}", "count": block_count, "error": str(exc)})
        return {"ok": not errors, "device_type": device_type, "device": device, "register_type": register_type, "mode": mode, "start": start, "start_hex": f"0x{start:04X}", "blocks": [{"address": a, "address_hex": f"0x{a:04X}", "count": c} for a,c in blocks], "rows": rows, "errors": errors}

    def legacy_ui_action(self, action: str, params: dict[str, Any] | None = None, confirm_text: str = "") -> dict[str, Any]:
        """Execute or explain remaining PySide UI actions from Web.

        This is intentionally conservative: actions that need a local file dialog
        are exposed as visible buttons but return a clear not_supported status in
        the headless Runtime. High-risk write/control actions require EXECUTE.
        """
        params = params if isinstance(params, dict) else {}
        action = str(action or "").strip()
        w = self.window
        high_risk = {
            "clear_fault_all_online", "power_on_all_online", "power_off_all_online", "stay_all_online",
            "hv_on_all_online", "hv_off_all_online", "fleet_pcs_start", "fleet_pcs_stop",
            "set_active_power", "set_reactive_power", "fleet_set_active", "fleet_set_reactive",
            "start_cluster_strategy", "apply_start_all_strategy", "stop_all_pcs_cluster",
            "write_single", "apply_cluster_power_once", "pcs_start", "pcs_stop", "pcs_reset_fault", "pcs_hv_on", "pcs_hv_off", "close_dc_breaker", "open_dc_breaker", "hv_on_workflow", "hv_off_workflow", "start_workflow", "stop_workflow", "apply_plus_start_all",
        }
        if action in high_risk and str(confirm_text or "") != "EXECUTE":
            return {"ok": False, "action": action, "status": "confirmation_required", "error": "Type EXECUTE to run this action."}

        # File-dialog / folder-dialog actions cannot safely run in hidden Runtime.
        file_dialog_actions = {
            "browse_bms_output", "browse_pcs_output", "import_pcs_profile", "load_alarm_csv",
            "load_history_csv", "load_main_csv", "load_operation_log", "load_capture", "load_can_log",
            "select_asc", "select_modbus_capture", "select_dbc", "select_mapping",
            "import_site", "export_site", "import_strategy_json", "export_strategy_json",
            "import_template_package", "export_template_package", "import_point_table_json",
            "open_output_folder", "open_crash_logs", "open_reports_folder", "open_point_tables_folder", "browse", "export_diagnosis_text", "export_result_csv",
        }
        if action in file_dialog_actions:
            return {
                "ok": False, "action": action, "status": "web_path_required",
                "error": "This PySide button uses a local file/folder dialog. In Web mode use the matching path input, export endpoint, or Release Center package instead.",
            }

        def call(method: str, *a: Any, **kw: Any) -> dict[str, Any]:
            if not hasattr(w, method):
                return {"ok": False, "action": action, "method": method, "status": "missing_method", "error": f"Runtime window has no method {method}"}
            result = getattr(w, method)(*a, **kw)
            return {"ok": True, "action": action, "method": method, "result": result}

        # v9.7 alias table: keep Web parity buttons compatible with the exact
        # labels extracted from PySide pages.  Older v9.6 action ids used
        # semantic names; this layer accepts the raw canonical button ids too.
        aliases = {
            "about_ess_aio": "about",
            "apply_plus_start_all": "apply_start_all_strategy",
            "apply_to_current_profile": "apply_template",
            "cancel_workflow": "cancel_hv_workflow",
            "close_dc_breaker": "pcs_close_dc_breaker",
            "open_dc_breaker": "pcs_open_dc_breaker",
            "disconnect_all_pcs": "pcs_disconnect_all",
            "pcs_start": "pcs_start",
            "pcs_stop": "pcs_stop",
            "pcs_reset_fault": "pcs_reset_fault",
            "pcs_hv_on": "pcs_hv_on",
            "pcs_hv_off": "pcs_hv_off",
            "set_selected_as_active": "set_point_table_active",
            "reset_default": "reset_default_strategy",
            "start_cluster_strategy": "start_cluster_strategy",
            "stop_cluster_strategy": "stop_cluster_strategy",
            "start_038b_2_cycle": "start_bms_038b_cycle",
            "stop_038b_cycle": "stop_bms_038b_cycle",
            "start_all_bms_hb": "start_bms_heartbeats",
            "stop_all_bms_hb": "stop_bms_heartbeats",
            "start_heartbeat": "start_bms_heartbeats",
            "stop_heartbeat": "stop_bms_heartbeats",
            "write_stay_1": "stay_all_online",
            "write_power_on_2": "power_on_all_online",
            "write_power_off_3": "power_off_all_online",
            "hv_on_workflow": "hv_on_all_online",
            "hv_off_workflow": "hv_off_all_online",
            "start_workflow": "start_cluster_strategy",
            "stop_workflow": "stop_cluster_strategy",
            "stop_all_pcs_in_cluster": "stop_all_pcs_cluster",
            "start_selected": "start_selected_bms",
            "stop_selected": "stop_selected_bms",
            "refresh": "refresh_site",
            "validate": "validate_template",
            "run_diagnosis": "run_packet_diagnosis",
            "read_registers": "read_bms_debug",
            "dbc_mapping": "select_dbc",
        }
        if action in aliases:
            action = aliases[action]

        mapping = {
            # Overview / logs / release / report / timeline
            "start_all": "start_all_devices",
            "stop_all": "stop_all_devices",
            "clear_log_view": "handle_clear_log_view",
            "run_self_check": "run_startup_self_check",
            "about": "show_about_dialog",
            "start_session": "start_debug_session",
            "end_session": "end_debug_session",
            "generate_html_report": "generate_debug_report",
            "export_debug_package": "export_debug_package",
            "refresh_timeline": "refresh_event_timeline",
            "export_timeline_csv": "export_event_timeline_csv",
            "analyze_current_alarms": "analyze_current_alarms",
            "export_markdown": "export_packet_diagnosis_markdown",
            # Devices / PCS list
            "add_device": "add_device",
            "remove_selected_bms": "remove_selected_bms",
            "add_update_pcs": "add_or_update_pcs",
            "remove_pcs": "remove_selected_pcs",
            "set_current_pcs": "set_selected_pcs_as_current",
            "save_pcs_list": "save_pcs_config",
            "connect_selected_pcs": "start_selected_pcs_polling",
            "disconnect_selected_pcs": "stop_selected_pcs_polling",
            "pcs_disconnect_all": "stop_all_pcs_polling",
            # BMS / PCS control extras
            "read_bms_debug": "handle_register_debug_read",
            "read_bms_version": "read_bms_version",
            "cancel_hv_workflow": "cancel_hv_workflow",
            "refresh_pcs_list": "refresh_pcs_view",
            "refresh_pcs_status": "refresh_pcs_view",
            "test_pcs_config": "reload_pcs_config",
            "read_pcs_debug": "handle_register_debug_read",
            "stop_debug": "handle_register_debug_write",
            "start_debug": "handle_register_debug_write",
            "hv_on_debug": "handle_register_debug_write",
            "hv_off_debug": "handle_register_debug_write",
            "refresh_pcs_live_registers": "refresh_pcs_view",
            "fleet_status": "refresh_pcs_view",
            # Curves / replay
            "clear_history": "handle_clear_history",
            "apply_time_filter": "apply_history_time_filter",
            "add_point": "add_dynamic_point_from_combo",
            "clear_dynamic": "clear_dynamic_points",
            "toggle_favorite": "toggle_selected_driver_point_favorite",
            "add_to_curve": "add_selected_driver_point_to_curve",
            "replay_next_row": "handle_replay_next_row",
            "start_replay": "handle_replay_start",
            "stop_replay": "handle_replay_stop",
            # Packet/CAN local actions that do not require choosing a file
            "clear_packet": "clear_packet_capture",
            "export_packet_csv": "export_packet_analysis_csv",
            "analyze_issues": "analyze_modbus_issues",
            "send_to_register_tool": "send_selected_modbus_to_register_tool",
            "packet_apply": "apply_packet_table_filters",
            "packet_first": "packet_first_page",
            "packet_prev": "packet_prev_page",
            "packet_next": "packet_next_page",
            "packet_last": "packet_last_page",
            "clear_can": "clear_can_log",
            "clear_dbc": "clear_can_mapping",
            "export_frames": "export_can_frames_csv",
            "export_stats": "export_can_stats_csv",
            "can_apply": "apply_can_table_filters",
            "can_first": "can_first_page",
            "can_prev": "can_prev_page",
            "can_next": "can_next_page",
            "can_last": "can_last_page",
            "add_signal": "add_packet_can_signal_from_combo",
            "clear_plot": "clear_packet_can_signals",
            "export_signal_csv": "export_packet_can_signals_csv",
            "run_packet_diagnosis": "run_packet_diagnosis",
            "clear_all_evidence": "clear_packet_all_evidence",
            "export_diagnosis_csv": "export_packet_diagnosis_csv",
            "export_diagnosis_markdown": "export_packet_diagnosis_markdown",
            # Site / templates / settings / strategy
            "apply_runtime_params": "save_runtime_config",
            "apply_site": "apply_site_name",
            "refresh_site": "refresh_site_view",
            "save_site": "save_site_config",
            "rename_cluster": "apply_cluster_name",
            "add_cluster": "add_cluster",
            "delete_selected_cluster": "_delete_selected_cluster",
            "add_pcs_to_cluster": "apply_cluster_pcs_binding",
            "remove_pcs_from_cluster": "remove_cluster_pcs_binding",
            "move_bms": "move_bms_to_cluster",
            "refresh_power_map": "_refresh_cluster_power_map_editor",
            "auto_even_map": "_auto_even_cluster_power_map",
            "apply_power_map": "_apply_cluster_power_map_from_ui",
            "clear_power_map": "_clear_cluster_power_map",
            "reload_strategy": "reload_strategy_config",
            "save_strategy": "save_strategy_from_editor",
            "reset_default_strategy": "reset_default_strategy",
            "refresh_clusters": "_cluster_strategy_refresh_controls",
            "apply_fake_scenario": "apply_selected_strategy_fake_test",
            "reset_fake_scenarios": "reset_fake_scenarios",
            "validate_template": "validate_selected_template_package",
            "apply_template": "apply_selected_template_package",
            "refresh_templates": "refresh_template_package_view",
            "apply_driver_binding": "apply_driver_binding",
            "set_point_table_active": "apply_selected_point_table_template",
            "refresh_point_tables": "refresh_point_template_view",
        }
        if action in mapping:
            return call(mapping[action])

        # High-risk actions that already have Runtime APIs; route them here so the parity page buttons work.
        if action == "clear_fault_all_online":
            return self._execute("bms_command", command="clear_fault", scope="all_online", _command_id=None)
        if action == "power_on_all_online":
            return self._execute("bms_command", command="power_on", scope="all_online", _command_id=None)
        if action == "power_off_all_online":
            return self._execute("bms_command", command="power_off", scope="all_online", _command_id=None)
        if action == "stay_all_online":
            return self._execute("bms_command", command="stay", scope="all_online", _command_id=None)
        if action == "hv_on_all_online":
            return self._execute("bms_hv_all", mode="on", _command_id=None)
        if action == "hv_off_all_online":
            return self._execute("bms_hv_all", mode="off", _command_id=None)
        if action == "start_bms_heartbeats":
            return self._execute("start_bms_heartbeats", _command_id=None)
        if action == "stop_bms_heartbeats":
            return self._execute("stop_bms_heartbeats", _command_id=None)
        if action == "start_bms_038b_cycle":
            return self._execute("start_bms_038b_cycle", _command_id=None)
        if action == "stop_bms_038b_cycle":
            return self._execute("stop_bms_038b_cycle", _command_id=None)
        if action == "pcs_start":
            return self._execute("pcs_fleet_command", method="start", _command_id=None)
        if action == "pcs_stop":
            return self._execute("pcs_fleet_command", method="stop", _command_id=None)
        if action == "pcs_reset_fault":
            return self._execute("pcs_fleet_command", method="reset", _command_id=None)
        if action == "pcs_hv_on":
            return self._execute("pcs_fleet_command", method="hv_on", _command_id=None)
        if action == "pcs_hv_off":
            return self._execute("pcs_fleet_command", method="hv_off", _command_id=None)
        if action == "pcs_close_dc_breaker":
            return self._execute("pcs_fleet_command", method="close_dc_breaker", _command_id=None)
        if action == "pcs_open_dc_breaker":
            return self._execute("pcs_fleet_command", method="open_dc_breaker", _command_id=None)
        if action == "fleet_pcs_start":
            return self._execute("pcs_fleet_command", method="start", _command_id=None)
        if action == "fleet_pcs_stop":
            return self._execute("pcs_fleet_command", method="stop", _command_id=None)

        return {"ok": False, "action": action, "status": "unknown_action", "error": f"No Web mapping for PySide action: {action}"}

    def _execute(self, name: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        w = self.window
        command_id = kwargs.get("_command_id")
        if name == "ui_action":
            return self.legacy_ui_action(str(kwargs.get("action") or ""), kwargs.get("params") if isinstance(kwargs.get("params"), dict) else {}, str(kwargs.get("confirm_text") or ""))
        if name == "register_read":
            return self._register_read_runtime(**kwargs)
        if name == "register_lookup":
            return self._register_point_lookup(str(kwargs.get("device_type") or "bms").lower(), str(kwargs.get("device") or ""), kwargs.get("address"))
        if name == "start_bms":
            dev_name = str(kwargs.get("device") or (args[0] if args else "")).strip()
            if not dev_name:
                return {"ok": False, "command": name, "error": "Missing BMS device"}
            w.start_device_by_name(dev_name)
            return {"ok": True, "command": name, "device": dev_name}
        if name == "stop_bms":
            dev_name = str(kwargs.get("device") or (args[0] if args else "")).strip()
            if not dev_name:
                return {"ok": False, "command": name, "error": "Missing BMS device"}
            w.stop_device_by_name(dev_name)
            return {"ok": True, "command": name, "device": dev_name}
        if name == "bms_command":
            command = str(kwargs.get("command") or "").strip().lower()
            scope = str(kwargs.get("scope") or "single").strip().lower()
            dev_name = str(kwargs.get("device") or "").strip()
            if command == "clear_fault":
                method, cmd_args, label = "clear_fault", (), "Clear Fault"
            elif command in {"stay", "power_on", "power_off"}:
                val = {"stay": 1, "power_on": 2, "power_off": 3}[command]
                method, cmd_args, label = "write_ems_cmd", (val,), f"EMS cmd {val} ({command})"
            elif command == "write_038b":
                method, cmd_args, label = "write_insulation_monitor_disable", (), "write 0x038B=2"
            else:
                return {"ok": False, "command": name, "error": f"Unsupported BMS command: {command}"}
            names = self._bms_worker_names() if scope in {"all", "all_online"} else [dev_name]
            result = self._queue_bms_command(names, method, *cmd_args, label=label, command_id=command_id)
            result.update({"command": name, "bms_command": command, "scope": scope})
            return result
        if name == "bms_register_write":
            scope = str(kwargs.get("scope") or "single").strip().lower()
            dev_name = str(kwargs.get("device") or "").strip()
            try:
                address = self._parse_register_address(kwargs.get("address"))
                value = int(kwargs.get("value"))
            except Exception as exc:
                return {"ok": False, "command": name, "error": f"Invalid address/value: {exc}"}
            if address < 0 or address > 0xFFFF or value < 0 or value > 0xFFFF:
                return {"ok": False, "command": name, "error": "address and value must be 0..65535"}
            names = self._bms_names_for_scope(scope, dev_name)
            label = f"write_register 0x{address:04X}={value}"
            result = self._queue_bms_command(names, "write_single_register", address, value, label=label, command_id=command_id)
            result.update({"command": name, "scope": scope, "address": address, "address_hex": f"0x{address:04X}", "value": value})
            return result
        if name == "bms_rtc_write":
            scope = str(kwargs.get("scope") or "single").strip().lower()
            dev_name = str(kwargs.get("device") or "").strip()
            vals = [int(kwargs.get(k)) for k in ("year", "month", "day", "hour", "minute", "second")]
            y, mo, d, h, mi, sec = vals
            if not (2000 <= y <= 2099 and 1 <= mo <= 12 and 1 <= d <= 31 and 0 <= h <= 23 and 0 <= mi <= 59 and 0 <= sec <= 59):
                return {"ok": False, "command": name, "error": "Invalid RTC value"}
            names = self._bms_names_for_scope(scope, dev_name)
            # Common CATL-style EMS RTC block. Each field is queued as one write
            # on the existing BMS polling worker connection to avoid extra sockets.
            rtc_map = [(0x0382, y), (0x0383, mo), (0x0384, d), (0x0385, h), (0x0386, mi), (0x0387, sec)]
            total = 0
            queued = 0
            errors: list[str] = []
            for addr, val in rtc_map:
                r = self._queue_bms_command(names, "write_single_register", addr, val, label=f"RTC 0x{addr:04X}={val}", command_id=command_id)
                total += int(r.get("total", 0) or 0)
                queued += int(r.get("queued", 0) or 0)
                if not r.get("ok") and r.get("error"):
                    errors.append(str(r.get("error")))
            ok = queued > 0 and not errors
            self._update_command_ack(command_id, status="queued_to_device_worker" if ok else "no_device_queued", ok=ok, message=f"queued {queued}/{total} RTC register writes")
            return {"ok": ok, "command": name, "scope": scope, "queued": queued, "total": total, "rtc": {"year": y, "month": mo, "day": d, "hour": h, "minute": mi, "second": sec}, "errors": errors}
        if name == "bms_hv":
            dev_name = str(kwargs.get("device") or "").strip()
            if not dev_name:
                return {"ok": False, "command": name, "error": "Missing BMS device"}
            mode = str(kwargs.get("mode") or "on").strip().lower()
            method = "hv_on_bms_only" if mode == "on" else "hv_off_bms_only"
            label = "HV ON BMS-only sequence" if mode == "on" else "HV OFF BMS-only sequence"
            timeout = float(kwargs.get("timeout", getattr(w, "hv_step_timeout", 30.0) or 30.0))
            poll = float(kwargs.get("poll_interval", getattr(w, "hv_poll_interval", 1.0) or 1.0))
            result = self._queue_bms_command([dev_name], method, timeout, poll, label=label, command_id=command_id)
            result.update({"command": name, "mode": mode, "device": dev_name, "ignore_pcs_precheck": bool(kwargs.get("ignore_pcs_precheck", True))})
            return result
        if name == "bms_hv_all":
            mode = str(kwargs.get("mode") or "on").strip().lower()
            method = "hv_on_bms_only" if mode == "on" else "hv_off_bms_only"
            label = "HV ON BMS-only sequence" if mode == "on" else "HV OFF BMS-only sequence"
            timeout = float(kwargs.get("timeout", getattr(w, "hv_step_timeout", 30.0) or 30.0))
            poll = float(kwargs.get("poll_interval", getattr(w, "hv_poll_interval", 1.0) or 1.0))
            result = self._queue_bms_command(self._bms_worker_names(), method, timeout, poll, label=label, command_id=command_id)
            result.update({"command": name, "mode": mode, "ignore_pcs_precheck": bool(kwargs.get("ignore_pcs_precheck", True))})
            return result
        if name == "start_bms_heartbeats":
            names = self._bms_worker_names()
            started = 0
            for n in names:
                try:
                    if hasattr(w, "_start_bms_queue_heartbeat") and w._start_bms_queue_heartbeat(n):
                        started += 1
                except Exception:
                    pass
            return {"ok": True, "command": name, "started": started, "total": len(names)}
        if name == "stop_bms_heartbeats":
            stopped = 0
            for n in list(getattr(w, "bms_queue_heartbeat_timers", {}).keys()):
                try:
                    if hasattr(w, "_stop_bms_queue_heartbeat") and w._stop_bms_queue_heartbeat(n):
                        stopped += 1
                except Exception:
                    pass
            return {"ok": True, "command": name, "stopped": stopped}
        if name == "start_bms_038b_cycle":
            # Runtime phase 3: keep this using the existing Qt timer implementation
            # inside the runtime process so the periodic task survives UI freezes.
            if hasattr(w, "handle_start_bms_insulation_disable_cycle"):
                w.handle_start_bms_insulation_disable_cycle()
                return {"ok": True, "command": name}
            return {"ok": False, "command": name, "error": "038B handler unavailable"}
        if name == "stop_bms_038b_cycle":
            if hasattr(w, "handle_stop_bms_insulation_disable_cycle"):
                w.handle_stop_bms_insulation_disable_cycle()
                return {"ok": True, "command": name}
            return {"ok": False, "command": name, "error": "038B handler unavailable"}
        if name == "connect_pcs":
            pcs_name = str(kwargs.get("pcs") or (args[0] if args else "")).strip()
            if not pcs_name:
                return {"ok": False, "command": name, "error": "Missing PCS"}
            w.start_pcs_polling_by_name(pcs_name)
            return {"ok": True, "command": name, "pcs": pcs_name}
        if name == "stop_pcs":
            pcs_name = str(kwargs.get("pcs") or (args[0] if args else "")).strip()
            if not pcs_name:
                return {"ok": False, "command": name, "error": "Missing PCS"}
            w.stop_pcs_polling_by_name(pcs_name)
            return {"ok": True, "command": name, "pcs": pcs_name}
        if name == "pcs_command":
            pcs_name = str(kwargs.get("pcs") or "").strip()
            method = str(kwargs.get("method") or "").strip()
            value = kwargs.get("value", None)
            if not method:
                return {"ok": False, "command": name, "error": "Missing PCS method"}
            args2 = () if value is None else (float(value),)
            result = self._queue_pcs_command([pcs_name], method, *args2, label=method, command_id=command_id)
            result.update({"command": name, "pcs": pcs_name, "method": method})
            return result
        if name == "pcs_fleet_command":
            method = str(kwargs.get("method") or "").strip()
            value = kwargs.get("value", None)
            names = self._pcs_names()
            args2 = () if value is None else (float(value),)
            result = self._queue_pcs_command(names, method, *args2, label=method, command_id=command_id)
            result.update({"command": name, "method": method})
            return result
        if name == "start_all_bms":
            w.start_all()
            return {"ok": True, "command": name}
        if name == "stop_all_bms":
            w.stop_all()
            return {"ok": True, "command": name}
        if name == "connect_all_pcs":
            w.start_all_pcs_polling()
            return {"ok": True, "command": name}
        if name == "stop_all_pcs":
            w.stop_all_pcs_polling()
            return {"ok": True, "command": name}
        if name == "start_strategy":
            cluster = str(kwargs.get("cluster") or (args[0] if args else "")).strip()
            if cluster and hasattr(w, "cluster_strategy_combo"):
                idx = w.cluster_strategy_combo.findText(cluster)
                if idx >= 0:
                    w.cluster_strategy_combo.setCurrentIndex(idx)
                else:
                    return {"ok": False, "command": name, "error": f"Unknown cluster: {cluster}"}
            w.start_cluster_strategy()
            return {"ok": True, "command": name, "cluster": cluster}
        if name == "stop_strategy":
            cluster = str(kwargs.get("cluster") or (args[0] if args else "")).strip()
            if cluster and hasattr(w, "cluster_strategy_combo"):
                idx = w.cluster_strategy_combo.findText(cluster)
                if idx >= 0:
                    w.cluster_strategy_combo.setCurrentIndex(idx)
            w.stop_cluster_strategy()
            return {"ok": True, "command": name, "cluster": cluster}
        if name == "start_all_strategies":
            names = self._cluster_names()
            actions = []
            errors = []
            for cluster in names:
                try:
                    if hasattr(w, "cluster_strategy_combo"):
                        idx = w.cluster_strategy_combo.findText(cluster)
                        if idx >= 0:
                            w.cluster_strategy_combo.setCurrentIndex(idx)
                    w.start_cluster_strategy()
                    actions.append({"cluster": cluster, "ok": True})
                except Exception as exc:
                    errors.append(f"{cluster}: {exc}")
                    actions.append({"cluster": cluster, "ok": False, "error": str(exc)})
            return {"ok": not errors, "command": name, "clusters": names, "actions": actions, "errors": errors}
        if name == "stop_all_strategies":
            names = self._cluster_names()
            actions = []
            errors = []
            for cluster in names:
                try:
                    if hasattr(w, "cluster_strategy_combo"):
                        idx = w.cluster_strategy_combo.findText(cluster)
                        if idx >= 0:
                            w.cluster_strategy_combo.setCurrentIndex(idx)
                    w.stop_cluster_strategy()
                    actions.append({"cluster": cluster, "ok": True})
                except Exception as exc:
                    errors.append(f"{cluster}: {exc}")
                    actions.append({"cluster": cluster, "ok": False, "error": str(exc)})
            return {"ok": not errors, "command": name, "clusters": names, "actions": actions, "errors": errors}
        if name == "set_cluster_strategy_settings":
            cluster = str(kwargs.get("cluster") or "").strip()
            if hasattr(w, "cluster_strategy_combo"):
                idx = w.cluster_strategy_combo.findText(cluster)
                if idx >= 0:
                    w.cluster_strategy_combo.setCurrentIndex(idx)
                else:
                    return {"ok": False, "command": name, "error": f"Unknown cluster: {cluster}"}
            mapping = {
                "mode": "cluster_strategy_mode_combo",
                "target_power_kw": "cluster_strategy_target_spin",
                "ramp_step_kw": "cluster_strategy_ramp_step_spin",
                "ramp_interval_s": "cluster_strategy_ramp_interval_spin",
                "bms_timeout_s": "cluster_strategy_timeout_spin",
                "charge_cutoff_mv": "cluster_strategy_charge_cutoff_spin",
                "discharge_cutoff_mv": "cluster_strategy_discharge_cutoff_spin",
                "allocation_mode": "cluster_strategy_allocation_combo",
                "timeout_action": "cluster_strategy_timeout_action_combo",
            }
            for key, attr in mapping.items():
                if key not in kwargs or kwargs.get(key) is None:
                    continue
                widget = getattr(w, attr, None)
                if widget is None:
                    continue
                try:
                    if hasattr(widget, "setCurrentText"):
                        widget.setCurrentText(str(kwargs[key]))
                    elif hasattr(widget, "setValue"):
                        widget.setValue(float(kwargs[key]))
                except Exception:
                    pass
            if hasattr(w, "capture_selected_cluster_strategy_from_ui"):
                w.capture_selected_cluster_strategy_from_ui()
            if hasattr(w, "save_site_config"):
                w.save_site_config()
            return {"ok": True, "command": name, "cluster": cluster}

        if name == "runtime_settings":
            return self._read_runtime_settings()
        if name == "save_runtime_settings":
            payload = kwargs.get("settings") or (args[0] if args else {})
            return self._write_runtime_settings(payload if isinstance(payload, dict) else {})
        if name == "power_map_status":
            return self._power_map_status()

        if name == "get_site_config":
            result = self._read_site_config_payload()
            result.update({"command": name})
            return result
        if name == "import_site_config":
            payload = kwargs.get("config") or (args[0] if args else {})
            result = self._write_site_config_payload(payload if isinstance(payload, dict) else {})
            result.update({"command": name})
            return result
        if name == "save_site_config":
            if hasattr(w, "save_site_config"):
                w.save_site_config()
            result = self._read_site_config_payload()
            result.update({"command": name})
            return result
        if name == "delete_cluster":
            cluster = str(kwargs.get("cluster") or (args[0] if args else "")).strip()
            result = self._delete_cluster_from_site_config(cluster)
            result.update({"command": name})
            return result

        if name == "project_config":
            result = self._project_config_summary()
            result.update({"command": name})
            return result
        if name == "project_validate":
            result = self._validate_project_config_runtime()
            result.update({"command": name})
            return result
        if name == "upsert_bms_config":
            payload = kwargs.get("config") or (args[0] if args else {})
            result = self._upsert_bms_config_runtime(payload if isinstance(payload, dict) else {})
            result.update({"command": name})
            return result
        if name == "remove_bms_config":
            dev_name = str(kwargs.get("device") or kwargs.get("name") or (args[0] if args else "")).strip()
            result = self._remove_bms_config_runtime(dev_name)
            result.update({"command": name})
            return result
        if name == "upsert_pcs_config":
            payload = kwargs.get("config") or (args[0] if args else {})
            result = self._upsert_pcs_config_runtime(payload if isinstance(payload, dict) else {})
            result.update({"command": name})
            return result
        if name == "remove_pcs_config":
            pcs_name = str(kwargs.get("pcs") or kwargs.get("name") or (args[0] if args else "")).strip()
            result = self._remove_pcs_config_runtime(pcs_name)
            result.update({"command": name})
            return result

        if name == "runtime_restore":
            return self.restore_runtime_state(
                restore_bms=bool(kwargs.get("restore_bms", False)),
                restore_pcs=bool(kwargs.get("restore_pcs", False)),
                restore_csv=bool(kwargs.get("restore_csv", False)),
                restore_strategy=bool(kwargs.get("restore_strategy", False)),
            )

        if name == "read_bms_racks":
            result = self._read_bms_racks_runtime(str(kwargs.get("device") or ""), int(kwargs.get("count") or 16))
            result.update({"command": name})
            return result
        if name == "apply_bms_rack_mask":
            result = self._apply_bms_rack_mask_runtime(str(kwargs.get("device") or ""), list(kwargs.get("changes") or []))
            result.update({"command": name})
            return result
        if name == "read_bms_version":
            result = self._read_bms_version_runtime(str(kwargs.get("device") or ""), int(kwargs.get("sbmu_count") or 1))
            result.update({"command": name})
            return result
        if name == "csv_status":
            result = self._recording_status()
            result.update({"ok": True, "command": name})
            return result
        if name == "start_bms_csv":
            names = list(kwargs.get("devices") or kwargs.get("names") or [])
            result = self._start_bms_csv_runtime([str(x) for x in names])
            result.update({"command": name})
            return result
        if name == "stop_bms_csv":
            names = list(kwargs.get("devices") or kwargs.get("names") or [])
            result = self._stop_bms_csv_runtime([str(x) for x in names])
            result.update({"command": name})
            return result
        if name == "start_pcs_csv":
            names = list(kwargs.get("devices") or kwargs.get("names") or kwargs.get("pcs") or [])
            result = self._start_pcs_csv_runtime([str(x) for x in names])
            result.update({"command": name})
            return result
        if name == "stop_pcs_csv":
            names = list(kwargs.get("devices") or kwargs.get("names") or kwargs.get("pcs") or [])
            result = self._stop_pcs_csv_runtime([str(x) for x in names])
            result.update({"command": name})
            return result
        if name == "log_status":
            result = self._log_status()
            result.update({"ok": True, "command": name})
            return result
        if name == "operation_log_recent":
            return self._read_recent_operation_log(int(kwargs.get("max_lines", 300) or 300))

        if name == "runtime_state":
            result = self.runtime_state_status()
            result.update({"command": name})
            return result
        if name == "runtime_state_clear":
            result = self.clear_runtime_state()
            result.update({"command": name})
            return result

        if name == "runtime_shutdown":
            try:
                try:
                    self._persist_runtime_state("shutdown")
                except Exception:
                    pass
                from PySide6.QtWidgets import QApplication
                app = QApplication.instance()
                if app is not None:
                    QTimer.singleShot(250, app.quit)
                return {"ok": True, "command": name, "message": "Runtime shutdown scheduled", "pid": os.getpid()}
            except Exception as exc:
                return {"ok": False, "command": name, "error": str(exc)}

        if name == "set_cluster_target_power":
            cluster = str(kwargs.get("cluster") or (args[0] if args else "")).strip()
            power_kw = float(kwargs.get("power_kw", args[1] if len(args) > 1 else 0.0))
            if hasattr(w, "cluster_strategy_combo"):
                idx = w.cluster_strategy_combo.findText(cluster)
                if idx >= 0:
                    w.cluster_strategy_combo.setCurrentIndex(idx)
                else:
                    return {"ok": False, "command": name, "error": f"Unknown cluster: {cluster}"}
            if hasattr(w, "cluster_strategy_target_spin"):
                w.cluster_strategy_target_spin.setValue(power_kw)
            if hasattr(w, "capture_selected_cluster_strategy_from_ui"):
                w.capture_selected_cluster_strategy_from_ui()
            if hasattr(w, "save_site_config"):
                w.save_site_config()
            return {"ok": True, "command": name, "cluster": cluster, "power_kw": power_kw}
        return {"ok": False, "command": name, "error": f"Unknown runtime command: {name}"}



def _runtime_shutdown_html() -> str:
    return """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ESS-AIO Runtime Shutdown</title>
<style>
:root{color-scheme:dark;background:#0b1220;color:#e6edf7;font-family:Inter,Segoe UI,Arial,sans-serif}body{margin:0;min-height:100vh;display:grid;place-items:center;background:radial-gradient(circle at top,#142033,#080d16)}.card{width:min(680px,calc(100vw - 32px));background:#111a2b;border:1px solid #24324a;border-radius:22px;box-shadow:0 22px 70px rgba(0,0,0,.35);padding:28px}h1{margin:0 0 8px;font-size:26px}.muted{color:#92a4bd;line-height:1.6}.row{display:flex;gap:10px;flex-wrap:wrap;margin-top:22px}button,a{border:1px solid #31435f;background:#17243a;color:#e6edf7;border-radius:12px;padding:12px 16px;text-decoration:none;cursor:pointer;font-weight:600}.danger{background:#5b1e2d;border-color:#9f3854}.danger:hover{background:#7a2840}pre{margin-top:18px;white-space:pre-wrap;background:#09111f;border:1px solid #24324a;border-radius:14px;padding:14px;color:#c8d4e4}.ok{color:#46d39a}.bad{color:#ff6b85}

    .analyzer-upload-grid{display:grid;grid-template-columns:repeat(4,minmax(180px,1fr));gap:12px}.upload-card,.analysis-card{background:rgba(15,23,42,.72);border:1px solid var(--line);border-radius:14px;padding:14px}.upload-card h3{margin:0 0 10px}.upload-card input[type=file]{width:100%;background:#020617;border:1px dashed #334155;border-radius:10px;padding:8px;color:var(--muted);margin-bottom:10px}.form-stack{display:grid;gap:10px;margin:8px 0 12px}.form-stack label{display:grid;gap:5px;color:var(--muted);font-size:12px}.form-stack input{width:100%;box-sizing:border-box}.small{font-size:12px}@media(max-width:1200px){.analyzer-upload-grid{grid-template-columns:repeat(2,minmax(180px,1fr));}}@media(max-width:700px){.analyzer-upload-grid{grid-template-columns:1fr;}}


    .runtime-page-footer{margin-top:18px;padding:12px 16px;border:1px solid var(--line);border-radius:14px;background:rgba(5,11,20,.62);display:flex;gap:16px;flex-wrap:wrap;color:var(--muted);font-size:12px}
    .runtime-page-footer b{color:var(--text)}
    .status-strip{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px;margin:10px 0 0}
    .status-item{border:1px solid var(--line);border-radius:12px;padding:10px 12px;background:rgba(2,6,23,.35)}
    .status-item .k{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.06em}.status-item .v{font-size:18px;font-weight:700;margin-top:4px}
    .command-feedback{border:1px solid var(--line2);border-radius:12px;padding:10px 12px;background:rgba(47,155,255,.06);margin-top:10px}
    .control-layout{display:grid;grid-template-columns:minmax(0,1.25fr) minmax(360px,.85fr);gap:16px;align-items:start}
    .quick-add-grid{display:grid;grid-template-columns:repeat(2,minmax(360px,1fr));gap:16px;align-items:start}.quick-add-card{border:1px solid var(--line);border-radius:14px;padding:14px;background:rgba(2,6,23,.25)}
    .analyzer-visual-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px;margin:12px 0}.analysis-step{border:1px solid var(--line);border-radius:14px;padding:14px;background:rgba(2,6,23,.32)}.analysis-step h3{margin:0 0 8px}
    @media(max-width:1100px){.control-layout,.quick-add-grid{grid-template-columns:1fr}}

  </style></head>
<body><div class="card"><h1>Shutdown ESS-AIO Runtime</h1>
<p class="muted">This standalone page uses only one small script. Use it when the main Web UI is frozen or not responding. Closing the browser tab will <b>not</b> stop Runtime; use this button or the Windows shutdown helper.</p>
<div class="row"><button class="danger" onclick="shutdownRuntime()">Shutdown Runtime</button><a href="/">Back to Web EMS</a><a href="/api/health" target="_blank">Health JSON</a></div>
<pre id="out">Runtime is still running until shutdown is requested.</pre></div>
<script>
async function shutdownRuntime(){
  if(!confirm('Stop ESS-AIO Runtime now? This will stop polling, CSV, workers, strategy and Web API.')) return;
  const out=document.getElementById('out'); out.textContent='Requesting shutdown...';
  try{
    const r=await fetch('/api/runtime/shutdown',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({source:'shutdown-page',confirmed:true})});
    const j=await r.json(); out.textContent=JSON.stringify(j,null,2)+'\n\nRuntime is stopping. This page will become unavailable shortly.';
    setTimeout(async()=>{ try{ await fetch('/api/health',{cache:'no-store'}); out.textContent+='\n\nHealth still responds; wait a few seconds.'; }catch(e){ out.textContent+='\n\nRuntime is offline. You may close this browser.'; } },1500);
  }catch(e){ out.textContent=String(e); }
}
</script></body></html>"""

def _runtime_dashboard_html() -> str:
    return r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>ESS-AIO Web EMS</title>
  <style>
    :root { color-scheme: dark; --bg:#050b14; --bg2:#07111f; --panel:#0d1726; --panel2:#0a1422; --panel3:#101c2d; --text:#f3f7fb; --muted:#9aa7b8; --ok:#27d66d; --warn:#f7b955; --bad:#ff4d5e; --line:#233246; --line2:#2c4058; --accent:#2f9bff; --accent2:#62b8ff; --shadow:0 20px 50px rgba(0,0,0,.35); }
    * { box-sizing:border-box; }
    body { margin:0; font-family: Inter, ui-sans-serif, system-ui, -apple-system, Segoe UI, Arial, sans-serif; background:radial-gradient(circle at top left, rgba(47,155,255,.16), transparent 30%), linear-gradient(135deg, #040914 0%, #07111f 45%, #03101c 100%); color:var(--text); }
    header { position:sticky; top:0; z-index:20; height:64px; padding:0 18px 0 0; background:rgba(5,11,20,.82); backdrop-filter:blur(18px); border-bottom:1px solid var(--line); display:flex; align-items:center; justify-content:space-between; gap:12px; }
    h1 { margin:0; font-size:22px; letter-spacing:-.02em; }
    h2 { font-size:17px; margin:0; letter-spacing:-.01em; }
    .muted { color:var(--muted); }
    .brand { width:260px; height:64px; padding:0 18px; display:flex; align-items:center; gap:12px; border-right:1px solid var(--line); background:rgba(3,8,16,.45); }
    .logo { width:30px; height:30px; border-radius:8px; background:linear-gradient(135deg,var(--accent),#75d3ff); box-shadow:0 0 28px rgba(47,155,255,.35); position:relative; }
    .logo:before,.logo:after{content:""; position:absolute; background:#06111f; border-radius:2px;} .logo:before{width:9px;height:22px;left:10px;top:4px;} .logo:after{width:22px;height:9px;left:4px;top:10px;}
    .topnav { flex:1; display:flex; gap:6px; align-items:center; height:100%; overflow:auto; padding-left:14px; }
    .topnav button { width:auto; margin:0; padding:12px 16px; border-radius:12px; background:transparent; border:1px solid transparent; color:#c7d2e1; font-size:14px; }
    .topnav button.active { color:var(--accent2); background:linear-gradient(180deg,rgba(47,155,255,.18),rgba(47,155,255,.06)); border-color:rgba(47,155,255,.28); box-shadow:inset 0 -2px 0 var(--accent); }
    .shell { display:grid; grid-template-columns: 260px minmax(0, 1fr); min-height:calc(100vh - 64px); }
    nav { background:rgba(5,11,20,.78); border-right:1px solid var(--line); padding:18px 12px 76px; position:sticky; top:64px; height:calc(100vh - 64px); overflow:auto; }
    nav:before { content:'OVERVIEW'; display:block; color:#8492a6; font-size:11px; letter-spacing:.08em; margin:8px 10px 8px; }
    nav button { width:100%; text-align:left; margin-bottom:6px; background:transparent; border:1px solid transparent; color:#cbd5e1; border-radius:10px; padding:10px 12px; font-size:14px; }
    nav button.active { background:linear-gradient(90deg,rgba(47,155,255,.24),rgba(47,155,255,.08)); border-color:rgba(47,155,255,.25); color:var(--accent2); box-shadow:inset 3px 0 0 var(--accent); }
    nav button:hover { background:rgba(47,155,255,.10); color:#fff; }
    .nav-section { color:#8492a6; font-size:11px; letter-spacing:.08em; margin:18px 10px 8px; text-transform:uppercase; }
    .nav-item { display:flex; align-items:center; gap:10px; }
    .nav-icon { width:18px; text-align:center; opacity:.95; font-size:15px; }
    .nav-sub { margin-left:28px; padding-left:10px; border-left:1px solid var(--line2); }
    .nav-sub button { padding:8px 10px; font-size:13px; margin-bottom:4px; }
    .nav-sub button.active { box-shadow:inset 2px 0 0 var(--accent); }
    .chip-list { display:flex; flex-wrap:wrap; gap:6px; margin-bottom:8px; min-height:28px; }
    .chip { display:inline-flex; align-items:center; gap:6px; padding:5px 8px; border:1px solid var(--line2); border-radius:999px; background:rgba(255,255,255,.055); color:#e6edf7; }
    .chip button { padding:0 4px; border:0; background:transparent; color:#cbd5e1; transform:none; }
    .chip button:hover { color:var(--bad); background:transparent; transform:none; }
    .bind-picker { display:flex; gap:8px; align-items:center; }
    .bind-picker select { min-width:210px; width:100%; }
    .bind-picker button { white-space:nowrap; }

    main { padding:22px; min-width:0; }
    .page { display:none; }
    .page.active { display:block; }
    .page:before { content:attr(data-title); display:block; font-size:24px; font-weight:750; margin:0 0 16px; letter-spacing:-.03em; }
    .cards { display:grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap:14px; margin-bottom:16px; }
    .card { background:linear-gradient(180deg,rgba(17,31,49,.94),rgba(8,18,31,.94)); border:1px solid var(--line); border-radius:16px; padding:16px; box-shadow:var(--shadow); }
    .card .label { color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.06em; }
    .card .value { font-size:30px; font-weight:760; margin-top:6px; }

    .dashboard-cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:14px;margin-bottom:16px}
    .metric-card{position:relative;background:linear-gradient(180deg,rgba(18,33,55,.96),rgba(8,18,31,.96));border:1px solid var(--line);border-radius:18px;padding:18px;box-shadow:var(--shadow);overflow:hidden}
    .metric-card:after{content:"";position:absolute;right:-28px;top:-28px;width:100px;height:100px;border-radius:999px;background:rgba(47,155,255,.08)}
    .metric-card.hero{background:linear-gradient(135deg,rgba(47,155,255,.20),rgba(8,18,31,.96) 58%)}
    .metric-label{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.08em}
    .metric-value{font-size:30px;font-weight:800;margin-top:8px;letter-spacing:-.04em}
    .metric-foot{color:var(--muted);font-size:12px;margin-top:8px}
    .dashboard-charts{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:14px}
    .chart-card{background:rgba(2,6,23,.34);border:1px solid var(--line);border-radius:16px;padding:16px}
    .chart-title{font-weight:750;margin-bottom:12px;color:#dbeafe}
    .bar-row{margin:12px 0}.bar-top{display:flex;justify-content:space-between;gap:12px;color:var(--muted);font-size:12px;margin-bottom:6px}.bar-top b{color:var(--text)}
    .bar-track{height:10px;background:rgba(148,163,184,.16);border-radius:999px;overflow:hidden;border:1px solid rgba(148,163,184,.10)}
    .bar-fill{height:100%;border-radius:999px;background:linear-gradient(90deg,var(--accent),var(--accent2))}.bar-fill.ok{background:linear-gradient(90deg,#22c55e,#46d39a)}.bar-fill.warn{background:linear-gradient(90deg,#f59e0b,#facc15)}.bar-fill.bad{background:linear-gradient(90deg,#ef4444,#fb7185)}.bar-fill.accent{background:linear-gradient(90deg,#2f9bff,#22d3ee)}
    #overviewDashboard.status-strip{display:block}
    .ok { color:var(--ok); } .warn { color:var(--warn); } .bad { color:var(--bad); } .accent { color:var(--accent2); }
    .toolbar { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
    button, a.btn, select, input, textarea { background:#091523; color:var(--text); border:1px solid var(--line2); border-radius:11px; padding:9px 12px; text-decoration:none; font-size:13px; outline:none; }
    button { cursor:pointer; transition:.16s ease; }
    button:hover, a.btn:hover { background:#102238; border-color:rgba(47,155,255,.45); transform:translateY(-1px); }
    input { min-width:180px; }
    select:focus,input:focus,textarea:focus { border-color:var(--accent); box-shadow:0 0 0 3px rgba(47,155,255,.14); }
    .section { background:linear-gradient(180deg,rgba(15,28,45,.92),rgba(8,18,31,.92)); border:1px solid var(--line); border-radius:16px; overflow:hidden; margin-bottom:16px; box-shadow:var(--shadow); }
    .section .head { padding:16px 18px; border-bottom:1px solid var(--line); display:flex; justify-content:space-between; align-items:center; gap:10px; flex-wrap:wrap; }
    .content { padding:16px 18px; }
    .scroll { max-height:560px; overflow:auto; }
    table { width:100%; border-collapse:separate; border-spacing:0; font-size:13px; }
    th, td { border-bottom:1px solid var(--line); padding:12px 14px; text-align:left; vertical-align:middle; }
    th { color:#a8b3c4; font-weight:650; position:sticky; top:0; background:#0b1625; z-index:2; }
    tr:hover td { background:rgba(47,155,255,.045); }
    .pill { display:inline-flex; align-items:center; gap:6px; padding:3px 9px; border-radius:999px; border:1px solid var(--line); font-size:12px; white-space:nowrap; background:rgba(255,255,255,.03); }
    .pill.online, .pill.write_success, .pill.device_write_success { color:var(--ok); border-color:rgba(34,197,94,.5); }
    .pill.error, .pill.failed, .pill.write_failed, .pill.device_write_failed { color:var(--bad); border-color:rgba(239,68,68,.5); }
    .pill.connecting, .pill.executing, .pill.queued_to_device_worker { color:var(--warn); border-color:rgba(245,158,11,.5); }
    .pill.offline, .pill.stopped { color:var(--muted); }
    code, pre { color:#93c5fd; }
    pre { background:#020617; padding:10px; border-radius:10px; overflow:auto; max-height:320px; }
    .grid2 { display:grid; grid-template-columns: minmax(0,1.2fr) minmax(320px,.8fr); gap:16px; }
    @media (max-width: 900px) { .shell { grid-template-columns: 1fr; } nav { position:static; height:auto; border-right:0; border-bottom:1px solid var(--line); display:flex; flex-wrap:wrap; gap:8px; } nav button { width:auto; margin:0; } .grid2 { grid-template-columns:1fr; } }
  
    .formgrid{display:grid; grid-template-columns:minmax(130px,190px) minmax(240px,1fr); gap:10px 14px; align-items:center}
    .formgrid label{margin:0; color:var(--muted); font-size:12px}
    .wide-form input,.wide-form select,.wide-form textarea{width:100%; min-width:220px; box-sizing:border-box}
    .nested-form{grid-column:1 / -1; grid-template-columns:minmax(130px,190px) minmax(240px,1fr); padding:8px; border:1px solid var(--border); border-radius:10px; background:rgba(255,255,255,.02)}
    .register-debug-layout{grid-template-columns:minmax(480px,1fr) minmax(420px,0.9fr)}
    select[multiple]{min-height:112px}

    .binding-table select { width:100%; min-width:240px; min-height:42px; background:#081422; }
    .binding-table select[multiple] { min-height:46px; max-height:96px; }
    .binding-table .power-map-code { color:var(--accent2); font-weight:700; cursor:pointer; display:block; max-width:360px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .row-actions { display:flex; gap:6px; flex-wrap:wrap; margin-top:8px; }
    .row-actions button { padding:6px 9px; font-size:12px; }
    .danger { color:var(--bad); border-color:rgba(255,77,94,.72)!important; background:rgba(255,77,94,.06)!important; }
    .add-cluster-box { margin-top:14px; padding:14px; border:1px dashed var(--line2); border-radius:14px; display:flex; gap:10px; align-items:center; justify-content:center; background:rgba(3,10,20,.38); }
    .add-cluster-box input { flex:1; max-width:520px; }
    .available-box { margin-top:14px; display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:12px; padding:14px; border:1px solid var(--line); border-radius:14px; background:rgba(47,155,255,.06); }
    .section > section { padding:0; }
    .pm-helper { display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:12px; margin-bottom:14px; }
    .pm-helper .mini { border:1px solid var(--line); border-radius:12px; padding:12px; background:rgba(47,155,255,.055); }
    .pm-weight { width:110px; min-width:90px; text-align:right; }
    .pm-row-sum.oksum { color:var(--ok); }
    .pm-row-sum.badsum { color:var(--warn); }
    .pm-editor-table th:first-child, .pm-editor-table td:first-child { position:sticky; left:0; background:#0b1625; z-index:1; }
    .pm-editor-table td:first-child { background:#081422; }

    tr.selected-row { outline:1px solid rgba(47,155,255,.75); background:rgba(47,155,255,.16)!important; box-shadow:inset 3px 0 0 var(--accent); }
    tr.clickable-row { cursor:pointer; }
    tr.clickable-row:hover { background:rgba(47,155,255,.10); }
    .subtabs{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 14px 0;padding:8px;border:1px solid var(--line);border-radius:14px;background:rgba(2,6,23,.38)}
    .subtabs button{padding:8px 12px;border-radius:10px;background:rgba(15,23,42,.75)}
    .subtabs button.active{background:linear-gradient(135deg,rgba(47,155,255,.22),rgba(34,211,238,.14));border-color:rgba(47,155,255,.65);color:#dbeafe}
    .subpage{display:none}.subpage.active{display:block}
    .file-chip{display:inline-flex;align-items:center;gap:6px;max-width:100%;padding:6px 9px;border:1px solid var(--line);border-radius:999px;background:rgba(47,155,255,.07);font-size:12px;color:var(--muted)}
    .file-chip b{color:var(--text);font-weight:600}.file-chip code{max-width:420px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;display:inline-block}
    .analysis-file-pick{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:8px;align-items:end}
    #page-analyzer .content{padding:20px 22px}
    #page-analyzer .analysis-card{padding:18px; margin-bottom:16px}
    #page-analyzer .analysis-card .head{padding:0 0 14px 0; border-bottom:1px solid var(--line); margin-bottom:14px}
    #page-analyzer .form-stack{gap:14px; max-width:1100px}
    #page-analyzer .form-stack label{font-size:13px; line-height:1.45}
    #page-analyzer .form-stack input,#page-analyzer .form-stack select{min-height:42px}
    #page-analyzer .file-chip{margin:4px 0 6px; padding:8px 11px}
    .inline-upload-strip{display:grid;grid-template-columns:minmax(240px,1fr) auto;gap:10px;align-items:end;padding:12px;border:1px dashed var(--line2);border-radius:13px;background:rgba(47,155,255,.055);margin:6px 0 10px}
    .inline-upload-strip input[type=file]{width:100%;background:#020617;border:1px dashed #334155;border-radius:10px;padding:9px;color:var(--muted)}
    .curve-upload-strip{display:grid;grid-template-columns:minmax(240px,1fr) auto auto;gap:10px;align-items:center;margin:0 0 10px}
    .curve-upload-strip input[type=file]{width:100%;background:#020617;border:1px dashed #334155;border-radius:10px;padding:9px;color:var(--muted)}
    @media(max-width:900px){.inline-upload-strip,.curve-upload-strip{grid-template-columns:1fr}}
  </style>
</head>
<body>
  <header>
    <div class="brand"><div class="logo"></div><div><h1>ESS-AIO</h1><div class="muted" id="subtitle">Connecting...</div></div></div>
    <div class="topnav">
      <button data-page="project" onclick="showPage('project')">Project</button>
      <button data-page="devices" onclick="showPage('devices')">Devices</button>
      <button data-page="clusters" onclick="showPage('clusters')">Clusters</button>
      <button data-page="strategy" onclick="showPage('strategy')">Strategy</button>
      <button data-page="alarmcenter" onclick="showPage('alarmcenter')">Alarms</button>
      <button data-page="curves" onclick="showPage('curves')">Data</button>
      <button data-page="settings" onclick="showPage('settings')">System</button>
    </div>
    <div class="toolbar">
      <span class="pill online">● Runtime</span>
      <button onclick="refreshNow(true)">Refresh</button>
      <label class="muted"><input type="checkbox" id="auto" checked style="min-width:0" /> Auto</label>
      <a class="btn" href="/docs" target="_blank">API</a>
    </div>
  </header>
  <div class="shell">
    <nav>
      <div class="nav-section">Overview</div>
      <button data-page="runtimecenter" class="active nav-item" onclick="showPage('runtimecenter')"><span class="nav-icon">⌂</span><span>Runtime Center</span></button>
      <button data-page="overview" class="nav-item" onclick="showPage('overview')"><span class="nav-icon">▦</span><span>Overview</span></button>
      <div class="nav-section">Project</div>
      <button data-page="project" class="nav-item" onclick="showPage('project')"><span class="nav-icon">▣</span><span>Project Detail</span></button>
      <div class="nav-sub">
        <button data-page="project" onclick="showPage('project')">Cluster Binding</button>
        <button data-page="clusters" onclick="showPage('clusters')">Power Map</button>
        <button data-page="site" onclick="showPage('site')">Site Config</button>
      </div>
      <div class="nav-section">Devices</div>
      <button data-page="devices" class="nav-item" onclick="showPage('devices')"><span class="nav-icon">▤</span><span>Device Status</span></button>
      <button data-page="ops" class="nav-item" onclick="showPage('ops')"><span class="nav-icon">▧</span><span>BMS Control</span></button>
      <button data-page="pcs" class="nav-item" onclick="showPage('pcs')"><span class="nav-icon">◈</span><span>PCS Control</span></button>
      <button data-page="registerdebug" class="nav-item" onclick="showPage('registerdebug')"><span class="nav-icon">⌗</span><span>Register Debug</span></button>
      <div class="nav-section">Runtime</div>
      <button data-page="strategy" class="nav-item" onclick="showPage('strategy')"><span class="nav-icon">✦</span><span>Strategy</span></button>
      <button data-page="commands" class="nav-item" onclick="showPage('commands')"><span class="nav-icon">⌁</span><span>Commands</span></button>
      <button data-page="health" class="nav-item" onclick="showPage('health')"><span class="nav-icon">●</span><span>Health Monitor</span></button>
      <button data-page="runtime" class="nav-item" onclick="showPage('runtime')"><span class="nav-icon">⏻</span><span>Runtime Lifecycle</span></button>
      <div class="nav-section">Data</div>
      <button data-page="curves" class="nav-item" onclick="showPage('curves')"><span class="nav-icon">⌁</span><span>Curves</span></button>
      <button data-page="alarmcenter" class="nav-item" onclick="showPage('alarmcenter')"><span class="nav-icon">⚠</span><span>Alarm Center</span></button>
      <button data-page="alarms" class="nav-item" onclick="showPage('alarms')"><span class="nav-icon">△</span><span>BMS Alarms</span></button>
      <button data-page="analyzer" class="nav-item" onclick="showPage('analyzer')"><span class="nav-icon">◇</span><span>Analyzer</span></button>
      <div class="nav-sub">
        <button data-page="analyzer" onclick="showPage('analyzer'); showAnalyzerTab('upload')">Upload</button>
        <button data-page="analyzer" onclick="showPage('analyzer'); showAnalyzerTab('modbus')">Modbus</button>
        <button data-page="analyzer" onclick="showPage('analyzer'); showAnalyzerTab('joint')">Joint</button>
        <button data-page="analyzer" onclick="showPage('analyzer'); showAnalyzerTab('history')">History</button>
      </div>
      <button data-page="release" class="nav-item" onclick="showPage('release')"><span class="nav-icon">⇩</span><span>Release</span></button>
      <div class="nav-section">System</div>
      <button data-page="settings" class="nav-item" onclick="showPage('settings')"><span class="nav-icon">⚙</span><span>Settings</span></button>
      <button data-page="parity" class="nav-item" onclick="showPage('parity')"><span class="nav-icon">✓</span><span>UI-Web Parity</span></button>
      <button data-page="uiactions" class="nav-item" onclick="showPage('uiactions')"><span class="nav-icon">☰</span><span>UI Buttons</span></button>
      <button data-page="lts" class="nav-item" onclick="showPage('lts')"><span class="nav-icon">★</span><span>9.x LTS</span></button>
    </nav>
    <main>
      <section id="page-runtimecenter" data-title="Runtime Center" class="page active">
        <section class="section"><div class="head"><h2>Site Runtime Center</h2><span class="muted">Main Web EMS entry. Site-level state, fleet health, strategy state, and recent command result.</span></div>
          <div class="content">
            <div id="runtimeCenterCards" class="cards"></div>
            <div class="toolbar" style="margin-bottom:12px; flex-wrap:wrap">
              <button onclick="bmsStartAll()">Start All BMS</button>
              <button onclick="pcsConnectAll()">Connect All PCS</button>
              <button onclick="strategyStartAll()">Start All Strategies</button>
              <button onclick="strategyStopAll()">Stop All Strategies</button>
              <button onclick="pcsFleetCommand('stop')">Fleet Stop</button>
              <button onclick="loadRuntimeCenter()">Refresh Runtime Center</button>
            </div>
            <div class="grid2">
              <section class="section"><div class="head"><h2>Fleet / Cluster Runtime</h2></div><div class="scroll"><table><thead><tr><th>Cluster</th><th>BMS</th><th>PCS</th><th>Allocation</th><th>Running</th></tr></thead><tbody id="runtimeCenterClusters"></tbody></table></div></section>
              <section class="section"><div class="head"><h2>Recent Runtime Commands</h2></div><div class="content"><pre id="runtimeCenterCommands">-</pre></div></section>
            </div>
            <pre id="runtimeCenterRaw">-</pre>
          </div>
        </section>
      </section>
      <section id="page-overview" data-title="Overview" class="page">
        <div class="cards" id="cards"></div>
        <section class="section"><div class="head"><h2>Site Dashboard</h2><span class="muted">Runtime, device health, PCS/BMS summary and active alarm overview.</span></div><div class="content"><div id="overviewDashboard" class="status-strip"></div></div></section>
        <div class="grid2">
          <section class="section"><div class="head"><h2>Active Alarms / Issues</h2><button onclick="showPage('alarmcenter')">Open Alarm Center</button></div><div class="scroll"><table><thead><tr><th>Severity</th><th>Area</th><th>Device</th><th>Message</th></tr></thead><tbody id="overviewAlarmRows"></tbody></table></div></section>
          <section class="section"><div class="head"><h2>Runtime Summary</h2></div><div class="content"><pre id="runtimeSummary">-</pre></div></section>
        </div>
        <section class="section"><div class="head"><h2>Overview Monitoring Curves</h2><span class="muted">Compact trend preview: BMS status and online rack count.</span></div><div class="content"><div class="dashboard-charts" id="overviewTrendCards"></div></div></section>
        <section class="section"><div class="head"><h2>Soak Test</h2><a class="btn" href="/api/soak/status" target="_blank">Open JSON</a></div><div class="content"><pre id="soakSummary">-</pre></div></section>
      </section>
      <section id="page-devices" data-title="Device Status" class="page">
        <section class="section"><div class="head"><h2>Device Status</h2><span class="muted">Read-only status list. Connect/control actions live in BMS Control and PCS Control.</span><div class="toolbar"><input id="deviceFilter" placeholder="Filter device..." oninput="renderDevices()" /><select id="typeFilter" onchange="renderDevices()"><option value="all">All</option><option value="BMS">BMS</option><option value="PCS">PCS</option></select><span class="muted" id="deviceCount"></span></div></div><div class="scroll"><table><thead><tr><th>Type</th><th>Name</th><th>Connection</th><th>Status</th><th>Errors</th><th>Latency</th><th>Last message</th></tr></thead><tbody id="devices"></tbody></table></div></section>
        <section class="section"><div class="head"><h2>Selected Device Snapshot</h2><span class="muted" id="selectedDeviceTitle">Click a device row</span></div><div class="content"><pre id="deviceSnapshot">-</pre></div></section>
      </section>

      <section id="page-project" data-title="Project Detail" class="page">
        <section class="section"><div class="head"><h2>Project / Device Config</h2><span class="muted">Add/update only changes config. It will not auto-connect BMS/PCS.</span></div>
          <div class="content">
            <div class="toolbar" style="margin-bottom:12px; flex-wrap:wrap">
              <button onclick="loadProjectConfig()">Load Project Config</button>
              <button onclick="downloadProjectConfig()">Download Project JSON</button>
              <button onclick="validateProjectConfig()">Validate Project</button>
            </div>
            <div class="quick-add-grid">
              <section class="quick-add-card">
                <h2>BMS Quick Add / Update</h2>
                <div class="toolbar" style="margin:8px 0; flex-wrap:wrap">
                  <input id="bmsCfgName" placeholder="BMS name e.g. BESS1" />
                  <input id="bmsCfgHost" placeholder="Host/IP" />
                  <input id="bmsCfgPort" type="number" value="502" placeholder="Port" />
                  <input id="bmsCfgUnit" type="number" value="1" placeholder="Unit ID" />
                  <input id="bmsCfgInterval" type="number" step="0.5" value="2" placeholder="Interval s" />
                  <select id="bmsCfgProfile"><option value="catl_v22">catl_v22</option></select>
                  <button onclick="saveBmsConfig()">Save BMS</button>
                  <button onclick="removeBmsConfig()">Remove BMS</button>
                </div>
              </section>
              <section class="quick-add-card">
                <h2>PCS Quick Add / Update</h2>
                <div class="toolbar" style="margin:8px 0; flex-wrap:wrap">
                  <input id="pcsCfgName" placeholder="PCS name e.g. PCS-1" />
                  <input id="pcsCfgHost" placeholder="Host/IP" />
                  <input id="pcsCfgPort" type="number" value="502" placeholder="Port" />
                  <input id="pcsCfgUnit" type="number" value="1" placeholder="Unit ID" />
                  <select id="pcsCfgProfile"><option value="kehua_bcs1250">kehua_bcs1250</option></select>
                  <button onclick="savePcsConfig()">Save PCS</button>
                  <button onclick="removePcsConfig()">Remove PCS</button>
                </div>
              </section>
            </div>
            <div id="projectConfigResult" class="muted" style="margin:8px 0">No project config loaded.</div>
            <pre id="projectValidationBox">Validation result will appear here.</pre>
          </div>
        </section>
        <section class="section"><div class="head"><h2>BMS Configs</h2><span id="projectBmsCount" class="muted"></span></div><div class="scroll"><table><thead><tr><th>Name</th><th>Host</th><th>Port</th><th>Unit</th><th>Interval</th><th>Profile</th><th>Action</th></tr></thead><tbody id="projectBmsRows"></tbody></table></div></section>
        <section class="section"><div class="head"><h2>PCS Configs</h2><span id="projectPcsCount" class="muted"></span></div><div class="scroll"><table><thead><tr><th>Name</th><th>Host</th><th>Port</th><th>Unit</th><th>Profile</th><th>Enabled</th><th>Action</th></tr></thead><tbody id="projectPcsRows"></tbody></table></div></section>
        <section class="section"><div class="head"><div><h2>Cluster Binding</h2><div class="muted">Bind added BMS and PCS devices to each cluster. Configuration only; no device connection is started here.</div></div><span id="projectClusterCount" class="muted"></span></div><div class="content">
          <div class="scroll"><table class="binding-table"><thead><tr><th>Cluster</th><th>BMS</th><th>PCS</th><th>Power Map</th><th>Action</th></tr></thead><tbody id="projectClusterRows"></tbody></table></div>
          <div class="add-cluster-box"><input id="newClusterName" placeholder="New cluster name e.g. Cluster-1-5" /><button onclick="addClusterBindingRow()">＋ Add Cluster</button></div>
          <div class="available-box"><div><b>Available BMS:</b> <span id="availableBmsCount">0</span></div><div><b>Available PCS:</b> <span id="availablePcsCount">0</span></div><div class="muted">Devices must be added in Project first. Connect/Disconnect is intentionally kept in BMS Control / PCS Control.</div></div>
        </div></section>
        <section class="section"><div class="head"><h2>Raw Project Config</h2></div><div class="content"><pre id="projectConfigRaw">-</pre></div></section>
      </section>
      <section id="page-ops" data-title="BMS Control" class="page">
        <section class="section"><div class="head"><h2>BMS Control</h2><span class="muted">Full Web migration of PySide BMS Control: polling, HV workflow, EMS command, RTC, register writes, heartbeat and 038B periodic task.</span></div>
          <div class="content">
            <div class="toolbar" style="margin-bottom:12px; flex-wrap:wrap">
              <label>Target</label><select id="bmsControlScope"><option value="single">Selected BMS</option><option value="all_online">All online/running BMS</option></select>
              <select id="opsBmsDevice"></select>
              <button onclick="bmsSingle('start')">Start Selected BMS</button>
              <button onclick="bmsSingle('stop')">Stop Selected BMS</button>
              <button onclick="bmsStartAll()">Start All BMS</button>
              <button onclick="bmsStopAll()">Stop All BMS</button>
            </div>
            <div class="cards" id="bmsControlCards"></div>
          </div>
        </section>
        <section class="grid2">
          <section class="section"><div class="head"><h2>HV Workflow</h2><span class="muted">PySide-style HV workflow controls. Single-device and all-online buttons are exposed separately so现场操作不用切 scope 才能找到。</span></div><div class="content">
            <div class="toolbar" style="margin-bottom:10px; flex-wrap:wrap">
              <input id="bmsHvTimeout" type="number" value="30" step="1" placeholder="Timeout s" title="Each HV workflow step timeout in seconds" />
              <input id="bmsHvPoll" type="number" value="1" step="0.5" placeholder="Poll s" title="HV readiness polling interval" />
              <label class="muted" title="Current runtime HV command reuses the BMS worker queue and does not execute PCS actions. Keep this checked for BMS-only commissioning."><input id="bmsHvIgnorePcsPrecheck" type="checkbox" checked style="min-width:0" /> Ignore PCS precheck / BMS-only</label>
            </div>
            <div class="toolbar" style="margin-bottom:10px; flex-wrap:wrap">
              <button onclick="bmsHv('on')">Selected BMS HV ON Workflow</button>
              <button onclick="bmsHv('off')">Selected BMS HV OFF Workflow</button>
              <button onclick="bmsHvAll('on')">HV ON All Online</button>
              <button onclick="bmsHvAll('off')">HV OFF All Online</button>
            </div>
            <p class="muted">Single workflow uses the selected BMS above. All-online workflow queues only BMS workers that are already running/online, so it will not probe offline devices or start extra reconnect loops. High-risk commands use browser confirmation and are written to command audit.</p>
          </div></section>
          <section class="section"><div class="head"><h2>Quick EMS Commands</h2><span class="muted">Common 0x0381 / clear-fault style controls.</span></div><div class="content">
            <div class="toolbar" style="margin-bottom:10px; flex-wrap:wrap">
              <button onclick="bmsQuickCommand('stay')">0381 Stay / Hold</button>
              <button onclick="bmsQuickCommand('power_on')">0381 Power On</button>
              <button onclick="bmsQuickCommand('power_off')">0381 Power Off</button>
              <button onclick="bmsQuickCommand('clear_fault')">Clear Fault</button>
              <button onclick="bmsClearFaultAll()">Clear Fault All Online</button>
              <button onclick="bmsQuickCommand('write_038b')">Write 038B=2</button>
            </div>
            <div class="toolbar" style="margin-bottom:10px; flex-wrap:wrap">
              <button onclick="bmsHeartbeat(true)">Start Heartbeat All</button>
              <button onclick="bmsHeartbeat(false)">Stop Heartbeat All</button>
              <button onclick="bms038b(true)">Start 038B Cycle</button>
              <button onclick="bms038b(false)">Stop 038B Cycle</button>
            </div>
            <div id="bmsHeartbeatStatus" class="command-feedback">Heartbeat idle. Start heartbeat to send EMS heartbeat periodically.</div>
          </div></section>
        </section>
        <section class="section"><div class="head"><h2>BMS Live Devices</h2><span class="muted" id="bmsControlCount"></span></div><div class="scroll"><table><thead><tr><th>Name</th><th>Connection</th><th>SOC</th><th>Voltage</th><th>Current</th><th>Power</th><th>Status</th><th>Online Racks</th><th>Updated</th><th>Last Message</th><th>Action</th></tr></thead><tbody id="bmsControlRows"></tbody></table></div></section>
        <section class="section"><div class="head"><h2>Rack / SBMU Monitor</h2><span class="muted">Selected BMS rack monitor. Enable/Disable selections are staged first, then written once as 0x038D/0x038E/0x038F bitmasks.</span></div>
          <div class="content">
            <div class="toolbar" style="margin-bottom:10px; flex-wrap:wrap">
              <label>SBMU / Rack count</label><input id="rackSbmuCount" type="number" min="1" max="48" value="16" />
              <button onclick="readRackSbmu()">Refresh Rack/SBMU</button>
              <button onclick="previewRackMask()">Preview Mask</button>
              <button onclick="applyRackMask()">Apply Enable/Disable</button>
            </div>
            <div id="rackMaskPreview" class="command-feedback">No rack changes selected.</div>
            <div class="scroll"><table><thead><tr><th>Rack</th><th>Online</th><th>SOC</th><th>V outside</th><th>V inside</th><th>Current</th><th>Power</th><th>Cell Sum</th><th>Ready</th><th>Relay +/-</th><th>Temp max/min</th><th>Action</th></tr></thead><tbody id="rackSbmuRows"><tr><td colspan="12" class="muted">Select a BMS and refresh.</td></tr></tbody></table></div>
            <pre id="rackSbmuRaw" style="max-height:260px;overflow:auto">No rack data.</pre>
          </div>
        </section>
        <section class="section"><div class="head"><h2>BMS Version Read</h2><span class="muted">Read MBMU, ETH and selected SBMU version blocks. SBMU01 uses the configured base address; SBMU02+ are base + 0x400 each.</span></div>
          <div class="content">
            <div class="toolbar" style="margin-bottom:10px; flex-wrap:wrap">
              <input id="bmsVersionSbmuCount" type="number" min="0" max="32" value="1" placeholder="SBMU count" />
              <button onclick="bmsReadVersion()">Read Selected BMS Version</button>
            </div>
            <pre id="bmsVersionResult">No version read yet.</pre>
            <details><summary class="muted">Advanced manual register write</summary>
              <div class="toolbar" style="margin:10px 0; flex-wrap:wrap">
                <select id="bmsWriteScope"><option value="single">selected BMS</option><option value="all_online">all online BMS</option></select>
                <input id="bmsWriteAddress" value="0x038B" placeholder="Address e.g. 0x038B" />
                <input id="bmsWriteValue" type="number" value="2" placeholder="Value" />
                <button onclick="bmsRegisterWrite()">Manual Write</button>
              </div>
            </details>
            <table style="display:none"><tbody id="bmsPresetRows"></tbody></table>
          </div>
        </section>
        <section class="section"><div class="head"><h2>RTC Write</h2><span class="muted">Writes 0x0382~0x0387 as year/month/day/hour/minute/second.</span></div><div class="content">
          <div class="toolbar" style="margin-bottom:6px; flex-wrap:wrap">
            <input id="rtcYear" type="number" value="2026" placeholder="Year" />
            <input id="rtcMonth" type="number" value="1" placeholder="Month" />
            <input id="rtcDay" type="number" value="1" placeholder="Day" />
            <input id="rtcHour" type="number" value="0" placeholder="Hour" />
            <input id="rtcMinute" type="number" value="0" placeholder="Minute" />
            <input id="rtcSecond" type="number" value="0" placeholder="Second" />
            <button onclick="fillRtcNow()">Use Browser Time</button>
            <button onclick="bmsRtcWrite()">Write RTC</button>
          </div>
        </div></section>
        <section class="section"><div class="head"><h2>CSV / Soak / Operation Log</h2><span class="muted">Kept here because these were originally operated from the control UI.</span></div><div class="content">
          <div class="toolbar" style="margin-bottom:12px; flex-wrap:wrap">
            <button onclick="csvBms(true)">Start BMS CSV</button><button onclick="csvBms(false)">Stop BMS CSV</button><button onclick="csvPcs(true)">Start PCS CSV</button><button onclick="csvPcs(false)">Stop PCS CSV</button><button onclick="loadCsvStatus()">Refresh CSV</button>
            <input id="soakLabel" value="field-soak" placeholder="Soak label" /><input id="soakInterval" type="number" min="10" step="10" value="60" placeholder="Interval seconds" /><button onclick="soakStart()">Start Soak</button><button onclick="soakStop()">Stop Soak</button><button onclick="soakReport()">Load Soak</button><button onclick="loadOperationLog()">Refresh Log</button>
          </div>
          <div id="opsCommandResult" class="muted">No command sent.</div><pre id="opsCsvStatus">-</pre><pre id="soakOpsBox">-</pre><pre id="operationLogBox">-</pre>
        </div></section>
      </section>
      <section id="page-pcs" data-title="PCS Control" class="page">
        <section class="section"><div class="head"><h2>PCS Control</h2><span class="muted">Full Web migration of PySide PCS Control: connect, start/stop, DC breaker, active/reactive power, PF/custom command and fleet dispatch.</span></div>
          <div class="content">
            <div class="toolbar" style="margin-bottom:12px; flex-wrap:wrap">
              <label>Selected PCS</label><select id="pcsSelected"></select>
              <button onclick="populatePcsSelected()">Refresh PCS List</button>
              <button onclick="pcsSingleSelected('connect')">Connect</button>
              <button onclick="pcsSingleSelected('stop')">Disconnect Comm</button>
              <button onclick="pcsOneCommandSelected('start')">PCS Start Command</button>
              <button onclick="pcsOneCommandSelected('stop')">PCS Stop Command</button>
              <button onclick="pcsOneCommandSelected('standby')">Standby</button>
            </div>
            <div class="cards" id="pcsControlCards"></div>
            <div id="pcsCommandResult" class="muted">No command sent.</div>
          </div>
        </section>
        <section class="grid2">
          <section class="section"><div class="head"><h2>Selected PCS Manual Control</h2><span class="muted">High-risk commands require browser confirmation.</span></div><div class="content">
            <div class="toolbar" style="margin-bottom:10px; flex-wrap:wrap">
              <button onclick="pcsOneCommandSelected('close_dc_breaker')">Close DC Breaker</button>
              <button onclick="pcsOneCommandSelected('open_dc_breaker')">Open DC Breaker</button>
              <button onclick="pcsOneCommandSelected('clear_fault')">Clear Fault</button>
              <button onclick="pcsOneCommandSelected('reset')">Reset</button>
            </div>
            <div class="toolbar" style="margin-bottom:10px; flex-wrap:wrap">
              <input id="pcsActivePower" type="number" step="0.1" value="0" placeholder="Active kW" />
              <button onclick="pcsPowerSelected('set_active_power','pcsActivePower')">Set Active</button>
              <input id="pcsReactivePower" type="number" step="0.1" value="0" placeholder="Reactive kvar" />
              <button onclick="pcsPowerSelected('set_reactive_power','pcsReactivePower')">Set Reactive</button>
              <input id="pcsPfValue" type="number" step="0.01" value="1.00" placeholder="PF" />
              <button onclick="pcsPowerSelected('set_power_factor','pcsPfValue')">Set PF</button>
            </div>
            <div class="toolbar" style="margin-bottom:10px; flex-wrap:wrap">
              <input id="pcsCustomMethod" placeholder="custom method from PCS profile" />
              <input id="pcsCustomValue" type="number" step="0.1" placeholder="optional value" />
              <button onclick="pcsCustomSelected()">Send Custom</button>
            </div>
          </div></section>
          <section class="section"><div class="head"><h2>Fleet PCS Control</h2><span class="muted">Broadcast to all configured/running PCS workers.</span></div><div class="content">
            <div class="toolbar" style="margin-bottom:12px; flex-wrap:wrap">
              <button onclick="pcsConnectAll()">Connect All PCS</button>
              <button onclick="pcsStopAll()">Disconnect All PCS</button>
              <button onclick="pcsFleetCommand('start')">Fleet PCS Start</button>
              <button onclick="pcsFleetCommand('stop')">Fleet PCS Stop</button>
              <button onclick="pcsFleetCommand('standby')">Fleet Standby</button>
            </div>
            <div class="toolbar" style="margin-bottom:12px; flex-wrap:wrap">
              <input id="fleetActivePower" type="number" step="0.1" value="0" placeholder="Active kW" />
              <button onclick="pcsFleetPower('set_active_power','fleetActivePower')">Fleet Set Active</button>
              <input id="fleetReactivePower" type="number" step="0.1" value="0" placeholder="Reactive kvar" />
              <button onclick="pcsFleetPower('set_reactive_power','fleetReactivePower')">Fleet Set Reactive</button>
              <input id="fleetPfValue" type="number" step="0.01" value="1.00" placeholder="PF" />
              <button onclick="pcsFleetPower('set_power_factor','fleetPfValue')">Fleet Set PF</button>
            </div>
          </div></section>
        </section>
        <section class="section"><div class="head"><h2>PCS Devices</h2><span class="muted" id="pcsCount"></span></div><div class="scroll"><table><thead><tr><th>Name</th><th>Connection</th><th>Status</th><th>AC</th><th>DC</th><th>Run</th><th>Active P</th><th>Reactive Q</th><th>Last message</th><th>Action</th></tr></thead><tbody id="pcsRows"></tbody></table></div></section>
      </section>
      
        <section class="section"><div class="head"><h2>PCS Alarms / Faults</h2><span class="muted">Derived from PCS runtime state, latest snapshot and Alarm Center.</span></div><div class="scroll"><table><thead><tr><th>PCS</th><th>Severity</th><th>Status</th><th>Message</th></tr></thead><tbody id="pcsAlarmRows"></tbody></table></div></section><section id="page-strategy" data-title="Strategy Center" class="page">
        <section class="section"><div class="head"><h2>Strategy Center</h2><span class="muted">Profile strategy, cluster runtime, safety gates and recent strategy commands.</span><div class="toolbar"><button onclick="loadStrategyCenter()">Refresh Strategy Center</button><a class="btn" href="/api/strategy-center" target="_blank">JSON</a></div></div>
          <div class="cards" id="strategyCards"></div>
          <div class="content"><pre id="strategyIssuesBox">-</pre></div>
        </section>
        <section class="section"><div class="head"><h2>Cluster Strategy Control</h2><span class="muted">Start/stop and target power are sent to Runtime.</span></div>
          <div class="content">
            <div class="toolbar" style="margin-bottom:12px; flex-wrap:wrap">
              <select id="strategyCluster"></select>
              <select id="strategyMode"><option value="charge">charge</option><option value="discharge">discharge</option></select>
              <input id="strategyTargetPower" type="number" step="0.1" value="0" placeholder="Target kW" />
              <input id="strategyRampStep" type="number" step="0.1" value="50" placeholder="Ramp step kW" />
              <input id="strategyRampInterval" type="number" step="0.5" value="5" placeholder="Ramp interval s" />
              <input id="strategyTimeout" type="number" step="0.5" value="5" placeholder="BMS timeout s" />
              <button onclick="strategyApplySettings()">Apply Settings</button>
              <button onclick="strategyStart()">Start Strategy</button>
              <button onclick="strategyStop()">Stop Strategy</button>
              <button onclick="strategyStartAll()">Start All Strategies</button>
              <button onclick="strategyStopAll()">Stop All Strategies</button>
            </div>
            <div id="strategyCommandResult" class="muted">No command sent.</div>
          </div>
        </section>
        <section class="section"><div class="head"><h2>Profile Strategy Config</h2><span class="muted">Edits strategy.json only. It does not auto-start any strategy.</span><div class="toolbar"><button onclick="loadStrategyConfig()">Load</button><button onclick="saveStrategyConfig()">Save</button></div></div><div class="content"><textarea id="strategyConfigEditor" style="width:100%; min-height:260px; background:#020617; color:var(--text); border:1px solid var(--line); border-radius:10px; padding:10px; font-family:ui-monospace, SFMono-Regular, Menlo, monospace;"></textarea><div id="strategyConfigResult" class="muted">No config loaded.</div></div></section>
        <section class="section"><div class="head"><h2>Strategy Runtime</h2></div><div class="scroll"><table><thead><tr><th>Cluster</th><th>Health</th><th>BMS</th><th>PCS</th><th>Mode</th><th>Power Map</th><th>Running</th></tr></thead><tbody id="strategyRows"></tbody></table></div></section>
        <section class="section"><div class="head"><h2>Recent Strategy Commands</h2></div><div class="scroll"><table><thead><tr><th>Time</th><th>Name</th><th>Risk</th><th>Status</th><th>OK</th><th>Message</th></tr></thead><tbody id="strategyCommandRows"></tbody></table></div></section>
      </section>
      <section id="page-analyzer" data-title="Diagnosis Center" class="page analyzer-page">
        <section class="section"><div class="head"><h2>Diagnosis Center</h2><span class="muted">Upload ASC / DBC / PCAP / PCAPNG / CSV / mapping.json explicitly. No default DBC is loaded.</span><div class="toolbar"><button onclick="loadAnalyzerFiles()">Refresh Files</button></div></div>
          <div class="content">
            <div class="subtabs" id="analyzerTabs">
              <button class="active" data-tab="upload" onclick="showAnalyzerTab('upload')">Upload & Session Files</button>
              <button data-tab="modbus" onclick="showAnalyzerTab('modbus')">Modbus Analysis</button>
              <button data-tab="can" onclick="showAnalyzerTab('can')">CAN Files</button>
              <button data-tab="joint" onclick="showAnalyzerTab('joint')">Joint Analysis</button>
              <button data-tab="history" onclick="showAnalyzerTab('history')">History / Export</button>
            </div>
            <div id="analyzerVisualSummary" class="analyzer-visual-grid"><div class="analysis-step"><h3>1 Upload</h3><div class="muted">Upload ASC, DBC, PCAP/PCAPNG, CSV and mapping.json explicitly.</div></div><div class="analysis-step"><h3>2 Select</h3><div class="muted">Analysis pages read the uploaded session files from dropdowns.</div></div><div class="analysis-step"><h3>3 Analyze</h3><div class="muted">Run Modbus, CAN or Joint analysis and review evidence/history.</div></div></div><div id="analyzer-upload" class="subpage active">
              <div class="analyzer-upload-grid">
                <div class="upload-card"><h3>ASC CAN Log</h3><input id="anAscFile" type="file" accept=".asc" /><button onclick="uploadAnalyzerFile('asc','anAscFile','anAscPath')">Upload ASC</button><div class="muted small">CAN log source.</div></div>
                <div class="upload-card"><h3>DBC</h3><input id="anDbcFile" type="file" accept=".dbc" /><button onclick="uploadAnalyzerFile('dbc','anDbcFile','anDbcPath')">Upload DBC</button><div class="muted small">Must be selected manually.</div></div>
                <div class="upload-card"><h3>Modbus Capture</h3><input id="anModbusFile" type="file" accept=".pcap,.pcapng,.csv" /><button onclick="uploadAnalyzerModbusFile()">Upload Capture</button><div class="muted small">pcap / pcapng / csv.</div></div>
                <div class="upload-card"><h3>mapping.json</h3><input id="anMappingFile" type="file" accept=".json" /><button onclick="uploadAnalyzerFile('mapping','anMappingFile','anMappingPath')">Upload Mapping</button><div class="muted small">CAN ↔ Modbus mapping.</div></div>
              </div>
              <div id="anUploadStatus" class="muted" style="margin-top:10px">No files uploaded in this session.</div>
              <section class="section" style="margin-top:14px"><div class="head"><h2>Session Files</h2><span class="muted">Click Use to fill analysis inputs. Files become visible in Analysis tabs immediately.</span></div>
                <div class="content scroll"><table><thead><tr><th>Kind</th><th>Name</th><th>Size</th><th>Modified</th><th>Action</th></tr></thead><tbody id="analyzerFileRows"><tr><td colspan="5" class="muted">No uploaded files loaded.</td></tr></tbody></table></div>
              </section>
            </div>
            <div id="analyzer-modbus" class="subpage">
              <section class="analysis-card">
                <div class="head"><h2>Modbus TCP Capture</h2><span class="muted">Select an uploaded pcap / pcapng / csv, or upload from the Upload tab first.</span></div>
                <div class="form-stack">
                  <div class="inline-upload-strip"><label>Upload capture here<input id="anModbusFileInline" type="file" accept=".pcap,.pcapng,.csv" /></label><button onclick="uploadAnalyzerModbusFileFrom('anModbusFileInline')">Upload & Select</button></div>
                  <label>Uploaded capture<select id="anModbusSelect" onchange="selectAnalyzerPath('modbus', this.value)"><option value="">No uploaded capture selected</option></select></label>
                  <label>Capture path<input id="anModbusPath" placeholder="Upload/select pcap / pcapng / csv path" /></label>
                  <div id="anModbusSelectedFile" class="file-chip"><span>No Modbus capture selected.</span></div>
                  <label>Timeout seconds<input id="anTimeout" type="number" step="0.1" value="2.0" /></label>
                  <button onclick="analyzeModbusCapture()">Analyze Modbus</button>
                </div>
                <pre id="anModbusResult">Upload or select a Modbus capture, then analyze.</pre>
              </section>
            </div>
            <div id="analyzer-can" class="subpage">
              <section class="analysis-card">
                <div class="head"><h2>CAN / DBC / Mapping Files</h2><span class="muted">No default DBC is loaded. Use uploaded files explicitly.</span></div>
                <div class="form-stack">
                  <div class="inline-upload-strip"><label>Upload ASC<input id="anAscFileInline" type="file" accept=".asc" /></label><button onclick="uploadAnalyzerFile('asc','anAscFileInline','anAscPath')">Upload ASC</button></div>
                  <div class="inline-upload-strip"><label>Upload DBC<input id="anDbcFileInline" type="file" accept=".dbc" /></label><button onclick="uploadAnalyzerFile('dbc','anDbcFileInline','anDbcPath')">Upload DBC</button></div>
                  <div class="inline-upload-strip"><label>Upload mapping.json<input id="anMappingFileInline" type="file" accept=".json" /></label><button onclick="uploadAnalyzerFile('mapping','anMappingFileInline','anMappingPath')">Upload Mapping</button></div>
                  <label>ASC file<select id="anAscSelect" onchange="selectAnalyzerPath('asc', this.value)"><option value="">No ASC selected</option></select></label>
                  <label>DBC file<select id="anDbcSelect" onchange="selectAnalyzerPath('dbc', this.value)"><option value="">No DBC selected</option></select></label>
                  <label>mapping.json<select id="anMappingSelect" onchange="selectAnalyzerPath('mapping', this.value)"><option value="">No mapping selected</option></select></label>
                  <label>ASC path<input id="anAscPath" placeholder="Upload/select ASC path" /></label>
                  <label>DBC path<input id="anDbcPath" placeholder="Upload/select DBC path; no default is loaded" /></label>
                  <label>mapping.json path<input id="anMappingPath" placeholder="Upload/select mapping.json path" /></label>
                  <div id="anCanSelectedFiles" class="muted small">No CAN-related files selected.</div>
                </div>
              </section>
            </div>
            <div id="analyzer-joint" class="subpage">
              <section class="analysis-card">
                <div class="head"><h2>CAN + Modbus Joint Check</h2><span class="muted">Requires ASC + Modbus capture + DBC + mapping.json. Nothing is auto-filled from bundled defaults.</span></div>
                <div class="form-stack">
                  <div class="inline-upload-strip"><label>Upload ASC<input id="anJointAscFileInline" type="file" accept=".asc" /></label><button onclick="uploadAnalyzerFile('asc','anJointAscFileInline','anAscPath')">Upload ASC</button></div>
                  <div class="inline-upload-strip"><label>Upload Modbus capture / CSV<input id="anJointModbusFileInline" type="file" accept=".pcap,.pcapng,.csv" /></label><button onclick="uploadAnalyzerModbusFileFrom('anJointModbusFileInline')">Upload Capture</button></div>
                  <div class="inline-upload-strip"><label>Upload DBC<input id="anJointDbcFileInline" type="file" accept=".dbc" /></label><button onclick="uploadAnalyzerFile('dbc','anJointDbcFileInline','anDbcPath')">Upload DBC</button></div>
                  <div class="inline-upload-strip"><label>Upload mapping.json<input id="anJointMappingFileInline" type="file" accept=".json" /></label><button onclick="uploadAnalyzerFile('mapping','anJointMappingFileInline','anMappingPath')">Upload Mapping</button></div>
                  <label>ASC<select id="anJointAscSelect" onchange="selectAnalyzerPath('asc', this.value)"><option value="">No ASC selected</option></select></label>
                  <label>Modbus capture / CSV<select id="anJointModbusSelect" onchange="selectAnalyzerPath('joint_modbus', this.value)"><option value="">No Modbus capture selected</option></select></label>
                  <label>DBC<select id="anJointDbcSelect" onchange="selectAnalyzerPath('dbc', this.value)"><option value="">No DBC selected</option></select></label>
                  <label>mapping.json<select id="anJointMappingSelect" onchange="selectAnalyzerPath('mapping', this.value)"><option value="">No mapping selected</option></select></label>
                  <label>Modbus path<input id="anJointModbusPath" placeholder="Upload/select Modbus capture path" /></label>
                  <label>Tolerance seconds<input id="anTolerance" type="number" step="0.1" value="0.5" /></label>
                  <button onclick="analyzeJoint()">Correlate</button>
                </div>
                <pre id="anJointResult">Upload and select ASC + Modbus capture + DBC + mapping.json first.</pre>
              </section>
            </div>
            <div id="analyzer-history" class="subpage">
              <section class="section"><div class="head"><h2>Diagnosis History</h2><div class="toolbar"><button onclick="loadDiagnosisHistory()">Refresh History</button><a class="btn" href="/api/diagnosis/history/export.csv" target="_blank">Export CSV</a><button onclick="clearDiagnosisHistory()">Clear</button></div></div>
                <div class="content scroll">
                  <table><thead><tr><th>Time</th><th>Type</th><th>Status</th><th>Count</th><th>Summary</th><th>Evidence</th></tr></thead><tbody id="diagnosisHistory"><tr><td colspan="6" class="muted">Run an analysis or click Refresh History.</td></tr></tbody></table>
                </div>
              </section>
            </div>
          </div>
        </section>
      </section>

      <section id="page-registerdebug" data-title="Register Debug" class="page">
        <section class="section"><div class="head"><h2>Register Debug</h2><span class="muted">Read raw registers from BMS/PCS. Strided mode is useful for reading the same SBMU offset every +0x400.</span><div class="toolbar"><button onclick="populateRegisterDevices()">Refresh Devices</button></div></div>
          <div class="content grid2 register-debug-layout">
            <div>
              <h3>Read Registers</h3>
              <div class="formgrid wide-form">
                <label>Device Type</label><select id="regDeviceType" onchange="populateRegisterDevices()"><option value="bms">BMS</option><option value="pcs">PCS</option></select>
                <label>Device</label><select id="regDevice"></select>
                <label>Register Type</label><select id="regType"><option value="holding">Holding</option><option value="input">Input</option></select>
                <label>Read Mode</label><select id="regReadMode" onchange="toggleRegisterMode()"><option value="continuous">Continuous</option><option value="strided">Strided + step</option></select>
                <label>Start / Base Address</label><input id="regStart" value="0x0400" />
                <div id="regContinuousFields" class="formgrid nested-form">
                  <label>Count</label><input id="regCount" type="number" min="1" max="125" value="8" />
                </div>
                <div id="regStridedFields" class="formgrid nested-form" style="display:none">
                  <label>Step</label><input id="regStep" value="0x400" />
                  <label>Quantity</label><input id="regQuantity" type="number" min="1" max="128" value="8" />
                  <label>Length per block</label><input id="regLength" type="number" min="1" max="32" value="1" />
                </div>
              </div>
              <div class="toolbar"><button onclick="registerRead()">Read</button><button onclick="registerLookup()">Lookup Address</button></div>
            </div>
            <div>
              <h3>Write Single BMS Register</h3>
              <p class="muted">Write is limited to BMS single-register write and is recorded in command audit. The browser confirmation dialog replaces manual EXECUTE typing.</p>
              <div class="formgrid wide-form">
                <label>BMS Device</label><select id="regWriteDevice"></select>
                <label>Address</label><input id="regWriteAddress" value="0x0381" />
                <label>Value</label><input id="regWriteValue" type="number" value="1" />
              </div>
              <button onclick="registerWriteBms()">Write Register</button>
              <pre id="regWriteResult">No write command.</pre>
            </div>
          </div>
        </section>
        <section class="section"><div class="head"><h2>Read Result</h2><div class="toolbar"><button onclick="copyRegisterResult()">Copy JSON</button></div></div><div class="scroll"><table><thead><tr><th>Address</th><th>Raw</th><th>Scaled</th><th>Name</th><th>Unit</th><th>Access</th></tr></thead><tbody id="regRows"></tbody></table></div><pre id="regReadJson">No register read yet.</pre></section>
        <section class="section"><div class="head"><h2>Point Lookup</h2></div><pre id="regLookupResult">No lookup yet.</pre></section>
      </section>

      <section id="page-release" data-title="Release Center" class="page">
        <section class="section"><div class="head"><h2>Release / Snapshot Center</h2><span class="muted">Export project/runtime evidence for site handover, support, and rollback review. No device connection or register write.</span></div>
          <div class="content">
            <div class="cards" id="releaseCards"></div>
            <div class="toolbar">
              <button onclick="loadReleaseCenter()">Refresh Release Summary</button>
              <a class="btn" href="/api/release/export.zip" target="_blank">Download Release ZIP</a>
              <a class="btn" href="/api/release/manifest" target="_blank">Manifest JSON</a>
              <a class="btn" href="/api/release/snapshot.json" target="_blank">Runtime Snapshot JSON</a>
            </div>
          </div>
        </section>
        <section class="grid2">
          <section class="section"><div class="head"><h2>Export Package Contents</h2><span class="muted">Files that will be included if present.</span></div><div class="scroll"><table><thead><tr><th>Kind</th><th>Path</th><th>Exists</th><th>Size</th></tr></thead><tbody id="releaseFileRows"></tbody></table></div></section>
          <section class="section"><div class="head"><h2>Release Notes</h2><span class="muted">Auto-generated handover summary.</span></div><div class="content"><pre id="releaseNotesBox">Click Refresh Release Summary.</pre></div></section>
        </section>
      </section>

      <section id="page-curves" data-title="Curves" class="page">
        <section class="section"><div class="head"><h2>Curves</h2><span class="muted">Web migration of real-time curves plus browser-side CSV playback. No browser-side Modbus reads.</span></div>
          <div class="content">
            <div class="toolbar" style="margin-bottom:12px; flex-wrap:wrap">
              <select id="curveDeviceType" onchange="renderCurve()"><option value="all">All</option><option value="BMS">BMS</option><option value="PCS">PCS</option></select>
              <select id="curveDevice" onchange="renderCurve()"></select>
              <select id="curveSignal" onchange="renderCurve()"><option value="soc">SOC</option><option value="voltage">Voltage</option><option value="current">Current</option><option value="power">Power</option><option value="actual_power">PCS Actual Power</option><option value="reactive_power">Reactive Power</option><option value="temperature">Temperature</option><option value="bms_status">BMS Status</option><option value="rack_count">BMS Online Rack Count</option></select>
              <label class="muted"><input type="checkbox" id="curveMulti" style="min-width:0" onchange="renderCurve()" /> Compare all devices of same type</label>
              <input id="curveMaxSamples" type="number" value="600" min="60" max="5000" step="60" placeholder="Max samples" />
              <button onclick="resetCurveBuffer()">Reset Buffer</button>
              <button onclick="exportCurveCsv()">Export Browser CSV</button>
            </div>
            <canvas id="curveCanvas" width="1100" height="380" style="width:100%; max-height:430px; background:#020617; border:1px solid var(--line); border-radius:12px"></canvas>
            <div class="muted" id="curveHint" style="margin-top:8px">Real-time mode keeps browser samples while this page is open. CSV playback can be pasted below.</div>
          </div>
        </section>
        <section class="grid2">
          <section class="section"><div class="head"><h2>Curve Stats</h2></div><div class="scroll"><table><thead><tr><th>Series</th><th>Samples</th><th>Min</th><th>Max</th><th>Last</th></tr></thead><tbody id="curveStatsRows"></tbody></table></div></section>
          <section class="section"><div class="head"><h2>CSV Playback</h2><span class="muted">Upload or paste CSV with columns time,device,signal,value or timestamp,name,value.</span></div><div class="content"><div class="curve-upload-strip"><input id="curveCsvFile" type="file" accept=".csv,.txt" /><button onclick="loadCurveCsvFile()">Upload CSV Playback</button><button onclick="loadCurveCsvPlayback()">Load Text Below</button></div><textarea id="curveCsvText" style="width:100%; min-height:120px; background:#020617; color:var(--text); border:1px solid var(--line); border-radius:10px; padding:10px; font-family:ui-monospace, SFMono-Regular, Menlo, monospace;" placeholder="time,device,signal,value
2026-05-31T10:00:00,BMS-1,soc,55.2"></textarea><div class="toolbar"><button onclick="loadCurveCsvPlayback()">Load CSV Playback</button><button onclick="clearCurveCsvPlayback()">Clear Playback</button></div><pre id="curveCsvStatus">No CSV loaded.</pre></div></section>
        </section>
      </section>
      <section id="page-settings" data-title="Settings" class="page">
        <section class="section"><div class="head"><div><h2>Runtime Settings</h2><span class="muted">Runtime-owned parameters. Safe live values can be saved; restart-required values are marked.</span></div><div class="toolbar"><button onclick="loadRuntimeSettings()">Reload Settings</button><button onclick="saveRuntimeSettingsFromTable()">Save Editable Settings</button><a class="btn" href="/api/runtime/settings" target="_blank">Settings JSON</a></div></div>
          <div class="content">
            <div class="cards">
              <div class="card"><div class="label">API Schema</div><div class="value" id="settingsSchema">-</div></div>
              <div class="card"><div class="label">Runtime PID</div><div class="value" id="settingsPid">-</div></div>
              <div class="card"><div class="label">Uptime</div><div class="value" id="settingsUptime">-</div></div>
            </div>
            <div id="runtimeSettingsResult" class="muted" style="margin:8px 0">Runtime settings not loaded.</div>
            <div class="scroll"><table><thead><tr><th>Key</th><th>Value</th><th>Source</th><th>Editable</th><th>Restart</th><th>Description</th></tr></thead><tbody id="runtimeSettingsRows"></tbody></table></div>
          </div>
        </section>
        <section class="section"><div class="head"><h2>Diagnostics</h2><div class="toolbar"><button onclick="loadMetrics()">Refresh Metrics</button><button onclick="loadLogsStatus()">Refresh Logs</button><button onclick="loadCsvStatus()">Refresh CSV</button></div></div>
          <div class="content grid2">
            <section><h2>Metrics</h2><pre id="settingsMetrics">-</pre></section>
            <section><h2>CSV / Logs</h2><pre id="settingsCsvLogs">-</pre></section>
          </div>
        </section>
      </section>
      <section id="page-site" data-title="Site Config" class="page">
        <section class="section"><div class="head"><h2>Site Config</h2><span class="muted">Runtime is the authority. Export is safe; import/save requires confirmation.</span></div>
          <div class="content">
            <div class="toolbar" style="margin-bottom:12px"><button onclick="loadSiteConfig()">Load From Runtime</button><button onclick="downloadSiteConfig()">Download JSON</button><button onclick="saveSiteConfigFromEditor()">Save Editor To Runtime</button><button onclick="siteSaveRuntime()">Persist Runtime Site</button></div>
            <textarea id="siteConfigEditor" spellcheck="false" style="width:100%; min-height:420px; background:#020617; color:var(--text); border:1px solid var(--line); border-radius:12px; padding:12px; font-family:ui-monospace, SFMono-Regular, Menlo, monospace"></textarea>
            <div id="siteConfigResult" class="muted" style="margin-top:8px">Load site config to edit/export.</div>
          </div>
        </section>
      </section>
      <section id="page-alarmcenter" data-title="Alarm Center" class="page">
        <section class="section"><div class="head"><h2>Alarm Center</h2><span class="muted">Site-level active alarms/errors with filter, acknowledge, and export.</span><div class="toolbar"><button onclick="loadAlarmCenter()">Refresh Alarm Center</button><a class="btn" href="/api/alarm-center/export.csv" target="_blank">Export CSV</a></div></div>
          <div class="content">
            <div class="toolbar" style="margin-bottom:10px">
              <input id="alarmFilterText" placeholder="Filter device / signal / message" oninput="loadAlarmCenterDebounced()">
              <select id="alarmFilterSeverity" onchange="loadAlarmCenter()"><option value="">All severity</option><option value="error">Error</option><option value="alarm">Alarm</option><option value="warning">Warning</option></select>
              <label class="muted"><input id="alarmIncludeAck" type="checkbox" checked onchange="loadAlarmCenter()"> include acknowledged</label>
              <button onclick="clearAlarmAckAll()">Clear ACK records</button>
            </div>
            <div id="alarmCenterCards" class="cards"></div>
            <div class="grid2">
              <section class="section"><div class="head"><h2>Active Alarms / Errors</h2></div><div class="scroll"><table><thead><tr><th>Device</th><th>Severity</th><th>Signal / Message</th><th>Value</th><th>ACK</th><th>Action</th></tr></thead><tbody id="alarmCenterRows"></tbody></table></div></section>
              <section class="section"><div class="head"><h2>Top Statistics</h2></div><div class="content"><pre id="alarmCenterStats">-</pre></div></section>
            </div>
            <pre id="alarmCenterRaw">-</pre>
          </div>
        </section>
      </section>
      <section id="page-alarms" data-title="BMS Alarms" class="page">
        <section class="section"><div class="head"><h2>BMS Alarms</h2><div class="toolbar"><select id="alarmDevice" onchange="loadAlarms()"></select><button onclick="loadAlarms()">Load</button></div></div><div class="scroll"><table><thead><tr><th>Address</th><th>Raw</th><th>Active Bits</th></tr></thead><tbody id="alarms"></tbody></table></div></section>
      </section>
      <section id="page-clusters" data-title="Clusters / Power Map" class="page">
        <section class="section"><div class="head"><h2>Clusters</h2><div class="toolbar"><button onclick="loadPowerMapStatus(); loadPowerMapEditor()">Reload Power Map</button><a class="btn" href="/api/power-map/status" target="_blank">Power Map JSON</a></div></div><div class="scroll"><table><thead><tr><th>Name</th><th>BMS</th><th>PCS</th><th>Mode</th><th>Power map</th></tr></thead><tbody id="clusters"></tbody></table></div></section>
        <section class="section">
          <div class="head"><div><h2>Power Map Editor</h2><div class="muted">Human-friendly weight editor. Configure how each PCS distributes power across BMS devices in the selected cluster. Each PCS row should normally sum to 1.000.</div></div><div class="toolbar"><select id="pmClusterSelect" onchange="renderPowerMapEditorForSelected()"></select><button onclick="powerMapAutoEvenSelected()">Auto Even</button><button onclick="powerMapNormalizeSelected()">Normalize Rows</button><button onclick="savePowerMapEditor()">Save Power Map</button></div></div>
          <div class="content">
            <div class="pm-helper">
              <div class="mini"><b>Recommended</b><br><span class="muted">Click Auto Even for normal commissioning. It assigns equal BMS weights for every PCS.</span></div>
              <div class="mini"><b>Weight meaning</b><br><span class="muted">0.25 means 25% share. The values in each PCS row should add up to 1.000.</span></div>
              <div class="mini"><b>Current cluster</b><br><span id="pmEditorSummary" class="muted">Select a cluster.</span></div>
            </div>
            <div class="scroll"><table class="pm-editor-table"><thead id="pmEditorHead"></thead><tbody id="pmEditorRows"></tbody></table></div>
            <pre id="pmEditorResult">Use Auto Even or edit the weight cells, then click Save Power Map.</pre>
          </div>
        </section>
        <section class="section"><div class="head"><h2>Power Map Runtime Status</h2><span class="muted">Shows whether saved power_map is in runtime-native PCS → BMS weight format and ready for strategy dispatch.</span></div><div class="content"><div class="scroll"><table><thead><tr><th>Cluster</th><th>Ready</th><th>PCS Weight Sums</th><th>Runtime Power Map</th></tr></thead><tbody id="powerMapStatusRows"></tbody></table></div><pre id="powerMapIssues">Click Reload Power Map Status.</pre></div></section>
      </section>
      <section id="page-commands" data-title="Commands" class="page"><section class="section"><div class="head"><h2>Recent Commands</h2><div class="toolbar"><a class="btn" href="/api/commands/recent" target="_blank">Memory JSON</a><a class="btn" href="/api/commands/audit" target="_blank">Audit JSONL View</a><button onclick="loadCommandAudit()">Load Audit Summary</button></div></div><div class="scroll"><table><thead><tr><th>Time</th><th>ID</th><th>Name</th><th>Risk</th><th>Confirmed</th><th>Status</th><th>OK</th><th>Message</th></tr></thead><tbody id="commands"></tbody></table></div></section><section class="section"><div class="head"><h2>Command Audit</h2><span class="muted">Persistent profile command_audit.jsonl summary.</span></div><div class="content"><pre id="commandAuditBox">-</pre></div></section></section>
      <section id="page-health" data-title="Health Monitor" class="page">
        <section class="section"><div class="head"><h2>Runtime Health Monitor</h2><span class="muted">Read-only self-check for large-site stability, worker status, data freshness, command queue and curve cache.</span></div>
          <div class="content">
            <div class="toolbar" style="margin-bottom:12px; flex-wrap:wrap">
              <button onclick="loadHealthMonitor()">Refresh Health</button>
              <a class="btn" href="/api/health-monitor" target="_blank">Health JSON</a>
              <a class="btn" href="/api/health-monitor/export.csv" target="_blank">Export CSV</a>
              <a class="btn" href="/api/performance/status" target="_blank">Performance JSON</a>
            </div>
            <div id="healthCards" class="cards"></div>
            <div class="grid2">
              <section class="section"><div class="head"><h2>Issues</h2></div><div class="scroll"><table><thead><tr><th>Severity</th><th>Area</th><th>Message</th></tr></thead><tbody id="healthIssues"></tbody></table></div></section>
              <section class="section"><div class="head"><h2>Device Freshness</h2></div><div class="scroll"><table><thead><tr><th>Kind</th><th>Device</th><th>Online</th><th>Age(s)</th><th>Error</th></tr></thead><tbody id="healthFreshness"></tbody></table></div></section>
            </div>
            <pre id="healthRaw">-</pre>
          </div>
        </section>
      </section>
      <section id="page-parity" data-title="UI-Web Parity" class="page">
        <section class="section"><div class="head"><h2>UI → Web Parity Audit</h2><span class="muted">Read-only checklist for confirming PySide functions have equivalent Web pages/APIs.</span></div>
          <div class="content">
            <div class="toolbar" style="margin-bottom:12px; flex-wrap:wrap">
              <button onclick="loadParityAudit()">Run Parity Audit</button>
              <a class="btn" href="/api/ui-web-parity" target="_blank">Parity JSON</a>
              <a class="btn" href="/api/ui-web-parity/export.csv" target="_blank">Export CSV</a>
              <a class="btn" href="/api/runtime/separation-audit" target="_blank">Separation JSON</a>
            </div>
            <div id="parityCards" class="cards"></div>
            <div class="scroll"><table><thead><tr><th>Area</th><th>PySide UI Feature</th><th>Web Page</th><th>Status</th><th>API / Evidence</th></tr></thead><tbody id="parityRows"></tbody></table></div>
            <pre id="parityRaw">Click Run Parity Audit.</pre>
          </div>
        </section>
      </section>

      <section id="page-uiactions" data-title="UI Buttons" class="page">
        <section class="section"><div class="head"><h2>UI Button Parity</h2><span class="muted">Buttons found in the PySide UI that were not first-class Web buttons yet. File-dialog actions are shown and return guidance instead of opening hidden dialogs.</span></div>
          <div class="content">
            <div class="toolbar" style="margin-bottom:12px">
              <button onclick="loadUiActionMatrix()">Reload Button Matrix</button>
              <a class="btn" href="/api/ui-button-matrix" target="_blank">Open Matrix JSON</a>
            </div>
            <div class="scroll"><table><thead><tr><th>Group</th><th>PySide Button</th><th>Web Action</th><th>Risk</th><th>Run</th></tr></thead><tbody id="uiActionRows"></tbody></table></div>
            <pre id="uiActionResult">-</pre>
          </div>
        </section>
      </section>


      <section id="page-lts" data-title="9.x LTS" class="page">
        <section class="section"><div class="head"><h2>9.x LTS Final Audit</h2><span class="muted">Read-only final checks for Web-only mode, packaging resources, control closure, and data consistency.</span></div>
          <div class="content">
            <div class="toolbar" style="margin-bottom:12px; flex-wrap:wrap">
              <button onclick="loadLtsAudit()">Run LTS Audit</button>
              <a class="btn" href="/api/lts/final" target="_blank">LTS JSON</a>
              <a class="btn" href="/api/lts/final/export.csv" target="_blank">Export CSV</a>
              <a class="btn" href="/api/lts/packaging-check" target="_blank">Packaging JSON</a>
              <a class="btn" href="/api/lts/data-consistency" target="_blank">Consistency JSON</a>
            </div>
            <div id="ltsCards" class="cards"></div>
            <div class="grid2">
              <section class="section"><div class="head"><h2>Control Closure</h2></div><div class="content scroll"><table><thead><tr><th>Area</th><th>Feature</th><th>Status</th><th>API</th><th>Safety</th></tr></thead><tbody id="ltsControlRows"></tbody></table></div></section>
              <section class="section"><div class="head"><h2>Packaging Resources</h2></div><div class="content scroll"><table><thead><tr><th>Item</th><th>Status</th><th>Path</th></tr></thead><tbody id="ltsPackagingRows"></tbody></table></div></section>
            </div>
            <pre id="ltsRaw">Click Run LTS Audit.</pre>
          </div>
        </section>
      </section>

      <section id="page-runtime" data-title="Runtime" class="page">
        <div class="grid2">
          <section class="section"><div class="head"><h2>Metrics</h2><button onclick="loadMetrics()">Refresh metrics</button></div><div class="content"><pre id="metricsBox">-</pre></div></section>
          <section class="section"><div class="head"><h2>Restore Plan</h2><button onclick="loadRestorePlan()">Refresh restore plan</button></div><div class="content"><pre id="restoreBox">-</pre></div></section>
        </div>
        <section class="section"><div class="head"><h2>Runtime Lifecycle</h2><span class="muted">Use this when work is finished. It stops the Runtime process that owns polling, workers, CSV and strategy.</span></div>
          <div class="content">
            <div class="toolbar">
              <button class="danger" onclick="shutdownRuntimeFromWeb()">Shutdown Runtime</button>
              <a class="btn" href="/api/health" target="_blank">Health JSON</a><a class="btn" href="/api/performance/status" target="_blank">Performance JSON</a>
            </div>
            <p class="muted">Closing the browser tab does not stop Runtime. Use this button when work is finished. If the main page is frozen, open <code>/shutdown</code> or run <code>ESS-AIO-Shutdown.exe</code>.</p>
            <pre id="runtimeShutdownBox">-</pre>
          </div>
        </section>
      </section>
    </main>
  </div>
<script>
let SNAP = null;
let CURRENT_PAGE = 'runtimecenter';
let SELECTED_DEVICE = {kind:'', name:''};
let ANALYZER_FILES = [];
let CURRENT_ANALYZER_TAB = 'upload';
const $ = (id) => document.getElementById(id);
function esc(v){ return String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function pill(v){ const c=String(v||'unknown').toLowerCase(); return `<span class="pill ${esc(c)}">${esc(v||'unknown')}</span>`; }
function fmtTs(ts){ if(!ts) return '-'; try { return new Date(ts*1000).toLocaleTimeString(); } catch(e){ return '-'; } }
function showPage(p){ CURRENT_PAGE=p; document.querySelectorAll('nav button, .topnav button').forEach(b=>b.classList.toggle('active', b.dataset.page===p)); document.querySelectorAll('.page').forEach(x=>x.classList.remove('active')); const pageEl=$(`page-${p}`); if(!pageEl){ console.warn('Missing page', p); return; } pageEl.classList.add('active'); renderActive(); if(p==='runtime'){ loadMetrics(); loadRestorePlan(); } if(p==='health'){ loadHealthMonitor(); } if(p==='parity'){ loadParityAudit(); } if(p==='uiactions'){ loadUiActionMatrix(); } if(p==='lts'){ loadLtsAudit(); } if(p==='commands'){ loadCommandAudit(); } if(p==='settings'){ loadRuntimeSettings(); } if(p==='clusters'){ loadPowerMapStatus(); loadPowerMapEditor(); } if(p==='project'){ loadProjectProfiles(); loadProjectConfig(); } if(p==='alarms'){ populateAlarmDevices(); } if(p==='strategy'){ populateStrategyClusters(); } if(p==='ops'){ populateOpsBmsDevices(); loadCsvStatus(); } }
function cardsHtml(summary, s){
  const rec = s.recording || {}; const soaking = (s.soak_test||{}).running;
  return [
    ['BMS online', `${summary.bms_online||0}/${summary.bms_total||0}`, (summary.bms_error||0)?'warn':'ok'],
    ['PCS online', `${summary.pcs_online||0}/${summary.pcs_total||0}`, (summary.pcs_error||0)?'warn':'ok'],
    ['Strategies', summary.strategy_count||0, 'accent'],
    ['CSV', (rec.bms_recording||rec.pcs_recording) ? 'Recording' : 'Idle', (rec.bms_recording||rec.pcs_recording) ? 'ok' : ''],
    ['Commands', (s.command_acks||[]).length, ''],
    ['Soak test', soaking ? 'Running' : 'Idle', soaking ? 'ok' : ''],
  ].map(([l,v,c]) => `<div class="card"><div class="label">${esc(l)}</div><div class="value ${c}">${esc(v)}</div></div>`).join('');
}

function numVal(v){ const n=Number(v); return Number.isFinite(n)?n:null; }
function fmtMetric(v, suffix='', digits=1){ const n=numVal(v); if(n===null) return esc(v ?? '-'); return esc(n.toFixed(digits).replace(/\.0$/,'')) + esc(suffix); }
function bmsStatusLabel(raw){
  const v = String(raw ?? '').trim();
  const n = Number(v);
  if(Number.isFinite(n)){
    const map = {1:'Normal',2:'Full Charge',3:'Full Discharge',4:'Warning',5:'Fault'};
    return map[n] || `Unknown(${n})`;
  }
  const l=v.toLowerCase();
  if(!l) return '-';
  if(l.includes('fault') || l.includes('alarm')) return 'Fault';
  if(l.includes('warn')) return 'Warning';
  if(l.includes('full') && l.includes('charge')) return 'Full Charge';
  if(l.includes('full') && l.includes('discharge')) return 'Full Discharge';
  if(l.includes('normal') || l.includes('ready') || l.includes('online') || l.includes('running')) return 'Normal';
  return v;
}
function bmsStatusClass(label){
  const l=String(label||'').toLowerCase();
  if(l.includes('fault')) return 'bad';
  if(l.includes('warning')) return 'warn';
  if(l.includes('full')) return 'accent';
  if(l.includes('normal')) return 'ok';
  return '';
}
function bmsStatusFromDevice(d, vals){
  vals=vals||{};
  const keys=['bms_status','system_status','status','state','work_status','running_status'];
  for(const k of keys){ if(vals[k]!==undefined&&vals[k]!==null&&vals[k]!==''&&vals[k]!=='-') return bmsStatusLabel(vals[k]); }
  return bmsStatusLabel(d.bms_status ?? d.system_status ?? d.work_status ?? d.state ?? d.status_code ?? d.status);
}
function metricFromDevice(d, vals, keys){
  vals=vals||{}; d=d||{};
  for(const k of keys){ if(vals[k]!==undefined&&vals[k]!==null&&vals[k]!==''&&vals[k]!=='-') return vals[k]; }
  for(const k of keys){ if(d[k]!==undefined&&d[k]!==null&&d[k]!==''&&d[k]!=='-') return d[k]; }
  if(keys.includes('power')){
    const v=metricFromDevice(d, vals, ['voltage','system_voltage','total_voltage','dc_voltage','pack_voltage']);
    const i=metricFromDevice(d, vals, ['current','system_current','dc_current','pack_current']);
    const vn=Number(v), inum=Number(i);
    if(Number.isFinite(vn)&&Number.isFinite(inum)) return vn*inum/1000.0;
  }
  return '-';
}
function dashboardBar(label, value, total, cls=''){
  const t=Math.max(Number(total)||0,0); const v=Math.max(Number(value)||0,0); const pct=t?Math.max(0,Math.min(100,(v/t)*100)):0;
  return `<div class="bar-row"><div class="bar-top"><span>${esc(label)}</span><b>${esc(v)}/${esc(t)}</b></div><div class="bar-track"><div class="bar-fill ${esc(cls)}" style="width:${pct}%"></div></div></div>`;
}
function dashboardSeverityBar(label, count, total, cls=''){
  const t=Math.max(Number(total)||0,0); const v=Math.max(Number(count)||0,0); const pct=t?Math.max(4,Math.min(100,(v/t)*100)):0;
  return `<div class="bar-row"><div class="bar-top"><span>${esc(label)}</span><b>${esc(v)}</b></div><div class="bar-track"><div class="bar-fill ${esc(cls)}" style="width:${pct}%"></div></div></div>`;
}

function deviceRows(){ const ds=(SNAP||{}).device_states||{}; const rows=[]; for(const [typ, items] of Object.entries({BMS:ds.bms||{}, PCS:ds.pcs||{}})){ Object.values(items).forEach(d=>rows.push({...d, _type:typ})); } return rows.sort((a,b)=>String(a._type+a.name).localeCompare(String(b._type+b.name))); }


let PROJECT=null;
async function loadProjectProfiles(){
  try{
    const r=await fetch('/api/project/profiles',{cache:'no-store'}); const data=await r.json();
    const setOptions=(id, arr, fallback)=>{ const el=$(id); if(!el) return; const old=el.value||fallback; const opts=(arr&&arr.length?arr:[fallback]); el.innerHTML=opts.map(x=>`<option value="${esc(x)}">${esc(x)}</option>`).join(''); if(opts.includes(old)) el.value=old; };
    setOptions('bmsCfgProfile', data.bms_profiles, 'catl_v22');
    setOptions('pcsCfgProfile', data.pcs_profiles, 'kehua_bcs1250');
  }catch(e){ console.warn('profile options unavailable', e); }
}
async function loadProjectConfig(){ const box=$('projectConfigResult'); if(box) box.textContent='Loading...'; try{ const r=await fetch('/api/project/config',{cache:'no-store'}); PROJECT=await r.json(); renderProjectConfig(); if(box) box.innerHTML='<span class="ok">Loaded.</span>'; }catch(e){ if(box) box.innerHTML=`<span class="bad">${esc(e)}</span>`; } }
function projectBmsNames(){ return (PROJECT?.bms_devices||[]).map(x=>String(x.name||'').trim()).filter(Boolean).sort(); }
function projectPcsNames(){ return Object.keys(PROJECT?.pcs_configs||{}).filter(Boolean).sort(); }
function setMultiSelectOptions(id, names, selected){
  const el=$(id); if(!el) return;
  const sel=new Set((selected||[]).map(String));
  el.innerHTML=(names||[]).map(n=>`<option value="${esc(n)}" ${sel.has(String(n))?'selected':''}>${esc(n)}</option>`).join('');
}
function getMultiSelectValues(id){ const el=$(id); if(!el) return []; return Array.from(el.selectedOptions||[]).map(o=>String(o.value||'').trim()).filter(Boolean); }
function refreshClusterBindingSelectors(selectedBms=null, selectedPcs=null){
  if(!PROJECT || !$('clusterCfgBms') || !$('clusterCfgPcs')) return;
  const curBms = selectedBms || getMultiSelectValues('clusterCfgBms');
  const curPcs = selectedPcs || getMultiSelectValues('clusterCfgPcs');
  setMultiSelectOptions('clusterCfgBms', projectBmsNames(), curBms);
  setMultiSelectOptions('clusterCfgPcs', projectPcsNames(), curPcs);
  refreshClusterPowerMapEditor();
}
function selectedClusterPowerMap(){
  const mode=$('clusterPowerMapMode')?.value || 'even';
  const pcs=getMultiSelectValues('clusterCfgPcs');
  if(mode==='keep') return null;
  if(mode==='even'){
    if(!pcs.length) return {};
    const share=Number((1/pcs.length).toFixed(6));
    const m={}; pcs.forEach(n=>m[n]=share); return m;
  }
  const txt=String($('clusterPowerMapJson')?.value||'').trim();
  if(!txt) return {};
  try{
    const parsed=JSON.parse(txt);
    const allowed=new Set(pcs);
    const out={};
    Object.entries(parsed||{}).forEach(([k,v])=>{ if(allowed.has(String(k))) out[String(k)] = Number(v); });
    return out;
  }catch(e){ throw new Error('Power Map JSON is invalid: '+e); }
}
function refreshClusterPowerMapEditor(){ if(!$('clusterPowerMapJson') || !$('clusterCfgPcs')) return;
  const mode=$('clusterPowerMapMode')?.value || 'even';
  const box=$('clusterPowerMapJson'); if(!box) return;
  if(mode==='keep'){ box.disabled=true; return; }
  box.disabled=false;
  if(mode==='even'){
    const pcs=getMultiSelectValues('clusterCfgPcs');
    const m={}; if(pcs.length){ const share=Number((1/pcs.length).toFixed(6)); pcs.forEach(n=>m[n]=share); }
    box.value=JSON.stringify(m,null,2);
  }
}
function autoEvenPowerMap(){ const rows=((PROJECT&&PROJECT.site_config&&PROJECT.site_config.clusters)||[]); rows.forEach((_,i)=>autoEvenRowPowerMap(i)); }
function renderProjectConfig(){
  if(!PROJECT) return;
  const bms=PROJECT.bms_devices||[]; const pcs=PROJECT.pcs_configs||{}; const clusters=((PROJECT.site_config||{}).clusters)||[];
  $('projectBmsCount').textContent=`${bms.length} BMS`; $('projectPcsCount').textContent=`${Object.keys(pcs).length} PCS`; if($('projectClusterCount')) $('projectClusterCount').textContent=`${clusters.length} Cluster`;
  $('projectBmsRows').innerHTML=bms.map(d=>{ const name=String(d.name||''); return `<tr><td>${esc(name)}</td><td>${esc(d.host)}</td><td>${esc(d.port)}</td><td>${esc(d.unit_id)}</td><td>${esc(d.interval||d.poll_interval)}</td><td>${esc(d.profile||d.driver||'')}</td><td><button data-action="fill-bms" data-name="${esc(name)}" onclick="return actionClick(event)">Edit</button> <button class="danger" data-action="remove-bms" data-name="${esc(name)}" onclick="return actionClick(event)">Remove</button></td></tr>`; }).join('') || '<tr><td colspan="7" class="muted">No BMS configured</td></tr>';
  $('projectPcsRows').innerHTML=Object.entries(pcs).sort().map(([name,c])=>`<tr><td>${esc(name)}</td><td>${esc(c.host)}</td><td>${esc(c.port)}</td><td>${esc(c.unit_id)}</td><td>${esc(c.profile||c.driver||'')}</td><td>${esc(c.enabled)}</td><td><button data-action="fill-pcs" data-name="${esc(name)}" onclick="return actionClick(event)">Edit</button> <button class="danger" data-action="remove-pcs" data-name="${esc(name)}" onclick="return actionClick(event)">Remove</button></td></tr>`).join('') || '<tr><td colspan="7" class="muted">No PCS configured</td></tr>';
  renderClusterBindingRows(clusters);
  $('projectConfigRaw').textContent=JSON.stringify(PROJECT,null,2);
}

function configuredBmsNames(){ return (PROJECT?.bms_devices||[]).map(x=>String(x.name||'')).filter(Boolean).sort(); }
function configuredPcsNames(){ return Object.keys(PROJECT?.pcs_configs||{}).sort(); }
let CLUSTER_EDIT = {};
let CLUSTER_BINDING_DIRTY = false;
let CLUSTER_BINDING_INTERACTIVE_UNTIL = 0;
function touchClusterBindingEditor(ms=30000){ CLUSTER_BINDING_INTERACTIVE_UNTIL = Date.now() + ms; }
function isClusterBindingEditing(){ return Date.now() < CLUSTER_BINDING_INTERACTIVE_UNTIL; }
function markClusterBindingDirty(){ CLUSTER_BINDING_DIRTY = true; touchClusterBindingEditor(60000); const el=$('projectConfigResult'); if(el) el.innerHTML='<span class="warn">Cluster binding has unsaved changes. Click Save on the row you changed.</span>'; }
function normalizedClusterBms(c){ return [...(c?.bms_devices||c?.bms||[])].map(String).filter(Boolean); }
function normalizedClusterPcs(c){ return [...(c?.pcs_devices||c?.pcs||(c?.pcs_device?[c.pcs_device]:[])||[])].map(String).filter(Boolean); }
function normalizePowerMap(raw, bmsList=[], pcsList=[]){
  const allowedBms=new Set((bmsList||[]).map(String));
  const allowedPcs=new Set((pcsList||[]).map(String));
  const out={};
  if(!raw || typeof raw!=='object' || Array.isArray(raw)) return out;
  Object.entries(raw).forEach(([pcs, weights])=>{
    const pc=String(pcs);
    if(allowedPcs.size && !allowedPcs.has(pc)) return;
    if(weights && typeof weights==='object' && !Array.isArray(weights)){
      const row={};
      Object.entries(weights).forEach(([bms,w])=>{
        const bm=String(bms);
        if(allowedBms.size && !allowedBms.has(bm)) return;
        const n=Number(w);
        if(Number.isFinite(n) && n>=0) row[bm]=n;
      });
      if(Object.keys(row).length) out[pc]=row;
    } else {
      // Backward compatibility with the old flat map: {"PCS-1": 0.5}
      // Interpret it as this PCS can use every selected BMS with that weight.
      const n=Number(weights);
      if(Number.isFinite(n) && n>=0){
        const row={};
        (bmsList||[]).forEach(bm=>row[String(bm)]=n);
        if(Object.keys(row).length) out[pc]=row;
      }
    }
  });
  return out;
}
function evenMapForRow(row){
  const bms=(row?.bms||[]).map(String).filter(Boolean);
  const pcs=(row?.pcs||[]).map(String).filter(Boolean);
  const out={};
  if(!bms.length || !pcs.length) return out;
  const share=Number((1/bms.length).toFixed(6));
  pcs.forEach(pc=>{ out[pc]={}; bms.forEach(bm=>out[pc][bm]=share); });
  return out;
}
function clusterEditFromProject(clusters){
  const next={};
  (clusters||[]).forEach((c,idx)=>{
    const name=String(c.name||`Cluster-${idx+1}`);
    const bms=normalizedClusterBms(c);
    const pcs=normalizedClusterPcs(c);
    next[name]={bms, pcs, power_map:normalizePowerMap(c.power_map||{}, bms, pcs)};
  });
  return next;
}
function sameClusterNames(a,b){ const ak=Object.keys(a||{}).sort().join('|'); const bk=Object.keys(b||{}).sort().join('|'); return ak===bk; }
function usedByOtherClusters(kind, clusterName){
  const used=new Set();
  Object.entries(CLUSTER_EDIT||{}).forEach(([name,row])=>{
    if(String(name)===String(clusterName)) return;
    (row[kind]||[]).forEach(x=>used.add(String(x)));
  });
  return used;
}
function clusterAvailable(kind, clusterName){
  const all = kind==='bms' ? configuredBmsNames() : configuredPcsNames();
  const current = new Set(((CLUSTER_EDIT[clusterName]||{})[kind]||[]).map(String));
  const used = usedByOtherClusters(kind, clusterName);
  return all.filter(n => current.has(n) || !used.has(n));
}
function bindSelectOptions(names){ return ['<option value="">Select device...</option>'].concat((names||[]).map(n=>`<option value="${esc(n)}">${esc(n)}</option>`)).join(''); }
function jsArg(v){ return JSON.stringify(String(v)); }
function encArg(v){ return encodeURIComponent(String(v)); }
function decArg(v){ return decodeURIComponent(String(v||'')); }
function clusterDomId(name){ return 'cl_' + String(name||'').replace(/[^a-zA-Z0-9_-]/g, '_'); }
function chipHtml(clusterName, kind, values){
  return (values||[]).map(v=>`<span class="chip">${esc(v)} <button title="Remove" data-action="remove-bind-chip" data-cluster="${esc(clusterName)}" data-kind="${esc(kind)}" data-value="${esc(v)}" onclick="return actionClick(event)">×</button></span>`).join('') || '<span class="muted">None</span>';
}
function refreshBindRow(clusterName){
  const row=CLUSTER_EDIT[clusterName]||{bms:[],pcs:[],power_map:{}};
  const cid=clusterDomId(clusterName);
  const bmsBox=$(`bindBmsChips_${cid}`), pcsBox=$(`bindPcsChips_${cid}`);
  if(bmsBox) bmsBox.innerHTML=chipHtml(clusterName,'bms',row.bms);
  if(pcsBox) pcsBox.innerHTML=chipHtml(clusterName,'pcs',row.pcs);
  const bmsSel=$(`bindBmsPick_${cid}`), pcsSel=$(`bindPcsPick_${cid}`);
  if(bmsSel) bmsSel.innerHTML=bindSelectOptions(clusterAvailable('bms', clusterName).filter(n=>!(row.bms||[]).includes(n)));
  if(pcsSel) pcsSel.innerHTML=bindSelectOptions(clusterAvailable('pcs', clusterName).filter(n=>!(row.pcs||[]).includes(n)));
  const pm=$(`clusterRowPower_${cid}`); if(pm) pm.textContent=JSON.stringify(row.power_map||{});
}
function addBindPick(clusterName, kind){
  touchClusterBindingEditor(60000);
  const cid=clusterDomId(clusterName); const sel=$(`bind${kind==='bms'?'Bms':'Pcs'}Pick_${cid}`); const v=String(sel?.value||'').trim();
  if(!v) return;
  const row=CLUSTER_EDIT[clusterName]||(CLUSTER_EDIT[clusterName]={bms:[],pcs:[],power_map:{}});
  const arr=row[kind]||(row[kind]=[]);
  if(!arr.includes(v)) arr.push(v);
  row.power_map=normalizePowerMap(row.power_map||{}, row.bms||[], row.pcs||[]);
  markClusterBindingDirty();
  refreshAllBindRows();
}
function removeBindChip(clusterName, kind, value){
  touchClusterBindingEditor(60000);
  const row=CLUSTER_EDIT[clusterName]; if(!row) return;
  row[kind]=(row[kind]||[]).filter(x=>String(x)!==String(value));
  row.power_map=normalizePowerMap(row.power_map||{}, row.bms||[], row.pcs||[]);
  markClusterBindingDirty();
  refreshAllBindRows();
}
function evenMap(pcs){ return {}; /* deprecated flat map helper; use evenMapForRow(row) */ }
function refreshAllBindRows(){ Object.keys(CLUSTER_EDIT||{}).forEach(refreshBindRow); }
function renderClusterBindingRows(clusters){
  const bmsNames=configuredBmsNames(), pcsNames=configuredPcsNames();
  if($('availableBmsCount')) $('availableBmsCount').textContent=String(bmsNames.length);
  if($('availablePcsCount')) $('availablePcsCount').textContent=String(pcsNames.length);
  const freshEdit = clusterEditFromProject(clusters||[]);
  // While the user is selecting from a dropdown, do not redraw this table.
  if(isClusterBindingEditing() && $('projectClusterRows') && $('projectClusterRows').children.length){ return; }
  if(!CLUSTER_BINDING_DIRTY || !sameClusterNames(CLUSTER_EDIT, freshEdit)) CLUSTER_EDIT = freshEdit;
  const rows=(clusters||[]).map((c,idx)=>{
    const name=String(c.name||`Cluster-${idx+1}`);
    const cid=clusterDomId(name);
    const arg=jsArg(name);
    return `<tr data-cluster="${esc(name)}">
      <td><b>${esc(name)}</b></td>
      <td>
        <div id="bindBmsChips_${cid}" class="chip-list"></div>
        <div class="bind-picker"><select id="bindBmsPick_${cid}" onfocus="touchClusterBindingEditor(60000)" onchange="touchClusterBindingEditor(60000)"></select><button data-action="add-bind-pick" data-cluster="${esc(name)}" data-kind="bms" onmousedown="touchClusterBindingEditor(60000)" onclick="return actionClick(event)">Add</button></div>
      </td>
      <td>
        <div id="bindPcsChips_${cid}" class="chip-list"></div>
        <div class="bind-picker"><select id="bindPcsPick_${cid}" onfocus="touchClusterBindingEditor(60000)" onchange="touchClusterBindingEditor(60000)"></select><button data-action="add-bind-pick" data-cluster="${esc(name)}" data-kind="pcs" onmousedown="touchClusterBindingEditor(60000)" onclick="return actionClick(event)">Add</button></div>
      </td>
      <td>
        <code id="clusterRowPower_${cid}" class="power-map-code" title="Click Edit to configure power map">{}</code>
        <div class="row-actions"><button data-action="edit-power-map" data-cluster="${esc(name)}" onclick="return actionClick(event)">Edit</button><button data-action="auto-even-power-map" data-cluster="${esc(name)}" onclick="return actionClick(event)">Auto Even</button><button class="danger" data-action="clear-power-map" data-cluster="${esc(name)}" onclick="return actionClick(event)">Clear Power Map</button></div>
      </td>
      <td><button data-action="save-cluster-binding" data-cluster="${esc(name)}" onclick="return actionClick(event)">Save</button> <button class="danger" data-action="remove-cluster" data-cluster="${esc(name)}" onclick="return actionClick(event)">Remove Cluster</button></td>
    </tr>`;
  }).join('');
  if($('projectClusterRows')) $('projectClusterRows').innerHTML=rows || '<tr><td colspan="5" class="muted">No clusters configured. Add a cluster below.</td></tr>';
  refreshAllBindRows();
}
function autoEvenPowerMap(){ Object.values(CLUSTER_EDIT||{}).forEach(row=>{ row.power_map=evenMapForRow(row); }); markClusterBindingDirty(); refreshAllBindRows(); }
function autoEvenPowerMapForCluster(name){
  touchClusterBindingEditor(60000);
  const row=CLUSTER_EDIT[name]||(CLUSTER_EDIT[name]={bms:[],pcs:[],power_map:{}});
  row.power_map=evenMapForRow(row);
  markClusterBindingDirty(); refreshBindRow(name);
}
function clearPowerMapForCluster(name){
  touchClusterBindingEditor(60000);
  const row=CLUSTER_EDIT[name]||(CLUSTER_EDIT[name]={bms:[],pcs:[],power_map:{}});
  row.power_map={};
  markClusterBindingDirty(); refreshBindRow(name);
}
async function clearPowerMapAndSave(name){
  if(!confirm(`Clear Power Map for ${name}? This will keep the BMS/PCS binding but remove all PCS→BMS weights.`)) return;
  clearPowerMapForCluster(name);
  await saveClusterBindingByName(name);
}
function editRowPowerMapByName(name){
  touchClusterBindingEditor(120000);
  // Prefer the human-friendly table editor instead of a JSON prompt.
  showPage('clusters');
  setTimeout(()=>{
    const sel=$('pmClusterSelect');
    if(sel){ sel.value=String(name||''); renderPowerMapEditorForSelected(); }
    if($('pmEditorResult')) $('pmEditorResult').innerHTML='<span class="warn">Editing Power Map for '+esc(name)+'. Change weights in the table and click Save Power Map.</span>';
  }, 50);
}
function editRowPowerMap(idx){ const clusters=((PROJECT&&PROJECT.site_config&&PROJECT.site_config.clusters)||[]); const name=String((clusters[idx]||{}).name||''); if(name) editRowPowerMapByName(name); }
async function saveClusterBindingByName(name){
  if(!PROJECT) await loadProjectConfig();
  const data=JSON.parse(JSON.stringify((PROJECT&&PROJECT.site_config)||{site:'ESS Site',clusters:[]}));
  const oldByName={};
  (data.clusters||[]).forEach(c=>{ oldByName[String(c.name||'')]=c; });
  // Save the whole visible binding editor, not only the clicked row. This avoids losing other pending row edits.
  data.clusters=Object.entries(CLUSTER_EDIT||{}).map(([clusterName,row])=>{
    const bms=[...(row.bms||[])].map(String).filter(Boolean);
    const pcs=[...(row.pcs||[])].map(String).filter(Boolean);
    const power_map=normalizePowerMap(row.power_map||{}, bms, pcs);
    return Object.assign({}, oldByName[String(clusterName)]||{}, {
      name:String(clusterName),
      // Native keys used by the PySide/runtime site loader:
      bms_devices:bms,
      pcs_devices:pcs,
      pcs_device:pcs[0]||'',
      // Backward-compatible aliases used by some Web helpers:
      bms:bms,
      pcs:pcs,
      power_map:power_map
    });
  });
  await postJson('/api/site/config', data, 'projectConfigResult');
  CLUSTER_BINDING_DIRTY=false; CLUSTER_BINDING_INTERACTIVE_UNTIL=0;
  await loadProjectConfig(); await validateProjectConfig();
}
async function saveClusterBindingRow(idx, name){ await saveClusterBindingByName(name); }
async function addClusterBindingRow(){
  if(!PROJECT) await loadProjectConfig();
  const name=String($('newClusterName')?.value||'').trim() || prompt('New cluster name:');
  if(!name) return;
  const data=JSON.parse(JSON.stringify((PROJECT&&PROJECT.site_config)||{site:'ESS Site',clusters:[]}));
  if(!Array.isArray(data.clusters)) data.clusters=[];
  if(data.clusters.some(x=>String(x.name)===String(name))){ alert('Cluster already exists.'); return; }
  data.clusters.push({name, bms:[], pcs:[], power_map:{}});
  await postJson('/api/site/config', data, 'projectConfigResult'); if($('newClusterName')) $('newClusterName').value=''; await loadProjectConfig();
}
async function removeClusterBindingRow(name){
  if(!confirm(`Remove cluster ${name}?`)) return;
  if(!PROJECT) await loadProjectConfig();
  const data=JSON.parse(JSON.stringify((PROJECT&&PROJECT.site_config)||{site:'ESS Site',clusters:[]}));
  data.clusters=(data.clusters||[]).filter(x=>String(x.name)!==String(name));
  await postJson('/api/site/config', data, 'projectConfigResult'); await loadProjectConfig(); await validateProjectConfig();
}

function setCfgValue(id, value){ const el=$(id); if(el) el.value = value ?? ''; }
function showConfigEditMessage(kind, name){
  const box=$('projectConfigResult');
  if(box) box.innerHTML = `<span class="ok">Loaded ${esc(kind)} config for <b>${esc(name)}</b>. Edit the form above, then click Save.</span>`;
  const first = kind === 'BMS' ? $('bmsCfgName') : $('pcsCfgName');
  const form = first ? first.closest('.section,.card,.panel,div') : null;
  try{ (form || first)?.scrollIntoView({behavior:'smooth', block:'center'}); }catch(e){ try{ (form || first)?.scrollIntoView(); }catch(_){} }
  if(first){ try{ first.focus({preventScroll:true}); }catch(e){ try{ first.focus(); }catch(_){} } }
}
function getBmsConfigByName(name){
  const target=String(name||'');
  const lists=[PROJECT?.bms_devices, PROJECT?.devices?.bms, PROJECT?.bms];
  for(const list of lists){
    if(Array.isArray(list)){ const d=list.find(x=>String(x?.name||x?.id||'')===target); if(d) return d; }
    else if(list && typeof list==='object'){ const d=list[target]; if(d) return Object.assign({name:target}, d); }
  }
  return null;
}
function getPcsConfigByName(name){
  const target=String(name||'');
  const sources=[PROJECT?.pcs_configs, PROJECT?.pcs_devices, PROJECT?.devices?.pcs, PROJECT?.pcs];
  for(const src of sources){
    if(Array.isArray(src)){ const c=src.find(x=>String(x?.name||x?.id||'')===target); if(c) return c; }
    else if(src && typeof src==='object'){ const c=src[target]; if(c) return Object.assign({name:target}, c); }
  }
  return null;
}
function fillBmsConfig(name){
  const d=getBmsConfigByName(name);
  if(!d){ const box=$('projectConfigResult'); if(box) box.innerHTML=`<span class="bad">BMS config not found: ${esc(name)}</span>`; return false; }
  setCfgValue('bmsCfgName', d.name||d.id||name);
  setCfgValue('bmsCfgHost', d.host||d.ip||'');
  setCfgValue('bmsCfgPort', d.port||502);
  setCfgValue('bmsCfgUnit', d.unit_id ?? d.slave_id ?? d.device_id ?? 1);
  setCfgValue('bmsCfgInterval', d.interval ?? d.poll_interval ?? 2);
  setCfgValue('bmsCfgProfile', d.profile||d.driver||'catl_v22');
  showConfigEditMessage('BMS', d.name||d.id||name);
  return false;
}
function fillPcsConfig(name){
  const c=getPcsConfigByName(name);
  if(!c){ const box=$('projectConfigResult'); if(box) box.innerHTML=`<span class="bad">PCS config not found: ${esc(name)}</span>`; return false; }
  setCfgValue('pcsCfgName', c.name||c.id||name);
  setCfgValue('pcsCfgHost', c.host||c.ip||'');
  setCfgValue('pcsCfgPort', c.port||502);
  setCfgValue('pcsCfgUnit', c.unit_id ?? c.slave_id ?? c.device_id ?? 1);
  setCfgValue('pcsCfgProfile', c.profile||c.driver||'kehua_bcs1250');
  showConfigEditMessage('PCS', c.name||c.id||name);
  return false;
}
function splitNames(v){ return String(v||'').split(',').map(x=>x.trim()).filter(Boolean); }
function fillClusterBinding(name){
  const clusters=((PROJECT&&PROJECT.site_config&&PROJECT.site_config.clusters)||[]); const c=clusters.find(x=>String(x.name)===String(name))||{};
  $('clusterCfgName').value=c.name||name||'';
  refreshClusterBindingSelectors(normalizedClusterBms(c), normalizedClusterPcs(c));
  if($('clusterPowerMapMode')) $('clusterPowerMapMode').value = c.power_map ? 'manual' : 'even';
  if($('clusterPowerMapJson')) $('clusterPowerMapJson').value=JSON.stringify(c.power_map||{},null,2);
  refreshClusterPowerMapEditor();
}
async function saveClusterBinding(){
  if(!PROJECT) await loadProjectConfig();
  const data=JSON.parse(JSON.stringify((PROJECT&&PROJECT.site_config)||{site:'ESS Site',clusters:[]}));
  if(!Array.isArray(data.clusters)) data.clusters=[];
  const name=String($('clusterCfgName').value||'').trim(); if(!name){ $('projectConfigResult').innerHTML='<span class="bad">Cluster name is required.</span>'; return; }
  let pmap=null;
  try{ pmap=selectedClusterPowerMap(); }catch(e){ $('projectConfigResult').innerHTML=`<span class="bad">${esc(e.message||e)}</span>`; return; }
  const _bms=getMultiSelectValues('clusterCfgBms'), _pcs=getMultiSelectValues('clusterCfgPcs'); const next={name, bms_devices:_bms, pcs_devices:_pcs, pcs_device:_pcs[0]||'', bms:_bms, pcs:_pcs};
  const old=data.clusters.find(x=>String(x.name)===name)||{};
  if(pmap===null){ if(old.power_map) next.power_map=old.power_map; }
  else { next.power_map=pmap; }
  const idx=data.clusters.findIndex(x=>String(x.name)===name); if(idx>=0) data.clusters[idx]=Object.assign({}, old, next); else data.clusters.push(next);
  await postJson('/api/site/config', data, 'projectConfigResult'); await loadProjectConfig(); await validateProjectConfig();
}
async function saveBmsConfig(){ const payload={name:$('bmsCfgName').value,host:$('bmsCfgHost').value,port:Number($('bmsCfgPort').value||502),unit_id:Number($('bmsCfgUnit').value||1),interval:Number($('bmsCfgInterval').value||2),profile:$('bmsCfgProfile').value}; await postJson('/api/project/bms/upsert', payload, 'projectConfigResult'); await loadProjectConfig(); }
async function removeBmsByName(name){ if(!name || !confirm(`Remove BMS ${name}? This also removes it from cluster binding and power map references.`)) return; await postJson('/api/project/bms/remove', {name}, 'projectConfigResult'); await loadProjectConfig(); await validateProjectConfig(); }
async function removeBmsConfig(){ const name=$('bmsCfgName').value; await removeBmsByName(name); }
async function savePcsConfig(){ const payload={name:$('pcsCfgName').value,host:$('pcsCfgHost').value,port:Number($('pcsCfgPort').value||502),unit_id:Number($('pcsCfgUnit').value||1),profile:$('pcsCfgProfile').value,enabled:true}; await postJson('/api/project/pcs/upsert', payload, 'projectConfigResult'); await loadProjectConfig(); }
async function removePcsByName(name){ if(!name || !confirm(`Remove PCS ${name}? This also removes it from cluster binding and power map references.`)) return; await postJson('/api/project/pcs/remove', {name}, 'projectConfigResult'); await loadProjectConfig(); await validateProjectConfig(); }
async function removePcsConfig(){ const name=$('pcsCfgName').value; await removePcsByName(name); }
function downloadProjectConfig(){ const text=JSON.stringify(PROJECT||{}, null, 2); const a=document.createElement('a'); a.href=URL.createObjectURL(new Blob([text], {type:'application/json'})); a.download='ess_aio_project_config_export.json'; a.click(); URL.revokeObjectURL(a.href); }
async function validateProjectConfig(){ const box=$('projectValidationBox'); if(box) box.textContent='Validating...'; try{ const r=await fetch('/api/project/validate',{cache:'no-store'}); const data=await r.json(); if(box) box.textContent=JSON.stringify(data,null,2); const target=$('projectConfigResult'); if(target) target.innerHTML=data.ok?'<span class="ok">Project validation passed.</span>':`<span class="bad">Project validation found ${esc((data.issues||[]).length)} error(s).</span>`; return data; }catch(e){ if(box) box.textContent=String(e); return {ok:false,error:String(e)}; } }


function pcsRows(){ const ds=(SNAP||{}).device_states||{}; return Object.values((ds.pcs||{})).sort((a,b)=>String(a.name).localeCompare(String(b.name))); }
function requireExecute(label){
  return confirm(`${label}\n\nThis may write to equipment or change dispatch state. Continue?`);
}
async function postJson(url, payload, targetId, method='POST'){
  const target = targetId ? $(targetId) : null;
  if(target) target.innerHTML = '<span class="muted">Sending...</span>';
  try{
    const r = await fetch(url, {method, headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload||{})});
    const data = await r.json();
    const ok = r.ok && data.ok !== false;
    const risk = data.risk ? ` risk=${esc(data.risk)}` : '';
    const html = `${ok ? '<span class="ok">OK</span>' : '<span class="bad">BLOCKED/ERROR</span>'} <code>${esc(data.command_id||data.status||r.status)}</code>${risk} ${esc(data.message||data.detail||data.error||'')}`;
    if(target) target.innerHTML = html;
    await refreshNow(true);
    return data;
  }catch(e){ if(target) target.innerHTML = `<span class="bad">${esc(e)}</span>`; return {ok:false,error:String(e)}; }
}

function decodeDatasetValue(v){ return String(v ?? '').trim(); }
function actionButton(el){
  // Robustly resolve dynamic action buttons. In some browsers the click target can
  // be a text node inside the button; Text does not have closest(), which made
  // Edit/Clear/Remove look like they did nothing.
  let node = el;
  if(node && node.nodeType === 3) node = node.parentElement;
  while(node && node !== document){
    if(node.matches && node.matches('button[data-action]')) return node;
    node = node.parentElement;
  }
  return null;
}
function actionClick(ev){
  // Central dispatcher for dynamically rendered table buttons. Only cancel the
  // browser event after a real action button is found; otherwise normal clicks
  // must continue to work.
  const btn = actionButton(ev ? ev.target : null);
  if(!btn) return true;
  try{
    if(ev){
      ev.preventDefault();
      ev.stopPropagation();
      if(ev.stopImmediatePropagation) ev.stopImmediatePropagation();
    }
    dispatchActionButton(btn).catch(err=>{
      console.error('Action button failed', err);
      const target=$('projectConfigResult') || $('pmEditorResult');
      if(target) target.innerHTML = `<span class="bad">Action failed: ${esc(err && err.message ? err.message : err)}</span>`;
    });
  }catch(err){ console.error('Action click failed', err); }
  return false;
}
async function dispatchActionButton(btn){
  const action=btn.dataset.action||'';
  const name=decodeDatasetValue(btn.dataset.name);
  const cluster=decodeDatasetValue(btn.dataset.cluster);
  const kind=decodeDatasetValue(btn.dataset.kind);
  const value=decodeDatasetValue(btn.dataset.value);
  if(action==='fill-bms') return fillBmsConfig(name);
  if(action==='remove-bms') return removeBmsByName(name);
  if(action==='fill-pcs') return fillPcsConfig(name);
  if(action==='remove-pcs') return removePcsByName(name);
  if(action==='add-bind-pick') return addBindPick(cluster, kind);
  if(action==='remove-bind-chip') return removeBindChip(cluster, kind, value);
  if(action==='edit-power-map') return editRowPowerMapByName(cluster);
  if(action==='auto-even-power-map') return autoEvenPowerMapForCluster(cluster);
  if(action==='clear-power-map') return clearPowerMapAndSave(cluster);
  if(action==='save-cluster-binding') return saveClusterBindingByName(cluster);
  if(action==='remove-cluster') return removeClusterBindingRow(cluster);
  console.warn('Unknown action button', action, btn);
}
document.addEventListener('click', function(ev){
  const btn=actionButton(ev.target);
  if(btn) actionClick(ev);
}, false);

function bmsRows(){ return (((SNAP||{}).device_states||{}).bms) ? Object.entries(SNAP.device_states.bms).map(([name,v])=>Object.assign({name},v||{})) : []; }
let RACK_DATA=null; let RACK_ACTIONS={};
function populateOpsBmsDevices(){ if(!SNAP) return; const sel=$('opsBmsDevice'); if(!sel) return; const old=sel.value; const bms=bmsRows().map(x=>x.name).sort(); sel.innerHTML=bms.map(n=>`<option value="${esc(n)}">${esc(n)}</option>`).join(''); if(bms.includes(old)) sel.value=old; renderBmsControl(); renderBmsPresetRegisters(); }
function opsBmsName(){ return $('opsBmsDevice') ? $('opsBmsDevice').value : ''; }
function bmsScope(){ return $('bmsControlScope') ? $('bmsControlScope').value : 'single'; }
function syncBmsWriteScope(){ if($('bmsWriteScope') && $('bmsControlScope')) $('bmsWriteScope').value=$('bmsControlScope').value; }
function bmsStartAll(){ postJson('/api/bms/start-all', {}, 'opsCommandResult'); }
function bmsStopAll(){ if(!confirm('Stop all BMS polling?')) return; postJson('/api/bms/stop-all', {}, 'opsCommandResult'); }
function bmsByName(device, action, target='opsCommandResult'){ if(!device) return; const url=action==='start'?'/api/bms/start':'/api/bms/stop'; postJson(url, {device}, target); }
function bmsSingle(action){ const device=opsBmsName(); return bmsByName(device, action, 'opsCommandResult'); }
function bmsHvOptions(){ return {timeout:parseFloat($('bmsHvTimeout')?.value||'30'), poll_interval:parseFloat($('bmsHvPoll')?.value||'1'), ignore_pcs_precheck: !!($('bmsHvIgnorePcsPrecheck')?.checked), confirm_text:'EXECUTE'}; }
function bmsHvByName(device, mode){ if(!device) return; const opts=bmsHvOptions(); const note=opts.ignore_pcs_precheck?' (BMS-only / ignore PCS precheck)':''; if(!requireExecute(`Send HV ${mode.toUpperCase()} workflow to ${device}${note}?`)) return; postJson('/api/bms/hv', {device, mode, ...opts}, 'opsCommandResult'); }
function bmsHv(mode){ const device=opsBmsName(); if(!device){ $('opsCommandResult').innerHTML='<span class="bad">Select a BMS first.</span>'; return; } return bmsHvByName(device, mode); }
function bmsHvAll(mode){ const opts=bmsHvOptions(); const note=opts.ignore_pcs_precheck?' (BMS-only / ignore PCS precheck)':''; if(!requireExecute(`Send HV ${mode.toUpperCase()} workflow to all online BMS${note}?`)) return; postJson('/api/bms/hv-all', {mode, ...opts}, 'opsCommandResult'); }
function bmsHvScoped(mode){ return bmsScope()==='single' ? bmsHv(mode) : bmsHvAll(mode); }
async function bmsHeartbeat(start){ const box=$('bmsHeartbeatStatus'); if(box) box.innerHTML=start?'<span class="ok">Heartbeat start command sent. Periodic heartbeat is being queued by Runtime.</span>':'<span class="warn">Heartbeat stop command sent.</span>'; const data=await postJson(start?'/api/bms/heartbeat/start-all':'/api/bms/heartbeat/stop-all', {}, 'opsCommandResult'); if(box) box.innerHTML=(data.ok!==false?(start?'<span class="ok">Heartbeat active / start acknowledged.</span>':'<span class="warn">Heartbeat stopped / stop acknowledged.</span>'):'<span class="bad">Heartbeat command failed.</span>')+' <code>'+esc(data.command_id||data.status||'')+'</code>'; }
function bmsClearFaultAll(){ if(!requireExecute('Clear fault on all online BMS?')) return; postJson('/api/bms/command', {scope:'all_online', command:'clear_fault', confirm_text:'EXECUTE'}, 'opsCommandResult'); }
function bmsCommandByName(device, command){ if(!device) return; if(command==='clear_fault' && !requireExecute(`Clear fault on ${device}?`)) return; postJson('/api/bms/command', {device, scope:'single', command, confirm_text:'EXECUTE'}, 'opsCommandResult'); }
function bmsHeartbeatByName(device){ if(!device) return; postJson('/api/bms/register-write', {device, scope:'single', address:0x0380, value:1, confirm_text:'EXECUTE'}, 'opsCommandResult'); }
function rackAction(rack,action){ RACK_ACTIONS[String(rack)]=action; renderRackRows(); previewRackMask(); }
function renderRackRows(){ const tb=$('rackSbmuRows'); if(!tb) return; const rows=(RACK_DATA&&RACK_DATA.racks)||[]; if(!rows.length){ tb.innerHTML='<tr><td colspan="12" class="muted">No rack data. Select a BMS and refresh.</td></tr>'; return; } tb.innerHTML=rows.map(r=>{ const act=RACK_ACTIONS[String(r.rack)]||'none'; const cls=act==='disable'?'bad':(act==='enable'?'ok':''); return `<tr><td>Rack ${esc(r.rack)}<br><code>${esc(r.base_address||'')}</code></td><td>${pill(String(r.online??'-'))}</td><td>${fmtMetric(r.soc_percent,'%',1)}</td><td>${fmtMetric(r.voltage_outside_v,' V',1)}</td><td>${fmtMetric(r.voltage_inside_v,' V',1)}</td><td>${fmtMetric(r.current_a,' A',1)}</td><td>${fmtMetric(r.power_kw,' kW',1)}</td><td>${fmtMetric(r.cell_voltage_sum_v,' V',1)}</td><td>${r.power_on_ready?'<span class="ok">Ready</span>':'<span class="muted">-</span>'}</td><td>${esc(r.positive_relay??'-')}/${esc(r.negative_relay??'-')}</td><td>${fmtMetric(r.max_temp_c,' ℃',1)} / ${fmtMetric(r.min_temp_c,' ℃',1)}</td><td><select class="${cls}" onchange="rackAction(${Number(r.rack)}, this.value)"><option value="none" ${act==='none'?'selected':''}>No change</option><option value="enable" ${act==='enable'?'selected':''}>Enable</option><option value="disable" ${act==='disable'?'selected':''}>Disable</option></select></td></tr>`; }).join(''); }
async function readRackSbmu(){ const device=opsBmsName(); if(!device){ if($('rackSbmuRaw')) $('rackSbmuRaw').textContent='Select a BMS first.'; return; } const count=parseInt($('rackSbmuCount')?.value||'16'); const data=await postJson('/api/bms/racks/read',{device,count},'rackSbmuRaw'); RACK_DATA=data; RACK_ACTIONS={}; renderRackRows(); previewRackMask(); if($('rackSbmuRaw')) $('rackSbmuRaw').textContent=JSON.stringify(data,null,2); }
function previewRackMask(){ const box=$('rackMaskPreview'); if(!box) return; const changes=Object.entries(RACK_ACTIONS).filter(([r,a])=>a&&a!=='none').map(([r,a])=>({rack:Number(r),action:a})); if(!changes.length){ box.innerHTML='No rack changes selected.'; return; } const cur=(RACK_DATA&&RACK_DATA.disable_masks)||{}; const targets={}; for(const a of ['0x038D','0x038E','0x038F']) targets[a]=Number(cur[a]??0); for(const ch of changes){ const addr='0x'+(0x038D+Math.floor((ch.rack-1)/16)).toString(16).toUpperCase().padStart(4,'0'); const bit=(ch.rack-1)%16; if(ch.action==='disable') targets[addr]|=(1<<bit); else targets[addr]&=~(1<<bit); }
  box.innerHTML=`Selected changes: ${esc(changes.map(c=>`Rack ${c.rack} ${c.action}`).join(', '))}<br>`+Object.keys(targets).map(a=>`${a}: current ${String(Number(cur[a]??0).toString(2)).padStart(16,'0')} → target ${String(Number(targets[a]??0).toString(2)).padStart(16,'0')}`).join('<br>'); }
async function applyRackMask(){ const device=opsBmsName(); const changes=Object.entries(RACK_ACTIONS).filter(([r,a])=>a&&a!=='none').map(([r,a])=>({rack:Number(r),action:a})); if(!device||!changes.length) return; if(!requireExecute(`Apply rack enable/disable mask to ${device}?\n${changes.map(c=>`Rack ${c.rack}: ${c.action}`).join('\n')}`)) return; const data=await postJson('/api/bms/racks/apply-mask',{device,changes,confirm_text:'EXECUTE'},'rackSbmuRaw'); if($('rackSbmuRaw')) $('rackSbmuRaw').textContent=JSON.stringify(data,null,2); await readRackSbmu(); }
async function bmsReadVersion(){ const device=opsBmsName(); if(!device){ $('bmsVersionResult').textContent='Select a BMS first.'; return; } const sbmu_count=parseInt($('bmsVersionSbmuCount')?.value||'1'); const r=await postJson('/api/bms/version', {device, sbmu_count}, 'bmsVersionResult'); if($('bmsVersionResult')) $('bmsVersionResult').textContent=JSON.stringify(r, null, 2); }
function bms038b(start){ postJson(start?'/api/bms/038b/start':'/api/bms/038b/stop', {}, 'opsCommandResult'); }
function parseAddrForApi(v){ const t=String(v||'').trim(); return t.toLowerCase().startsWith('0x') ? t : Number(t); }
function fillRtcNow(){ const d=new Date(); $('rtcYear').value=d.getFullYear(); $('rtcMonth').value=d.getMonth()+1; $('rtcDay').value=d.getDate(); $('rtcHour').value=d.getHours(); $('rtcMinute').value=d.getMinutes(); $('rtcSecond').value=d.getSeconds(); }
function bmsRegisterWrite(){ syncBmsWriteScope(); const scope=$('bmsWriteScope').value; const device=opsBmsName(); const address=parseAddrForApi($('bmsWriteAddress').value); const value=parseInt($('bmsWriteValue').value||'0'); if(scope==='single'&&!device) return; if(!requireExecute(`Write BMS register ${$('bmsWriteAddress').value}=${value} scope=${scope}?`)) return; postJson('/api/bms/register-write', {device, scope, address, value, confirm_text:'EXECUTE'}, 'opsCommandResult'); }
function bmsRtcWrite(){ syncBmsWriteScope(); const scope=$('bmsWriteScope').value; const device=opsBmsName(); if(scope==='single'&&!device) return; const payload={device, scope, year:+$('rtcYear').value, month:+$('rtcMonth').value, day:+$('rtcDay').value, hour:+$('rtcHour').value, minute:+$('rtcMinute').value, second:+$('rtcSecond').value, confirm_text:'EXECUTE'}; if(!requireExecute(`Write RTC to ${scope==='single'?device:'all online BMS'}?`)) return; postJson('/api/bms/rtc-write', payload, 'opsCommandResult'); }
function bmsQuickCommand(command){ syncBmsWriteScope(); const scope=bmsScope(); const device=opsBmsName(); if(scope==='single'&&!device) return; if(!requireExecute(`Send BMS command ${command} to ${scope==='single'?device:scope}?`)) return; postJson('/api/bms/command', {device, scope, command, confirm_text:'EXECUTE'}, 'opsCommandResult'); }
const BMS_PRESET_REGS=[
  ['0x0381','EMS command / HV request','1'],['0x038B','Insulation monitor disable','2'],['0x0380','EMS heartbeat manual value','1'],['0x0382','RTC year','2026'],['0x0383','RTC month','1'],['0x0384','RTC day','1'],['0x0385','RTC hour','0'],['0x0386','RTC minute','0'],['0x0387','RTC second','0'],['0x0388','Reserved control 0388','0'],['0x0389','Reserved control 0389','0'],['0x038A','Reserved control 038A','0'],['0x038C','Fault Clear cmd (pulse 1 then 0)','1'],['0x038D','Reserved control 038D','0'],['0x038E','Reserved control 038E','0'],['0x038F','Reserved control 038F','0'],['0x0390','Reserved control 0390','0'],['0x0391','Reserved control 0391','0'],['0x0392','Reserved control 0392','0'],['0x0393','Reserved control 0393','0'],['0x0394','Reserved control 0394','0']
];
function renderBmsPresetRegisters(){ const tb=$('bmsPresetRows'); if(!tb) return; tb.innerHTML=BMS_PRESET_REGS.map((r,i)=>`<tr><td><code>${r[0]}</code></td><td>${esc(r[1])}</td><td><input id="bmsPresetVal${i}" type="number" value="${esc(r[2])}" /></td><td><button onclick="setBmsManual('${r[0]}','bmsPresetVal${i}')">Use</button></td><td><button onclick="bmsPresetWrite('${r[0]}','bmsPresetVal${i}')">Write</button></td></tr>`).join(''); }
function setBmsManual(addr,inputId){ $('bmsWriteAddress').value=addr; $('bmsWriteValue').value=$(inputId).value; }
function bmsPresetWrite(addr,inputId){ $('bmsWriteAddress').value=addr; $('bmsWriteValue').value=$(inputId).value; bmsRegisterWrite(); }
function bmsMetric(vals, keys){ vals=vals||{}; for(const k of keys){ if(vals[k]!==undefined&&vals[k]!==null&&vals[k]!=='' ){ return vals[k]; } } return '-'; }
function renderBmsControl(){
  if(!SNAP) return;
  const rows=bmsRows();
  const online=rows.filter(d=>d.online || d.connection==='online').length;
  const statusCounts={normal:0,fullCharge:0,fullDischarge:0,warning:0,fault:0,unknown:0};
  rows.forEach(d=>{ const vals=d.latest_values||d.snapshot||{}; const label=bmsStatusFromDevice(d, vals); const l=String(label).toLowerCase(); if(l.includes('fault')) statusCounts.fault++; else if(l.includes('warning')) statusCounts.warning++; else if(l.includes('full charge')) statusCounts.fullCharge++; else if(l.includes('full discharge')) statusCounts.fullDischarge++; else if(l.includes('normal')) statusCounts.normal++; else statusCounts.unknown++; });
  if($('bmsControlCards')) $('bmsControlCards').innerHTML=[
    ['BMS Total',rows.length,''],['Online',online,online===rows.length?'ok':'warn'],['Normal',statusCounts.normal,'ok'],['Warning/Fault',statusCounts.warning+statusCounts.fault,(statusCounts.warning+statusCounts.fault)?'bad':'ok']
  ].map(([l,v,c])=>`<div class="card"><div class="label">${esc(l)}</div><div class="value ${c}">${esc(v)}</div></div>`).join('');
  if($('bmsControlCount')) $('bmsControlCount').textContent=`${rows.length} BMS`;
  if($('bmsControlRows')) $('bmsControlRows').innerHTML=rows.map(d=>{
    const vals=d.latest_values||d.snapshot||{};
    const isSel=$('opsBmsDevice') && $('opsBmsDevice').value===d.name;
    const soc=metricFromDevice(d, vals, ['soc','SOC','soc_value','system_soc']);
    const voltage=metricFromDevice(d, vals, ['voltage','system_voltage','total_voltage','dc_voltage','pack_voltage']);
    const current=metricFromDevice(d, vals, ['current','system_current','dc_current','pack_current']);
    const power=metricFromDevice(d, vals, ['power','system_power','active_power','dc_power','actual_power','power_kw']);
    const statusLabel=bmsStatusFromDevice(d, vals);
    const stClass=bmsStatusClass(statusLabel);
    const onlineRacks=metricFromDevice(d, vals, ['hv_online_racks','online_rack_count','rack_online_count','racks_online']);
    const totalRacks=metricFromDevice(d, vals, ['number_of_racks','rack_count','total_racks']);
    const rackText=(onlineRacks!=='-'||totalRacks!=='-')?`${onlineRacks}/${totalRacks}`:'-';
    return `<tr class="${isSel?'selected-row':''}" onclick="if($('opsBmsDevice')){$('opsBmsDevice').value='${esc(d.name)}'; renderBmsControl(); renderBmsPresetRegisters();}"><td>${esc(d.name)}</td><td>${pill(d.connection||'')}</td><td>${fmtMetric(soc,'%',1)}</td><td>${fmtMetric(voltage,' V',1)}</td><td>${fmtMetric(current,' A',1)}</td><td>${fmtMetric(power,' kW',1)}</td><td><span class="pill ${esc(stClass)}">${esc(statusLabel)}</span></td><td>${esc(rackText)}</td><td>${esc(d.updated_at||d.last_update||d.last_seen||'-')}</td><td>${esc(d.last_message||'')}</td><td><button onclick="event.stopPropagation(); bmsByName('${esc(d.name)}','start')">Connect</button> <button onclick="event.stopPropagation(); bmsByName('${esc(d.name)}','stop')">Disconnect</button> <button onclick="event.stopPropagation(); bmsHvByName('${esc(d.name)}','on')">HV ON</button> <button onclick="event.stopPropagation(); bmsHvByName('${esc(d.name)}','off')">HV OFF</button> <button onclick="event.stopPropagation(); bmsCommandByName('${esc(d.name)}','clear_fault')">Clear Fault</button> <button onclick="event.stopPropagation(); bmsHeartbeatByName('${esc(d.name)}')">Heartbeat</button></td></tr>`;
  }).join('') || '<tr><td colspan="10" class="muted">No BMS devices</td></tr>';
}
async function csvBms(start){ const device=opsBmsName(); const payload=device?{devices:[device]}:{devices:[]}; const data=await postJson(start?'/api/csv/bms/start':'/api/csv/bms/stop', payload, 'opsCommandResult'); $('opsCsvStatus').textContent=JSON.stringify(data.recording||data, null, 2); }
async function csvPcs(start){ const data=await postJson(start?'/api/csv/pcs/start':'/api/csv/pcs/stop', {devices:[]}, 'opsCommandResult'); $('opsCsvStatus').textContent=JSON.stringify(data.recording||data, null, 2); }
async function soakStart(){ const label=$('soakLabel').value||'field-soak'; const interval_s=parseFloat($('soakInterval').value||'60'); const data=await postJson('/api/soak/start', {label, interval_s}, 'opsCommandResult'); $('soakOpsBox').textContent=JSON.stringify(data, null, 2); }
async function soakStop(){ const data=await postJson('/api/soak/stop', {}, 'opsCommandResult'); $('soakOpsBox').textContent=JSON.stringify(data, null, 2); }
async function soakReport(){ try{ const r=await fetch('/api/soak/report', {cache:'no-store'}); $('soakOpsBox').textContent=JSON.stringify(await r.json(), null, 2); }catch(e){ $('soakOpsBox').textContent=String(e); } }
async function loadOperationLog(){ try{ const r=await fetch('/api/logs/operation/recent?max_lines=120', {cache:'no-store'}); const data=await r.json(); $('operationLogBox').textContent=(data.lines||[]).join('\n') || JSON.stringify(data,null,2); }catch(e){ $('operationLogBox').textContent=String(e); } }
function populatePcsSelected(){ if(!SNAP) return; const sel=$('pcsSelected'); if(!sel) return; const old=sel.value; const names=pcsRows().map(x=>x.name).filter(Boolean).sort(); sel.innerHTML=names.map(n=>`<option value="${esc(n)}">${esc(n)}</option>`).join(''); if(names.includes(old)) sel.value=old; renderPcsCards(); }
function selectedPcs(){ return $('pcsSelected') ? $('pcsSelected').value : ''; }
function renderPcsCards(){ const rows=pcsRows(); const online=rows.filter(d=>d.online || d.connection==='online').length; if($('pcsControlCards')) $('pcsControlCards').innerHTML=[['PCS Total',rows.length,''],['Online',online,online===rows.length?'ok':'warn'],['Running',((SNAP?.workers||{}).pcs_running||[]).length,'accent'],['Errors',rows.filter(d=>d.error||d.errors).length,'bad']].map(([l,v,c])=>`<div class="card"><div class="label">${esc(l)}</div><div class="value ${c}">${esc(v)}</div></div>`).join(''); }
function pcsStateLabel(vals, d, keys){ const v=bmsMetric(vals, keys); if(v==='-'||v===undefined||v===null||v==='') return d.status||'-'; const n=Number(v); if(Number.isFinite(n)){ if(n===0) return 'Open/Off'; if(n===1) return 'Closed/On'; return String(v); } return String(v); }
function renderPCS(){ if(!SNAP) return; populatePcsSelected(); const rows=pcsRows(); $('pcsCount').textContent=`${rows.length} PCS`; $('pcsRows').innerHTML=rows.map(d=>{ const vals=d.latest_values||d.snapshot||{}; const isSel=selectedPcs()===d.name; const ac=pcsStateLabel(vals,d,['ac_breaker_status','ac_contactor_status','grid_contactor_status','ac_relay_status']); const dc=pcsStateLabel(vals,d,['dc_breaker_status','dc_contactor_status','dc_relay_status','dc_breaker_closed']); const run=pcsStateLabel(vals,d,['run_status','work_status','running_status','pcs_running','power_on_status']); const p=fmtMetric(bmsMetric(vals,['active_power','actual_power','power','power_kw','p_kw']), ' kW', 1); const q=fmtMetric(bmsMetric(vals,['reactive_power','actual_reactive_power','q','q_kvar','reactive_power_kvar','capacitive_reactive_power','inductive_reactive_power']), ' kvar', 1); return `<tr class="${isSel?'selected-row':''}" onclick="if($('pcsSelected')){$('pcsSelected').value='${esc(d.name)}'; renderPCS();}"><td>${esc(d.name)}</td><td>${pill(d.connection)}</td><td>${esc(d.status||'')}</td><td>${esc(ac)}</td><td>${esc(dc)}</td><td>${esc(run)}</td><td>${p}</td><td>${q}</td><td>${esc(d.last_message||'')}</td><td><button onclick="event.stopPropagation(); pcsSingle('${esc(d.name)}','connect')">Connect Comm</button> <button onclick="event.stopPropagation(); pcsSingle('${esc(d.name)}','stop')">Disconnect Comm</button> <button onclick="event.stopPropagation(); pcsOneCommand('${esc(d.name)}','start')">PCS Start</button> <button onclick="event.stopPropagation(); pcsOneCommand('${esc(d.name)}','stop')">PCS Stop</button> <button onclick="event.stopPropagation(); pcsOneCommand('${esc(d.name)}','close_dc_breaker')">Close DC</button> <button onclick="event.stopPropagation(); pcsOneCommand('${esc(d.name)}','open_dc_breaker')">Open DC</button></td></tr>`}).join('') || '<tr><td colspan="10" class="muted">No PCS devices</td></tr>'; renderPcsCards(); }

function renderPcsAlarms(){
  const tb=$('pcsAlarmRows'); if(!tb) return;
  const rows=pcsRows().flatMap(d=>{
    const vals=d.latest_values||d.snapshot||{}; const items=[];
    if(d.error||d.errors) items.push({name:d.name,severity:'alarm',status:d.status||d.connection||'',message:d.last_message||d.error||`${d.errors} error(s)`});
    for(const [k,v] of Object.entries(vals||{})){ const kl=String(k).toLowerCase(); if((kl.includes('alarm')||kl.includes('fault')||kl.includes('warning')) && String(v)!=='0' && String(v)!=='false' && String(v)!=='') items.push({name:d.name,severity:kl.includes('alarm')||kl.includes('fault')?'alarm':'warning',status:k,message:String(v)}); }
    return items;
  });
  tb.innerHTML=rows.map(a=>`<tr><td>${esc(a.name)}</td><td>${esc(a.severity)}</td><td>${esc(a.status)}</td><td>${esc(a.message)}</td></tr>`).join('') || '<tr><td colspan="4" class="muted">No PCS alarm/fault data detected.</td></tr>';
}
function pcsConnectAll(){ postJson('/api/pcs/connect-all', {}, 'pcsCommandResult'); }
function pcsStopAll(){ postJson('/api/pcs/stop-all', {}, 'pcsCommandResult'); }
function pcsFleetCommand(method){ if(!requireExecute(`Send ${method} to all online PCS?`)) return; postJson('/api/pcs/fleet-command', {method, confirm_text:'EXECUTE'}, 'pcsCommandResult'); }
function pcsFleetPower(method,inputId){ const value=parseFloat($(inputId).value||'0'); if(!requireExecute(`Send ${method}=${value} to fleet?`)) return; postJson('/api/pcs/fleet-command', {method,value, confirm_text:'EXECUTE'}, 'pcsCommandResult'); }
function pcsSingle(pcs, action, target='pcsCommandResult'){ const url= action==='connect' ? '/api/pcs/connect' : '/api/pcs/stop'; postJson(url, {pcs}, target); }
function pcsSingleSelected(action){ const pcs=selectedPcs(); if(!pcs) return; pcsSingle(pcs, action); }
function pcsOneCommand(pcs, method){ if(!requireExecute(`Send ${method} to ${pcs}?`)) return; postJson('/api/pcs/command', {pcs, method, confirm_text:'EXECUTE'}, 'pcsCommandResult'); }
function pcsOneCommandSelected(method){ const pcs=selectedPcs(); if(!pcs) return; pcsOneCommand(pcs, method); }
function pcsPowerSelected(method,inputId){ const pcs=selectedPcs(); if(!pcs) return; const value=parseFloat($(inputId).value||'0'); if(!requireExecute(`Send ${method}=${value} to ${pcs}?`)) return; postJson('/api/pcs/command', {pcs, method, value, confirm_text:'EXECUTE'}, 'pcsCommandResult'); }
function pcsCustomSelected(){ const pcs=selectedPcs(); const method=($('pcsCustomMethod').value||'').trim(); if(!pcs||!method) return; const raw=($('pcsCustomValue').value||'').trim(); const payload={pcs, method, confirm_text:'EXECUTE'}; if(raw!=='') payload.value=parseFloat(raw); if(!requireExecute(`Send custom PCS command ${method} to ${pcs}?`)) return; postJson('/api/pcs/command', payload, 'pcsCommandResult'); }
let STRATEGY_CENTER=null;
function populateStrategyClusters(){ if(!SNAP) return; const sel=$('strategyCluster'); const old=sel.value; const clusters=(SNAP.clusters||[]).map(c=>c.name).sort(); sel.innerHTML=clusters.map(n=>`<option value="${esc(n)}">${esc(n)}</option>`).join(''); if(clusters.includes(old)) sel.value=old; renderStrategy(); loadStrategyCenter(); }
function populateStrategyClustersOnce(){ const sel=$('strategyCluster'); if(!sel || sel.options.length || !SNAP) return; const clusters=(SNAP.clusters||[]).map(c=>c.name).sort(); sel.innerHTML=clusters.map(n=>`<option value="${esc(n)}">${esc(n)}</option>`).join(''); }
async function loadStrategyCenter(){ try{ const r=await fetch('/api/strategy-center',{cache:'no-store'}); STRATEGY_CENTER=await r.json(); renderStrategy(); }catch(e){ const box=$('strategyIssuesBox'); if(box) box.textContent=String(e); } }
function renderStrategy(){ if(!SNAP) return; populateStrategyClustersOnce(); const running=new Set(((SNAP.workers||{}).strategies)||[]); const sc=STRATEGY_CENTER||{}; const rows=(sc.clusters||SNAP.clusters||[]); if($('strategyCards')){ const prof=sc.profile_strategy||{}; $('strategyCards').innerHTML=[['Profile', prof.enabled===false?'Disabled':'Enabled', prof.enabled===false?'warn':'ok'],['Running', sc.strategies_running??running.size, 'accent'],['Ready clusters', `${sc.clusters_ready??'-'}/${sc.clusters_total??rows.length}`, ''],['Issues', (sc.issues||[]).length, (sc.issues||[]).length?'warn':'ok']].map(([l,v,c])=>`<div class="card"><div class="label">${esc(l)}</div><div class="value ${c}">${esc(v)}</div></div>`).join(''); } if($('strategyIssuesBox')){ $('strategyIssuesBox').textContent=JSON.stringify({profile:sc.profile_strategy||{}, issues:sc.issues||[], safety_note:sc.safety_note||''}, null, 2); } $('strategyRows').innerHTML=rows.map(c=>{ const name=c.cluster||c.name||''; return `<tr><td>${esc(name)}</td><td>${pill(c.health||'ready')}</td><td>${esc((c.bms_devices||[]).join(', '))}</td><td>${esc((c.pcs_devices||[]).join(', '))}</td><td>${esc(c.allocation_mode||'')}</td><td><code>${esc(JSON.stringify(c.power_map||{}))}</code></td><td>${(c.running||running.has(name))?pill('running'):pill('stopped')}</td></tr>` }).join('') || '<tr><td colspan="7" class="muted">No clusters</td></tr>'; if($('strategyCommandRows')){ $('strategyCommandRows').innerHTML=(sc.recent_strategy_commands||[]).map(c=>`<tr><td>${fmtTs(c.updated_ts||c.created_ts)}</td><td>${esc(c.name)}</td><td>${esc(c.risk||'low')}</td><td>${pill(c.status)}</td><td>${esc(c.ok)}</td><td>${esc(c.message||'')}</td></tr>`).join('') || '<tr><td colspan="6" class="muted">No strategy commands</td></tr>'; } }
function strategyClusterName(){ return $('strategyCluster').value; }
async function loadStrategyConfig(){ const box=$('strategyConfigResult'); if(box) box.textContent='Loading...'; try{ const r=await fetch('/api/strategy/config',{cache:'no-store'}); const data=await r.json(); $('strategyConfigEditor').value=JSON.stringify(data.config||{}, null, 2); if(box) box.innerHTML=data.ok?`<span class="ok">Loaded ${esc(data.path||'strategy.json')}</span>`:`<span class="bad">${esc(data.error||'load failed')}</span>`; }catch(e){ if(box) box.innerHTML=`<span class="bad">${esc(e)}</span>`; } }
async function saveStrategyConfig(){ const box=$('strategyConfigResult'); try{ const config=JSON.parse($('strategyConfigEditor').value||'{}'); await postJson('/api/strategy/config', {config}, 'strategyConfigResult', 'PUT'); await loadStrategyCenter(); }catch(e){ if(box) box.innerHTML=`<span class="bad">Invalid JSON: ${esc(e)}</span>`; } }
async function strategyApplySettings(){ const cluster=strategyClusterName(); if(!cluster) return; const payload={cluster, mode:$('strategyMode').value, target_power_kw:parseFloat($('strategyTargetPower').value||'0'), ramp_step_kw:parseFloat($('strategyRampStep').value||'50'), ramp_interval_s:parseFloat($('strategyRampInterval').value||'5'), bms_timeout_s:parseFloat($('strategyTimeout').value||'5')}; await postJson('/api/cluster/strategy-settings', payload, 'strategyCommandResult'); await loadStrategyCenter(); }
async function strategyStart(){ const cluster=strategyClusterName(); if(!cluster) return; await strategyApplySettings(); if(!requireExecute(`Start strategy for ${cluster}?`)) return; await postJson('/api/strategy/start', {cluster, confirm_text:'EXECUTE'}, 'strategyCommandResult'); await loadStrategyCenter(); }
async function strategyStop(){ const cluster=strategyClusterName(); if(!cluster) return; if(!confirm(`Stop strategy for ${cluster}?`)) return; await postJson('/api/strategy/stop', {cluster}, 'strategyCommandResult'); await loadStrategyCenter(); }
async function strategyStartAll(){ if(!requireExecute('Start strategy for all configured clusters?')) return; await postJson('/api/strategy/start-all', {confirm_text:'EXECUTE'}, 'strategyCommandResult'); await loadStrategyCenter(); }
async function strategyStopAll(){ if(!confirm('Stop strategy for all configured clusters?')) return; await postJson('/api/strategy/stop-all', {}, 'strategyCommandResult'); await loadStrategyCenter(); }
function renderOverview(){
  if(!SNAP) return;
  const summary=SNAP.summary||{};
  if($('cards')) $('cards').innerHTML=cardsHtml(summary,SNAP);
  const alarms=overviewAlarmItems();
  const workers=SNAP.workers||{};
  const bmsTotal=Number(summary.bms_total||0), bmsOnline=Number(summary.bms_online||0);
  const pcsTotal=Number(summary.pcs_total||0), pcsOnline=Number(summary.pcs_online||0);
  const alarmCount=alarms.filter(a=>String(a.severity||'').toLowerCase().includes('alarm')).length;
  const warnCount=alarms.filter(a=>String(a.severity||'').toLowerCase().includes('warn')).length;
  const siteState = alarmCount ? 'Fault' : (warnCount ? 'Warning' : (bmsOnline+pcsOnline>0 ? 'Running' : 'Standby'));
  const stateClass = siteState==='Fault'?'bad':(siteState==='Warning'?'warn':(siteState==='Running'?'ok':''));
  const dashboardHtml = `
    <div class="dashboard-cards">
      <div class="metric-card hero"><div class="metric-label">Site state</div><div class="metric-value ${stateClass}">${esc(siteState)}</div><div class="metric-foot">Uptime ${esc(SNAP.uptime_s||0)}s</div></div>
      <div class="metric-card"><div class="metric-label">BMS online</div><div class="metric-value ${bmsOnline===bmsTotal?'ok':'warn'}">${esc(bmsOnline)}/${esc(bmsTotal)}</div><div class="metric-foot">Running ${(workers.bms_running||[]).length}</div></div>
      <div class="metric-card"><div class="metric-label">PCS online</div><div class="metric-value ${pcsOnline===pcsTotal?'ok':'warn'}">${esc(pcsOnline)}/${esc(pcsTotal)}</div><div class="metric-foot">Running ${(workers.pcs_running||[]).length}</div></div>
      <div class="metric-card"><div class="metric-label">Active issues</div><div class="metric-value ${alarms.length?'bad':'ok'}">${esc(alarms.length)}</div><div class="metric-foot">Alarm ${alarmCount} · Warning ${warnCount}</div></div>
      <div class="metric-card"><div class="metric-label">CSV</div><div class="metric-value">${esc(csvStatusText())}</div><div class="metric-foot">Recorder status</div></div>
      <div class="metric-card"><div class="metric-label">Strategy</div><div class="metric-value accent">${esc((workers.strategies||[]).length)}</div><div class="metric-foot">Active strategy workers</div></div>
    </div>
    <div class="dashboard-charts">
      <div class="chart-card"><div class="chart-title">Device online distribution</div>${dashboardBar('BMS online', bmsOnline, bmsTotal, 'ok')}${dashboardBar('PCS online', pcsOnline, pcsTotal, 'accent')}</div>
      <div class="chart-card"><div class="chart-title">Alarm / warning distribution</div>${dashboardSeverityBar('Alarm', alarmCount, Math.max(alarms.length,1), 'bad')}${dashboardSeverityBar('Warning', warnCount, Math.max(alarms.length,1), 'warn')}${dashboardSeverityBar('Normal', Math.max((bmsTotal+pcsTotal)-alarms.length,0), Math.max(bmsTotal+pcsTotal,1), 'ok')}</div>
      <div class="chart-card"><div class="chart-title">Runtime activity</div>${dashboardSeverityBar('BMS workers', (workers.bms_running||[]).length, Math.max(bmsTotal,1), 'ok')}${dashboardSeverityBar('PCS workers', (workers.pcs_running||[]).length, Math.max(pcsTotal,1), 'accent')}${dashboardSeverityBar('Strategies', (workers.strategies||[]).length, Math.max((SNAP.clusters||[]).length,1), 'warn')}</div>
    </div>`;
  if($('overviewDashboard')) $('overviewDashboard').innerHTML=dashboardHtml;
  if($('overviewAlarmRows')) $('overviewAlarmRows').innerHTML=alarms.slice(0,12).map(a=>`<tr><td>${esc(a.severity||'')}</td><td>${esc(a.area||a.kind||'')}</td><td>${esc(a.device||'')}</td><td>${esc(a.message||a.key||a.status||'')}</td></tr>`).join('') || '<tr><td colspan="4" class="muted">No active runtime alarms or issues.</td></tr>';
  if($('runtimeSummary')) $('runtimeSummary').textContent=JSON.stringify({api_schema:SNAP.api_schema, uptime_s:SNAP.uptime_s, workers:SNAP.workers, summary:SNAP.summary, recording:SNAP.recording}, null, 2);
  if($('soakSummary')) $('soakSummary').textContent=JSON.stringify(SNAP.soak_test||{}, null, 2);
  renderOverviewTrendCards();
}
function sparklineSvg(series){ const data=(series?.data||[]).slice(-80); if(!data.length) return '<div class="muted">No samples yet</div>'; const ys=data.map(p=>Number(p.y)).filter(Number.isFinite); const mn=Math.min(...ys), mx=Math.max(...ys); const span=(mx-mn)||1; const pts=data.map((p,i)=>`${(i/Math.max(data.length-1,1)*100).toFixed(1)},${(28-((Number(p.y)-mn)/span)*24).toFixed(1)}`).join(' '); return `<svg viewBox="0 0 100 32" preserveAspectRatio="none" style="width:100%;height:70px"><polyline points="${pts}" fill="none" stroke="currentColor" stroke-width="2"/></svg><div class="muted">min ${esc(mn)} · max ${esc(mx)} · last ${esc(ys[ys.length-1])}</div>`; }
async function renderOverviewTrendCards(){ const box=$('overviewTrendCards'); if(!box) return; try{ const [st,rk]=await Promise.all([fetch('/api/curves/live?signal=bms_status&device_type=bms&multi=true&limit=120',{cache:'no-store'}).then(r=>r.json()), fetch('/api/curves/live?signal=rack_count&device_type=bms&multi=true&limit=120',{cache:'no-store'}).then(r=>r.json())]); const cards=[]; for(const s of (st.series||[]).slice(0,6)) cards.push(`<div class="chart-card"><div class="chart-title">${esc(s.name)}</div>${sparklineSvg(s)}</div>`); for(const s of (rk.series||[]).slice(0,6)) cards.push(`<div class="chart-card"><div class="chart-title">${esc(s.name)}</div>${sparklineSvg(s)}</div>`); box.innerHTML=cards.join('') || '<div class="muted">No BMS status/rack-count samples yet.</div>'; }catch(e){ box.innerHTML=`<span class="bad">Trend load failed: ${esc(e)}</span>`; }
}
function csvStatusText(){ const r=SNAP?.recording||{}; const on=[]; if((r.bms_csv||[]).length) on.push('BMS'); if((r.pcs_csv||[]).length) on.push('PCS'); return on.length?on.join('+'):'idle'; }
function overviewAlarmItems(){ const out=[]; const ds=(SNAP?.device_states)||{}; for(const [kind,map] of Object.entries({bms:ds.bms||{}, pcs:ds.pcs||{}})){ for(const [name,d] of Object.entries(map||{})){ if(d.error||d.errors){ out.push({severity:'alarm',area:kind,device:name,message:d.last_message||d.error||`${d.errors} error(s)`}); } if(d.online===false||d.connection==='offline'){ out.push({severity:'warning',area:kind,device:name,message:'offline'}); } } } return out; }
function renderDevices(){ if(!SNAP) return; const q=($('deviceFilter')?.value||'').toLowerCase(); const tf=$('typeFilter')?.value||'all'; const rows=deviceRows().filter(d=>(tf==='all'||d._type===tf) && (!q || String(d.name).toLowerCase().includes(q) || String(d.connection).toLowerCase().includes(q) || String(d.last_message).toLowerCase().includes(q))); $('devices').innerHTML=rows.map(d=>{ const isSel=SELECTED_DEVICE.kind===d._type && SELECTED_DEVICE.name===d.name; return `<tr class="clickable-row ${isSel?'selected-row':''}" onclick="selectDevice('${esc(d._type)}','${esc(d.name)}')"><td>${esc(d._type)}</td><td>${esc(d.name)}</td><td>${pill(d.connection)}</td><td>${esc(d.status)}</td><td>${esc(d.errors||0)}</td><td>${esc(d.last_latency_ms||0)}</td><td>${esc(d.last_message||'')}</td></tr>`; }).join('') || '<tr><td colspan="7" class="muted">No devices</td></tr>'; $('deviceCount').textContent=`${rows.length} rows`; }
async function selectDevice(kind,name){ SELECTED_DEVICE={kind,name}; renderDevices(); if($('selectedDeviceTitle')) $('selectedDeviceTitle').textContent=`${kind} ${name}`; if($('deviceSnapshot')) $('deviceSnapshot').textContent='Loading...'; try{ const r=await fetch(`/api/device/${kind.toLowerCase()}/${encodeURIComponent(name)}/snapshot`, {cache:'no-store'}); if($('deviceSnapshot')) $('deviceSnapshot').textContent=JSON.stringify(await r.json(), null, 2); }catch(e){ if($('deviceSnapshot')) $('deviceSnapshot').textContent=String(e); } }
function populateAlarmDevices(){ if(!SNAP) return; const sel=$('alarmDevice'); const old=sel.value; const bms=Object.keys(((SNAP.device_states||{}).bms)||{}).sort(); sel.innerHTML=bms.map(n=>`<option value="${esc(n)}">${esc(n)}</option>`).join(''); if(bms.includes(old)) sel.value=old; }
async function loadAlarms(){ const name=$('alarmDevice').value; if(!name){ $('alarms').innerHTML='<tr><td colspan="3" class="muted">No BMS selected</td></tr>'; return; } $('alarms').innerHTML='<tr><td colspan="3" class="muted">Loading...</td></tr>'; try{ const r=await fetch(`/api/device/bms/${encodeURIComponent(name)}/alarms`, {cache:'no-store'}); const data=await r.json(); const active=(data.alarms||[]).filter(a=>(a.active||[]).length); $('alarms').innerHTML=(active.length?active:(data.alarms||[]).slice(0,32)).map(a=>`<tr><td>${esc(a.address)}</td><td>${esc(a.raw)}</td><td>${esc((a.active||[]).join('\n'))}</td></tr>`).join('') || '<tr><td colspan="3" class="muted">No alarm data</td></tr>'; }catch(e){ $('alarms').innerHTML=`<tr><td colspan="3" class="bad">${esc(e)}</td></tr>`; } }
function renderClusters(){ if(!SNAP) return; $('clusters').innerHTML=(SNAP.clusters||[]).map(c=>`<tr><td>${esc(c.name)}</td><td>${esc((c.bms_devices||[]).join(', '))}</td><td>${esc((c.pcs_devices||[]).join(', '))}</td><td>${esc(c.allocation_mode||'')}</td><td><code>${esc(JSON.stringify(c.power_map||{}))}</code></td></tr>`).join('') || '<tr><td colspan="5" class="muted">No clusters</td></tr>'; }
function renderCommands(){ if(!SNAP) return; $('commands').innerHTML=(SNAP.command_acks||[]).map(c=>`<tr><td>${fmtTs(c.updated_ts||c.created_ts)}</td><td><code>${esc(c.command_id)}</code></td><td>${esc(c.name)}</td><td>${esc(c.risk||'low')}</td><td>${esc(c.confirmed)}</td><td>${pill(c.status)}</td><td>${esc(c.ok)}</td><td>${esc(c.message||'')}</td></tr>`).join('') || '<tr><td colspan="8" class="muted">No commands</td></tr>'; }
async function loadCommandAudit(){ const box=$('commandAuditBox'); if(!box) return; box.textContent='Loading...'; try{ const r=await fetch('/api/commands/audit-summary',{cache:'no-store'}); box.textContent=JSON.stringify(await r.json(), null, 2); }catch(e){ box.textContent=String(e); } }
async function loadMetrics(){ try{ const r=await fetch('/api/runtime/metrics', {cache:'no-store'}); const data=await r.json(); if($('metricsBox')) $('metricsBox').textContent=JSON.stringify(data, null, 2); if($('settingsMetrics')) $('settingsMetrics').textContent=JSON.stringify(data, null, 2); }catch(e){ if($('metricsBox')) $('metricsBox').textContent=String(e); if($('settingsMetrics')) $('settingsMetrics').textContent=String(e); } }
async function loadRestorePlan(){ try{ const r=await fetch('/api/runtime/restore-plan', {cache:'no-store'}); $('restoreBox').textContent=JSON.stringify(await r.json(), null, 2); }catch(e){ $('restoreBox').textContent=String(e); } }
async function loadLogsStatus(){ try{ const r=await fetch('/api/logs/status', {cache:'no-store'}); const data=await r.json(); const old=$('settingsCsvLogs').textContent||''; $('settingsCsvLogs').textContent=`Logs:\n${JSON.stringify(data,null,2)}\n\n${old}`; }catch(e){ $('settingsCsvLogs').textContent=String(e); } }
async function loadCsvStatus(){ try{ const r=await fetch('/api/csv/status', {cache:'no-store'}); const data=await r.json(); if($('settingsCsvLogs')) $('settingsCsvLogs').textContent=`CSV:\n${JSON.stringify(data,null,2)}\n\n${$('settingsCsvLogs').textContent||''}`; if($('opsCsvStatus')) $('opsCsvStatus').textContent=JSON.stringify(data,null,2); }catch(e){ if($('settingsCsvLogs')) $('settingsCsvLogs').textContent=String(e); if($('opsCsvStatus')) $('opsCsvStatus').textContent=String(e); } }
function renderSettings(){ if(!SNAP) return; if($('settingsSchema')) $('settingsSchema').textContent=SNAP.api_schema||'-'; if($('settingsPid')) $('settingsPid').textContent=((SNAP.runtime||{}).pid || SNAP.pid || '-'); if($('settingsUptime')) $('settingsUptime').textContent=(SNAP.uptime_s||0)+'s'; if(SNAP.metrics && $('settingsMetrics') && $('settingsMetrics').textContent==='-') $('settingsMetrics').textContent=JSON.stringify(SNAP.metrics,null,2); }
async function loadRuntimeSettings(){
  renderSettings();
  const target=$('runtimeSettingsResult'); if(target) target.textContent='Loading runtime settings...';
  try{
    const r=await fetch('/api/runtime/settings',{cache:'no-store'}); const data=await r.json();
    if(target) target.innerHTML=data.ok?`<span class="ok">Loaded from ${esc(data.path||'-')}</span>`:`<span class="bad">${esc(data.error||'failed')}</span>`;
    const rows=(data.rows||[]).map(row=>{
      const key=String(row.key||''); const typ=String(row.type||'string'); const editable=!!row.editable;
      let input='';
      if(editable){
        if(typ==='boolean') input=`<select data-runtime-key="${esc(key)}"><option value="true" ${row.value===true?'selected':''}>true</option><option value="false" ${row.value===false?'selected':''}>false</option></select>`;
        else input=`<input data-runtime-key="${esc(key)}" value="${esc(row.value??'')}" />`;
      } else input=`<code>${esc(row.value??'')}</code>`;
      return `<tr><td><code>${esc(key)}</code></td><td>${input}</td><td>${esc(row.source||'')}</td><td>${editable?'yes':'no'}</td><td>${row.restart_required?'required':'no'}</td><td class="muted">${esc(row.description||'')}</td></tr>`;
    }).join('') || '<tr><td colspan="6" class="muted">No runtime settings found.</td></tr>';
    if($('runtimeSettingsRows')) $('runtimeSettingsRows').innerHTML=rows;
  }catch(e){ if(target) target.innerHTML=`<span class="bad">${esc(e)}</span>`; }
}
async function saveRuntimeSettingsFromTable(){
  const settings={};
  document.querySelectorAll('[data-runtime-key]').forEach(el=>{ const key=el.getAttribute('data-runtime-key'); settings[key]=el.value; });
  if(!confirm('Save editable runtime settings? Some values may require Runtime restart.')) return;
  await postJson('/api/runtime/settings', {settings}, 'runtimeSettingsResult');
  await loadRuntimeSettings();
}

function powerMapProjectClusters(){
  return ((PROJECT && PROJECT.site_config && PROJECT.site_config.clusters) || []).map((c,idx)=>{
    const name=String(c.name||`Cluster-${idx+1}`);
    const bms=normalizedClusterBms(c);
    const pcs=normalizedClusterPcs(c);
    const pmap=normalizePowerMap(c.power_map||{}, bms, pcs);
    return {name,bms,pcs,power_map:pmap, raw:c};
  });
}
async function loadPowerMapEditor(){
  try{
    const r=await fetch('/api/project/config',{cache:'no-store'});
    PROJECT=await r.json();
    const clusters=powerMapProjectClusters();
    const sel=$('pmClusterSelect');
    if(sel){
      const old=sel.value;
      sel.innerHTML=clusters.map(c=>`<option value="${esc(c.name)}">${esc(c.name)}</option>`).join('');
      if(clusters.some(c=>c.name===old)) sel.value=old;
    }
    renderPowerMapEditorForSelected();
  }catch(e){ if($('pmEditorResult')) $('pmEditorResult').innerHTML=`<span class="bad">${esc(e)}</span>`; }
}
function selectedPowerMapCluster(){
  const name=String($('pmClusterSelect')?.value||'');
  return powerMapProjectClusters().find(c=>c.name===name) || powerMapProjectClusters()[0] || null;
}
function renderPowerMapEditorForSelected(){
  const c=selectedPowerMapCluster();
  const head=$('pmEditorHead'), rows=$('pmEditorRows'), summary=$('pmEditorSummary');
  if(!head || !rows) return;
  if(!c){
    head.innerHTML=''; rows.innerHTML='<tr><td class="muted">No clusters configured. Add clusters and bind BMS/PCS in Project → Cluster Binding first.</td></tr>';
    if(summary) summary.textContent='No cluster.';
    return;
  }
  if(summary) summary.textContent=`${c.name}: ${c.pcs.length} PCS × ${c.bms.length} BMS`;
  if(!c.bms.length || !c.pcs.length){
    head.innerHTML=''; rows.innerHTML='<tr><td class="warn">Bind at least one BMS and one PCS before editing Power Map.</td></tr>';
    return;
  }
  const pm=normalizePowerMap(c.power_map||{}, c.bms, c.pcs);
  head.innerHTML=`<tr><th>PCS \ BMS</th>${c.bms.map(b=>`<th>${esc(b)}</th>`).join('')}<th>Row Sum</th></tr>`;
  rows.innerHTML=c.pcs.map(pc=>{
    const row=pm[pc]||{};
    const sum=c.bms.reduce((a,b)=>a+Number(row[b]||0),0);
    return `<tr data-pm-pcs="${esc(pc)}"><td><b>${esc(pc)}</b></td>${c.bms.map(b=>`<td><input class="pm-weight" data-pm-bms="${esc(b)}" value="${esc(Number(row[b]??0).toFixed(6).replace(/\.0+$/,'').replace(/(\.\d*?)0+$/,'$1'))}" onfocus="touchPowerMapEditor()" oninput="updatePowerMapRowSums()" /></td>`).join('')}<td class="pm-row-sum ${Math.abs(sum-1)<=0.001?'oksum':'badsum'}">${sum.toFixed(6)}</td></tr>`;
  }).join('');
  updatePowerMapRowSums();
}
function touchPowerMapEditor(){ touchClusterBindingEditor(90000); }
function collectPowerMapEditor(){
  const c=selectedPowerMapCluster();
  if(!c) return null;
  const out={};
  document.querySelectorAll('#pmEditorRows tr[data-pm-pcs]').forEach(tr=>{
    const pc=tr.getAttribute('data-pm-pcs');
    const row={};
    tr.querySelectorAll('input[data-pm-bms]').forEach(inp=>{
      const bm=inp.getAttribute('data-pm-bms');
      const n=Number(inp.value);
      if(Number.isFinite(n) && n>=0) row[bm]=n;
    });
    out[pc]=row;
  });
  return {cluster:c, power_map:normalizePowerMap(out, c.bms, c.pcs)};
}
function updatePowerMapRowSums(){
  document.querySelectorAll('#pmEditorRows tr[data-pm-pcs]').forEach(tr=>{
    let sum=0;
    tr.querySelectorAll('input[data-pm-bms]').forEach(inp=>{ const n=Number(inp.value); if(Number.isFinite(n)) sum+=n; });
    const cell=tr.querySelector('.pm-row-sum');
    if(cell){ cell.textContent=sum.toFixed(6); cell.classList.toggle('oksum', Math.abs(sum-1)<=0.001); cell.classList.toggle('badsum', Math.abs(sum-1)>0.001); }
  });
}
function powerMapAutoEvenSelected(){
  const c=selectedPowerMapCluster(); if(!c) return;
  if(!c.bms.length || !c.pcs.length){ alert('Bind BMS and PCS first.'); return; }
  const share=Number((1/c.bms.length).toFixed(6));
  document.querySelectorAll('#pmEditorRows input[data-pm-bms]').forEach(inp=>{ inp.value=String(share); });
  updatePowerMapRowSums(); touchPowerMapEditor();
  if($('pmEditorResult')) $('pmEditorResult').innerHTML='<span class="ok">Auto Even generated. Click Save Power Map to persist it.</span>';
}
function powerMapNormalizeSelected(){
  document.querySelectorAll('#pmEditorRows tr[data-pm-pcs]').forEach(tr=>{
    const inputs=Array.from(tr.querySelectorAll('input[data-pm-bms]'));
    const vals=inputs.map(inp=>{ const n=Number(inp.value); return Number.isFinite(n)&&n>=0?n:0; });
    const sum=vals.reduce((a,b)=>a+b,0);
    if(sum>0){ inputs.forEach((inp,i)=>{ inp.value=String(Number((vals[i]/sum).toFixed(6))); }); }
  });
  updatePowerMapRowSums(); touchPowerMapEditor();
  if($('pmEditorResult')) $('pmEditorResult').innerHTML='<span class="ok">Rows normalized. Click Save Power Map to persist it.</span>';
}
async function savePowerMapEditor(){
  if(!PROJECT) await loadPowerMapEditor();
  const collected=collectPowerMapEditor();
  if(!collected){ alert('No cluster selected.'); return; }
  const sums=Object.fromEntries(Object.entries(collected.power_map||{}).map(([pc,row])=>[pc,Object.values(row||{}).reduce((a,b)=>a+Number(b||0),0)]));
  const bad=Object.entries(sums).filter(([_,sum])=>Math.abs(sum-1)>0.001);
  if(bad.length && !confirm('Some PCS rows do not sum to 1.000. Save anyway?')) return;
  const data=JSON.parse(JSON.stringify((PROJECT&&PROJECT.site_config)||{site:'ESS Site',clusters:[]}));
  data.clusters=(data.clusters||[]).map(c=>{
    const name=String(c.name||c.id||'');
    if(name!==collected.cluster.name) return c;
    const bms=normalizedClusterBms(c);
    const pcs=normalizedClusterPcs(c);
    return Object.assign({}, c, {bms_devices:bms, pcs_devices:pcs, pcs_device:pcs[0]||'', bms:bms, pcs:pcs, power_map:normalizePowerMap(collected.power_map, bms, pcs)});
  });
  await postJson('/api/site/config', data, 'pmEditorResult');
  CLUSTER_BINDING_DIRTY=false; CLUSTER_BINDING_INTERACTIVE_UNTIL=0;
  await loadPowerMapEditor();
  await loadPowerMapStatus();
}
async function loadPowerMapStatus(){
  const issues=$('powerMapIssues'); if(issues) issues.textContent='Loading...';
  try{
    const r=await fetch('/api/power-map/status',{cache:'no-store'}); const data=await r.json();
    if($('powerMapStatusRows')) $('powerMapStatusRows').innerHTML=(data.clusters||[]).map(c=>`<tr><td>${esc(c.cluster)}</td><td>${c.ready_for_dispatch?'<span class="ok">ready</span>':'<span class="warn">not ready</span>'}</td><td><code>${esc(JSON.stringify(c.pcs_weight_sums||{}))}</code></td><td><pre>${esc(JSON.stringify(c.power_map||{},null,2))}</pre></td></tr>`).join('') || '<tr><td colspan="4" class="muted">No clusters.</td></tr>';
    if(issues) issues.textContent=JSON.stringify({runtime_use:data.runtime_use, issues:data.issues||[]}, null, 2);
  }catch(e){ if(issues) issues.textContent=String(e); }
}
async function loadSiteConfig(){ $('siteConfigResult').textContent='Loading...'; try{ const r=await fetch('/api/site/config', {cache:'no-store'}); const data=await r.json(); $('siteConfigEditor').value=JSON.stringify(data.config||data, null, 2); $('siteConfigResult').innerHTML='<span class="ok">Loaded from runtime.</span>'; }catch(e){ $('siteConfigResult').innerHTML=`<span class="bad">${esc(e)}</span>`; } }
function downloadSiteConfig(){ const text=$('siteConfigEditor').value||'{}'; const a=document.createElement('a'); a.href=URL.createObjectURL(new Blob([text], {type:'application/json'})); a.download='site_config.runtime_export.json'; a.click(); URL.revokeObjectURL(a.href); }
async function saveSiteConfigFromEditor(){ if(!confirm('Save the JSON editor content to Runtime site config?')) return; try{ const cfg=JSON.parse($('siteConfigEditor').value||'{}'); await postJson('/api/site/config', cfg, 'siteConfigResult'); }catch(e){ $('siteConfigResult').innerHTML=`<span class="bad">Invalid JSON: ${esc(e)}</span>`; } }
async function siteSaveRuntime(){ if(!confirm('Persist current runtime site config to disk?')) return; await postJson('/api/site/save', {}, 'siteConfigResult'); }
let CURVES={};
let CURVE_PLAYBACK=null;
function resetCurveBuffer(){ for(const k of Object.keys(CURVES)) delete CURVES[k]; CURVE_PLAYBACK=null; if($('curveCsvStatus')) $('curveCsvStatus').textContent='Cleared.'; renderCurve(); }
function curveValueFromSnapshot(snap,sig){ if(!snap) return NaN; const aliases={soc:['soc','SOC','soc_value'], voltage:['voltage','system_voltage','total_voltage','dc_voltage'], current:['current','system_current','dc_current'], power:['power','power_kw','actual_power','active_power'], actual_power:['actual_power','active_power','power_kw','power'], reactive_power:['reactive_power','q','q_kvar'], temperature:['temperature','max_temperature','temp']}; for(const k of (aliases[sig]||[sig])){ if(snap[k]!==undefined){ const v=Number(snap[k]); if(Number.isFinite(v)) return v; } } return NaN; }
function curveSeriesList(){ if(CURVE_PLAYBACK) return CURVE_PLAYBACK; return Object.values(CURVES).map(x=>x).filter(Boolean); }
async function loadLiveCurves(){ if(CURVE_PLAYBACK) return; const type=$('curveDeviceType')?.value||'all'; const dev=$('curveDevice')?.value||''; const sig=$('curveSignal')?.value||'soc'; const multi=$('curveMulti')?.checked; const maxSamples=Math.max(60, Math.min(10000, parseInt($('curveMaxSamples')?.value||'600'))); try{ const url=`/api/curves/live?signal=${encodeURIComponent(sig)}&device_type=${encodeURIComponent(String(type).toLowerCase())}&device=${encodeURIComponent(dev)}&multi=${multi?'true':'false'}&limit=${maxSamples}`; const r=await fetch(url,{cache:'no-store'}); const j=await r.json(); CURVES={}; (j.series||[]).forEach(s=>{ CURVES[s.name]={name:s.name,data:s.data||[]}; }); }catch(e){ /* fallback to compact snapshot below */ } }
async function renderCurve(){ if(!SNAP) return; const sel=$('curveDevice'); if(!sel) return; const type=$('curveDeviceType')?.value||'all'; const names=deviceRows().filter(d=>type==='all'||d._type===type).map(d=>d.name).sort(); const old=sel.value; if(!sel.options.length || names.indexOf(old)<0){ sel.innerHTML=names.map(n=>`<option value="${esc(n)}">${esc(n)}</option>`).join(''); if(names.includes(old)) sel.value=old; }
  await loadLiveCurves();
  const c=$('curveCanvas'), ctx=c.getContext('2d'); let series=curveSeriesList().filter(s=>(s.data||[]).length); ctx.clearRect(0,0,c.width,c.height); ctx.strokeStyle='#374151'; ctx.lineWidth=1; for(let i=0;i<6;i++){ const y=i*c.height/5; ctx.beginPath(); ctx.moveTo(0,y); ctx.lineTo(c.width,y); ctx.stroke(); }
  if(!series.length && !CURVE_PLAYBACK){ const sig=$('curveSignal')?.value||'soc'; const type=$('curveDeviceType')?.value||'all'; const dev=$('curveDevice')?.value||''; const multi=$('curveMulti')?.checked; const rows=deviceRows().filter(d=>(type==='all'||d._type===type) && (multi||!dev||d.name===dev)); rows.forEach(d=>{ const y=curveValueFromSnapshot(d.latest_values||d.snapshot||d, sig); if(Number.isFinite(y)) CURVES[d.name]={name:d.name,data:[{t:Date.now(),y}]}; }); series=curveSeriesList().filter(s=>(s.data||[]).length); } const all=series.flatMap(s=>s.data.map(p=>Number(p.y))).filter(Number.isFinite); ctx.fillStyle='#9ca3af'; ctx.fillText(`server-cache series=${series.length} samples=${all.length}`, 14, 20); if(all.length<1){ if($('curveStatsRows')) $('curveStatsRows').innerHTML='<tr><td colspan="5" class="muted">No curve samples yet.</td></tr>'; return; }
  let min=Math.min(...all), max=Math.max(...all); if(min===max){min-=1;max+=1;} const palette=['#60a5fa','#34d399','#fbbf24','#f87171','#c084fc','#22d3ee','#fb7185','#a3e635'];
  series.forEach((s,si)=>{ const arr=s.data; if(arr.length<1) return; ctx.strokeStyle=palette[si%palette.length]; ctx.lineWidth=2; ctx.beginPath(); arr.forEach((p,i)=>{ const x=arr.length===1?10:i*(c.width-30)/(arr.length-1)+15; const y=c.height-25-(Number(p.y)-min)*(c.height-55)/(max-min); if(i===0) ctx.moveTo(x,y); else ctx.lineTo(x,y); }); ctx.stroke(); ctx.fillStyle=palette[si%palette.length]; ctx.fillText(s.name, 14+(si%4)*230, 40+Math.floor(si/4)*16); });
  ctx.fillStyle='#e5e7eb'; ctx.fillText(`min=${min.toFixed(2)} max=${max.toFixed(2)}`, 14, c.height-8); if($('curveStatsRows')) $('curveStatsRows').innerHTML=series.map(s=>{ const ys=s.data.map(p=>Number(p.y)).filter(Number.isFinite); const mn=Math.min(...ys), mx=Math.max(...ys), last=ys[ys.length-1]; return `<tr><td>${esc(s.name)}</td><td>${ys.length}</td><td>${mn.toFixed(3)}</td><td>${mx.toFixed(3)}</td><td>${Number(last).toFixed(3)}</td></tr>`; }).join(''); }
async function loadCurveCsvFile(){
  const inp=$('curveCsvFile');
  if(!inp || !inp.files || !inp.files[0]){ alert('Please choose a CSV file first.'); return; }
  try{
    const text=await inp.files[0].text();
    if($('curveCsvText')) $('curveCsvText').value=text;
    if($('curveCsvStatus')) $('curveCsvStatus').textContent='Loaded file '+inp.files[0].name+' into playback buffer.';
    loadCurveCsvPlayback();
  }catch(e){ if($('curveCsvStatus')) $('curveCsvStatus').textContent='CSV upload error: '+e; }
}
function loadCurveCsvPlayback(){ const text=$('curveCsvText').value||''; const lines=text.split(/\r?\n/).filter(x=>x.trim()); if(lines.length<2){ $('curveCsvStatus').textContent='Need CSV header and at least one row.'; return; } const head=lines[0].split(',').map(x=>x.trim().toLowerCase()); const idx=(names)=>names.map(n=>head.indexOf(n)).find(i=>i>=0); const ti=idx(['time','timestamp','ts']); const di=idx(['device','name','dev']); const si=idx(['signal','key','field']); const vi=idx(['value','val','scaled','raw']); if(di<0||vi<0){ $('curveCsvStatus').textContent='CSV needs device/name and value columns.'; return; } const map={}; for(const line of lines.slice(1)){ const cols=line.split(','); const dev=(cols[di]||'device').trim(); const sig=si>=0?(cols[si]||'value').trim():($('curveSignal')?.value||'value'); const v=Number(cols[vi]); if(!Number.isFinite(v)) continue; const key=dev+':'+sig; if(!map[key]) map[key]=[]; map[key].push({t:ti>=0?Date.parse(cols[ti])||map[key].length:map[key].length,y:v}); } CURVE_PLAYBACK=Object.entries(map).map(([name,data])=>({name,data})); $('curveCsvStatus').textContent=`Loaded ${CURVE_PLAYBACK.length} playback series.`; renderCurve(); }
function clearCurveCsvPlayback(){ CURVE_PLAYBACK=null; $('curveCsvStatus').textContent='Playback cleared; back to live snapshot curves.'; renderCurve(); }
function exportCurveCsv(){ const series=curveSeriesList(); let out='series,t,value\n'; for(const s of series){ for(const p of (s.data||[])) out += `${s.name},${p.t},${p.y}\n`; } const a=document.createElement('a'); a.href=URL.createObjectURL(new Blob([out],{type:'text/csv'})); a.download='ess_aio_web_curves.csv'; a.click(); URL.revokeObjectURL(a.href); }

function showAnalyzerTab(tab){
  CURRENT_ANALYZER_TAB = tab || 'upload';
  const tabs=['upload','modbus','can','joint','history'];
  for(const t of tabs){ const p=$('analyzer-'+t); if(p) p.classList.toggle('active', t===tab); }
  document.querySelectorAll('#analyzerTabs button').forEach(b=>b.classList.toggle('active', b.dataset.tab===tab));
  if(tab==='history') loadDiagnosisHistory();
  if(tab==='modbus'||tab==='can'||tab==='joint'||tab==='upload') loadAnalyzerFiles();
}
function analyzerFilesByKind(kind){
  if(kind==='modbus') return ANALYZER_FILES.filter(f=>['modbus','csv','pcap','pcapng'].includes(String(f.kind||'').toLowerCase()));
  if(kind==='asc') return ANALYZER_FILES.filter(f=>String(f.kind||'').toLowerCase()==='asc');
  if(kind==='dbc') return ANALYZER_FILES.filter(f=>String(f.kind||'').toLowerCase()==='dbc');
  if(kind==='mapping') return ANALYZER_FILES.filter(f=>String(f.kind||'').toLowerCase()==='mapping');
  return [];
}
function setSelectOptionsKeep(id, files, placeholder){
  const el=$(id); if(!el) return; const old=el.value;
  el.innerHTML=`<option value="">${esc(placeholder||'Select file')}</option>` + (files||[]).map(f=>`<option value="${esc(String(f.path||''))}">${esc(f.name||f.path||'')}</option>`).join('');
  if(old && Array.from(el.options).some(o=>o.value===old)) el.value=old;
}
function refreshAnalyzerFileSelects(){
  const modbus=analyzerFilesByKind('modbus'), asc=analyzerFilesByKind('asc'), dbc=analyzerFilesByKind('dbc'), mapping=analyzerFilesByKind('mapping');
  setSelectOptionsKeep('anModbusSelect', modbus, 'Select uploaded Modbus capture');
  setSelectOptionsKeep('anJointModbusSelect', modbus, 'Select uploaded Modbus capture');
  setSelectOptionsKeep('anAscSelect', asc, 'Select uploaded ASC');
  setSelectOptionsKeep('anJointAscSelect', asc, 'Select uploaded ASC');
  setSelectOptionsKeep('anDbcSelect', dbc, 'Select uploaded DBC');
  setSelectOptionsKeep('anJointDbcSelect', dbc, 'Select uploaded DBC');
  setSelectOptionsKeep('anMappingSelect', mapping, 'Select uploaded mapping.json');
  setSelectOptionsKeep('anJointMappingSelect', mapping, 'Select uploaded mapping.json');
  updateAnalyzerSelectedLabels();
}
function selectAnalyzerPath(kind,path){
  if(!path) return;
  if(kind==='asc'){ if($('anAscPath')) $('anAscPath').value=path; ['anAscSelect','anJointAscSelect'].forEach(id=>{if($(id)) $(id).value=path;}); }
  else if(kind==='dbc'){ if($('anDbcPath')) $('anDbcPath').value=path; ['anDbcSelect','anJointDbcSelect'].forEach(id=>{if($(id)) $(id).value=path;}); }
  else if(kind==='mapping'){ if($('anMappingPath')) $('anMappingPath').value=path; ['anMappingSelect','anJointMappingSelect'].forEach(id=>{if($(id)) $(id).value=path;}); }
  else if(kind==='modbus'){ if($('anModbusPath')) $('anModbusPath').value=path; if($('anJointModbusPath')) $('anJointModbusPath').value=path; ['anModbusSelect','anJointModbusSelect'].forEach(id=>{if($(id)) $(id).value=path;}); }
  else if(kind==='joint_modbus'){ if($('anJointModbusPath')) $('anJointModbusPath').value=path; if($('anModbusPath')) $('anModbusPath').value=path; ['anModbusSelect','anJointModbusSelect'].forEach(id=>{if($(id)) $(id).value=path;}); }
  updateAnalyzerSelectedLabels();
}
function updateAnalyzerSelectedLabels(){
  const mod=$('anModbusPath')?.value||'';
  if($('anModbusSelectedFile')) $('anModbusSelectedFile').innerHTML = mod ? `<b>Selected:</b> <code>${esc(mod)}</code>` : '<span>No Modbus capture selected.</span>';
  const asc=$('anAscPath')?.value||'', dbc=$('anDbcPath')?.value||'', map=$('anMappingPath')?.value||'';
  if($('anCanSelectedFiles')) $('anCanSelectedFiles').innerHTML = [asc&&`ASC: <code>${esc(asc)}</code>`, dbc&&`DBC: <code>${esc(dbc)}</code>`, map&&`Mapping: <code>${esc(map)}</code>`].filter(Boolean).join('<br>') || 'No CAN-related files selected.';
}
async function uploadAnalyzerFile(kind,inputId,targetId){
  const inp=$(inputId); if(!inp || !inp.files || !inp.files[0]){ alert('Please choose a file first.'); return; }
  const fd=new FormData(); fd.append('file', inp.files[0]); fd.append('kind', kind);
  $('anUploadStatus').textContent='Uploading '+inp.files[0].name+'...';
  try{
    const r=await fetch('/api/analyzer/upload',{method:'POST',body:fd}); const j=await r.json();
    if(!j.ok){ $('anUploadStatus').textContent='Upload failed: '+(j.error||JSON.stringify(j)); return; }
    if(targetId && $(targetId)) $(targetId).value=j.path||'';
    selectAnalyzerPath(kind, j.path||'');
    $('anUploadStatus').textContent='Uploaded '+(j.filename||'file')+' → '+(j.path||'');
    await loadAnalyzerFiles();
  }catch(e){ $('anUploadStatus').textContent='Upload error: '+e; }
}
async function uploadAnalyzerModbusFile(){ await uploadAnalyzerModbusFileFrom('anModbusFile'); }
async function uploadAnalyzerModbusFileFrom(inputId){
  const inp=$(inputId); if(!inp || !inp.files || !inp.files[0]){ alert('Please choose a file first.'); return; }
  const fd=new FormData(); fd.append('file', inp.files[0]); fd.append('kind', 'modbus');
  if($('anUploadStatus')) $('anUploadStatus').textContent='Uploading '+inp.files[0].name+'...';
  try{
    const r=await fetch('/api/analyzer/upload',{method:'POST',body:fd}); const j=await r.json();
    if(!j.ok){ if($('anUploadStatus')) $('anUploadStatus').textContent='Upload failed: '+(j.error||JSON.stringify(j)); alert('Upload failed: '+(j.error||JSON.stringify(j))); return; }
    await loadAnalyzerFiles();
    selectAnalyzerPath('modbus', j.path||'');
    if($('anUploadStatus')) $('anUploadStatus').textContent='Uploaded '+(j.filename||'capture')+' → '+(j.path||'');
    showAnalyzerTab(CURRENT_ANALYZER_TAB||'modbus');
  }catch(e){ if($('anUploadStatus')) $('anUploadStatus').textContent='Upload error: '+e; alert('Upload error: '+e); }
}
function useAnalyzerFile(kind,path){
  if(kind==='asc') selectAnalyzerPath('asc', path);
  else if(kind==='dbc') selectAnalyzerPath('dbc', path);
  else if(kind==='mapping') selectAnalyzerPath('mapping', path);
  else if(kind==='modbus' || kind==='csv' || kind==='pcap' || kind==='pcapng') selectAnalyzerPath('modbus', path);
}
async function loadAnalyzerFiles(){
  const el=$('analyzerFileRows'); if(!el) return;
  el.innerHTML='<tr><td colspan="5" class="muted">Loading...</td></tr>';
  try{
    const r=await fetch('/api/analyzer/files',{cache:'no-store'}); const j=await r.json(); const files=j.files||[]; ANALYZER_FILES=files; refreshAnalyzerFileSelects();
    el.innerHTML=files.map(f=>`<tr><td>${esc(f.kind||'')}</td><td>${esc(f.name||'')}</td><td>${esc(formatBytes(f.size_bytes||0))}</td><td>${esc(f.modified_at||'')}</td><td><button data-kind="${esc(f.kind||'')}" data-path="${esc(String(f.path||''))}" onclick="useAnalyzerFile(this.dataset.kind,this.dataset.path)">Use</button></td></tr>`).join('') || '<tr><td colspan="5" class="muted">No uploaded files yet.</td></tr>';
  }catch(e){ el.innerHTML=`<tr><td colspan="5" class="bad">${esc(e)}</td></tr>`; }
}
function formatBytes(n){ n=Number(n)||0; if(n<1024) return n+' B'; if(n<1048576) return (n/1024).toFixed(1)+' KB'; if(n<1073741824) return (n/1048576).toFixed(1)+' MB'; return (n/1073741824).toFixed(2)+' GB'; }
async function analyzeModbusCapture(){
  const body={path:$('anModbusPath').value, timeout_seconds:parseFloat($('anTimeout').value||'2'), limit:200};
  $('anModbusResult').textContent='Analyzing...';
  try{ const r=await fetch('/api/analyzer/modbus',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}); const j=await r.json(); $('anModbusResult').textContent=JSON.stringify(j,null,2); await loadDiagnosisHistory(); }catch(e){ $('anModbusResult').textContent=String(e); }
}
async function analyzeJoint(){
  const body={asc_path:$('anAscPath').value, modbus_path:$('anJointModbusPath').value, dbc_path:$('anDbcPath').value, mapping_path:$('anMappingPath').value, tolerance_s:parseFloat($('anTolerance').value||'0.5'), limit:200};
  $('anJointResult').textContent='Correlating...';
  try{ const r=await fetch('/api/analyzer/joint',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}); const j=await r.json(); $('anJointResult').textContent=JSON.stringify(j,null,2); await loadDiagnosisHistory(); }catch(e){ $('anJointResult').textContent=String(e); }
}
async function loadDiagnosisHistory(){
  const el=$('diagnosisHistory'); if(!el) return;
  el.innerHTML='<tr><td colspan="6" class="muted">Loading...</td></tr>';
  try{
    const r=await fetch('/api/diagnosis/history?limit=100',{cache:'no-store'}); const data=await r.json(); const jobs=data.jobs||[];
    el.innerHTML=jobs.map(j=>`<tr><td>${esc(j.created_at||'')}</td><td>${esc(j.kind||'')}</td><td>${pill(j.status||'')}</td><td>${esc(j.count??'')}</td><td><code>${esc(JSON.stringify(j.summary||{}))}</code></td><td>${esc((j.evidence||[]).map(e=>e.message||e.type).join('\n'))}</td></tr>`).join('') || '<tr><td colspan="6" class="muted">No diagnosis history.</td></tr>';
  }catch(e){ el.innerHTML=`<tr><td colspan="6" class="bad">${esc(e)}</td></tr>`; }
}
async function clearDiagnosisHistory(){
  if(!confirm('Clear Diagnosis Center history?')) return;
  await postJson('/api/diagnosis/history/clear', {}, null);
  await loadDiagnosisHistory();
}


const UI_ACTION_MATRIX = [
  ['Overview','Start All','start_all','normal'], ['Overview','Stop All','stop_all','normal'], ['Overview','Clear Log View','clear_log_view','normal'], ['Overview','Open Output Folder','open_output_folder','file-dialog'],
  ['Devices','Browse BMS Output','browse_bms_output','file-dialog'], ['Devices','Add device','add_device','normal'], ['Devices','Remove selected BMS','remove_selected_bms','normal'], ['Devices','Browse PCS Output','browse_pcs_output','file-dialog'], ['Devices','Add / Update PCS','add_update_pcs','normal'], ['Devices','Remove PCS','remove_pcs','normal'], ['Devices','Set Current PCS','set_current_pcs','normal'], ['Devices','Import PCS Profile','import_pcs_profile','file-dialog'], ['Devices','Save PCS List','save_pcs_list','normal'], ['Devices','Connect selected PCS','connect_selected_pcs','normal'], ['Devices','Disconnect selected PCS','disconnect_selected_pcs','normal'],
  ['BMS Control','Clear Fault All Online','clear_fault_all_online','high'], ['BMS Control','Power On All Online','power_on_all_online','high'], ['BMS Control','Power Off All Online','power_off_all_online','high'], ['BMS Control','Stay All Online','stay_all_online','high'], ['BMS Control','Read BMS Debug','read_bms_debug','normal'], ['BMS Control','Read BMS Version','read_bms_version','normal'], ['BMS Control','HV ON All Online','hv_on_all_online','high'], ['BMS Control','HV OFF All Online','hv_off_all_online','high'], ['BMS Control','Cancel HV Workflow','cancel_hv_workflow','normal'],
  ['PCS Control','Refresh PCS List','refresh_pcs_list','normal'], ['PCS Control','Refresh PCS Status','refresh_pcs_status','normal'], ['PCS Control','Test PCS Config','test_pcs_config','normal'], ['PCS Control','Read PCS Debug','read_pcs_debug','normal'], ['PCS Control','Stop Debug','stop_debug','high'], ['PCS Control','Start Debug','start_debug','high'], ['PCS Control','HV On Debug','hv_on_debug','high'], ['PCS Control','HV Off Debug','hv_off_debug','high'], ['PCS Control','Refresh PCS Live Registers','refresh_pcs_live_registers','normal'], ['PCS Control','Fleet Status','fleet_status','normal'],
  ['Curves','Load History CSV','load_history_csv','file-dialog'], ['Curves','Clear History','clear_history','normal'], ['Curves','Apply Time Filter','apply_time_filter','normal'], ['Curves','Add Point','add_point','normal'], ['Curves','Clear Dynamic','clear_dynamic','normal'], ['Curves','Toggle Favorite','toggle_favorite','normal'], ['Curves','Add to Curve','add_to_curve','normal'],
  ['Replay','Load Main CSV','load_main_csv','file-dialog'], ['Replay','Replay Next Row','replay_next_row','normal'], ['Replay','Start Replay','start_replay','normal'], ['Replay','Stop Replay','stop_replay','normal'],
  ['Packet Analyzer','Load Capture','load_capture','file-dialog'], ['Packet Analyzer','Clear','clear_packet','normal'], ['Packet Analyzer','Export CSV','export_packet_csv','normal'], ['Packet Analyzer','Analyze Issues','analyze_issues','normal'], ['Packet Analyzer','Send to Register Tool','send_to_register_tool','normal'], ['Packet Analyzer','Apply','packet_apply','normal'], ['Packet Analyzer','First Page','packet_first','normal'], ['Packet Analyzer','Prev Page','packet_prev','normal'], ['Packet Analyzer','Next Page','packet_next','normal'], ['Packet Analyzer','Last Page','packet_last','normal'],
  ['CAN','Load CAN Log','load_can_log','file-dialog'], ['CAN','Clear CAN','clear_can','normal'], ['CAN','DBC / Mapping','select_mapping','file-dialog'], ['CAN','Clear DBC','clear_dbc','normal'], ['CAN','Export Frames','export_frames','normal'], ['CAN','Export Stats','export_stats','normal'], ['CAN','Apply','can_apply','normal'], ['CAN','Add Signal','add_signal','normal'], ['CAN','Clear Plot','clear_plot','normal'], ['CAN','Export Signal CSV','export_signal_csv','normal'],
  ['Joint Analysis','Select ASC','select_asc','file-dialog'], ['Joint Analysis','Select Modbus Capture','select_modbus_capture','file-dialog'], ['Joint Analysis','Select DBC','select_dbc','file-dialog'], ['Joint Analysis','Select Mapping','select_mapping','file-dialog'], ['Diagnosis','Run Diagnosis','run_packet_diagnosis','normal'], ['Diagnosis','Clear All Evidence','clear_all_evidence','normal'], ['Diagnosis','Export CSV','export_diagnosis_csv','normal'], ['Diagnosis','Export Markdown','export_diagnosis_markdown','normal'],
  ['Release','Run Self Check','run_self_check','normal'], ['Release','Open Crash Logs','open_crash_logs','file-dialog'], ['Release','About ESS-AIO','about','normal'], ['Report','Start Session','start_session','normal'], ['Report','End Session','end_session','normal'], ['Report','Generate HTML Report','generate_html_report','normal'], ['Report','Export Debug Package','export_debug_package','normal'], ['Report','Open Reports Folder','open_reports_folder','file-dialog'],
  ['Settings','Apply Runtime Params','apply_runtime_params','normal'], ['Site','Apply Site','apply_site','normal'], ['Site','Refresh','refresh_site','normal'], ['Site','Save Site','save_site','normal'], ['Site','Import Site','import_site','file-dialog'], ['Site','Export Site','export_site','file-dialog'], ['Site','Rename Cluster','rename_cluster','normal'], ['Site','Add Cluster','add_cluster','normal'], ['Site','Delete Selected Cluster','delete_selected_cluster','normal'], ['Site','Add PCS to Cluster','add_pcs_to_cluster','normal'], ['Site','Remove PCS from Cluster','remove_pcs_from_cluster','normal'], ['Site','Move BMS','move_bms','normal'], ['Site','Refresh Power Map','refresh_power_map','normal'], ['Site','Auto Even Map','auto_even_map','normal'], ['Site','Apply Power Map','apply_power_map','normal'], ['Site','Clear Power Map','clear_power_map','normal'],
  ['Strategy','Reload Strategy','reload_strategy','normal'], ['Strategy','Save Strategy','save_strategy','normal'], ['Strategy','Import Strategy JSON','import_strategy_json','file-dialog'], ['Strategy','Export Strategy JSON','export_strategy_json','file-dialog'], ['Strategy','Reset Default','reset_default_strategy','normal'], ['Strategy','Refresh Clusters','refresh_clusters','normal'], ['Strategy','Apply Fake Scenario','apply_fake_scenario','normal'], ['Strategy','Reset Fake Scenarios','reset_fake_scenarios','normal'],
  ['Templates','Import Template Package','import_template_package','file-dialog'], ['Templates','Validate','validate_template','normal'], ['Templates','Apply to Current Profile','apply_template','normal'], ['Templates','Export Template Package','export_template_package','file-dialog'], ['Templates','Refresh','refresh_templates','normal'], ['Templates','Apply Driver Binding','apply_driver_binding','normal'], ['Templates','Import Point Table JSON','import_point_table_json','file-dialog'], ['Templates','Set Selected As Active','set_point_table_active','normal'], ['Templates','Refresh Point Tables','refresh_point_tables','normal'], ['Templates','Open Point Tables Folder','open_output_folder','file-dialog'],
  ['Timeline','Refresh Timeline','refresh_timeline','normal'], ['Timeline','Export CSV','export_timeline_csv','normal']
];
function loadUiActionMatrix(){
  const el=$('uiActionRows'); if(!el) return;
  el.innerHTML=UI_ACTION_MATRIX.map(([group,label,action,risk])=>`<tr><td>${esc(group)}</td><td>${esc(label)}</td><td><code>${esc(action)}</code></td><td>${pill(risk)}</td><td><button onclick="runUiAction('${esc(action)}','${esc(label)}','${esc(risk)}')">Run</button></td></tr>`).join('');
  if($('uiActionResult')) $('uiActionResult').textContent=`${UI_ACTION_MATRIX.length} PySide buttons listed for Web parity.`;
}
async function runUiAction(action,label,risk){
  let confirm_text='';
  if(risk==='high'){
    if(!requireExecute(`${label} is a high-risk action. Type EXECUTE to continue.`)) return;
    confirm_text='EXECUTE';
  }
  await postJson('/api/ui-action', {action, params:{}, confirm_text}, 'uiActionResult');
}


async function loadRuntimeCenter(){
  try{
    const r=await fetch('/api/runtime-center',{cache:'no-store'}); const data=await r.json();
    const site=data.site||{};
    const state=data.site_state||'Unknown';
    const cls=state==='Fault'?'bad':(state==='Warning'?'warn':(state==='Running'?'ok':'muted'));
    if($('runtimeCenterCards')) $('runtimeCenterCards').innerHTML=`
      <div class="card"><div class="label">Site State</div><div class="value ${cls}">${esc(state)}</div></div>
      <div class="card"><div class="label">BMS Online</div><div class="value">${esc(site.bms_online||0)} / ${esc(site.bms_total||0)}</div></div>
      <div class="card"><div class="label">PCS Online</div><div class="value">${esc(site.pcs_online||0)} / ${esc(site.pcs_total||0)}</div></div>
      <div class="card"><div class="label">Strategies</div><div class="value">${esc(site.strategies_running||0)}</div></div>
      <div class="card"><div class="label">Recent Failed Cmd</div><div class="value ${site.commands_failed_recent?'bad':''}">${esc(site.commands_failed_recent||0)}</div></div>`;
    if($('runtimeCenterClusters')) $('runtimeCenterClusters').innerHTML=(data.clusters||[]).map(c=>`<tr><td>${esc(c.name)}</td><td>${esc((c.bms_devices||[]).join(', '))}</td><td>${esc((c.pcs_devices||[]).join(', '))}</td><td>${esc(c.allocation_mode||'')}</td><td>${(data.workers?.strategies||[]).includes(c.name)?pill('running'):pill('stopped')}</td></tr>`).join('') || '<tr><td colspan="5" class="muted">No clusters configured</td></tr>';
    if($('runtimeCenterCommands')) $('runtimeCenterCommands').textContent=JSON.stringify((data.recent_commands||[]).slice(0,8),null,2);
    if($('runtimeCenterRaw')) $('runtimeCenterRaw').textContent=JSON.stringify(data,null,2);
  }catch(e){ if($('runtimeCenterRaw')) $('runtimeCenterRaw').textContent=String(e); }
}
let ALARM_TIMER=null;
function loadAlarmCenterDebounced(){ clearTimeout(ALARM_TIMER); ALARM_TIMER=setTimeout(loadAlarmCenter, 250); }
async function loadAlarmCenter(){
  try{
    const q=$('alarmFilterText')?$('alarmFilterText').value:'';
    const sev=$('alarmFilterSeverity')?$('alarmFilterSeverity').value:'';
    const includeAck=$('alarmIncludeAck')?$('alarmIncludeAck').checked:true;
    const url=`/api/alarm-center?limit=500&query=${encodeURIComponent(q)}&severity=${encodeURIComponent(sev)}&include_ack=${includeAck?'true':'false'}`;
    const r=await fetch(url,{cache:'no-store'}); const data=await r.json();
    if($('alarmCenterCards')) $('alarmCenterCards').innerHTML=`
      <div class="card"><div class="label">Active Count</div><div class="value ${data.active_count?'bad':'ok'}">${esc(data.active_count||0)}</div></div>
      <div class="card"><div class="label">Filtered</div><div class="value">${esc(data.filtered_count??(data.active||[]).length)}</div></div>
      <div class="card"><div class="label">Acknowledged</div><div class="value">${esc(data.ack_count||0)}</div></div>
      <div class="card"><div class="label">Top Devices</div><div class="value">${esc((data.top_by_device||[]).length)}</div></div>`;
    if($('alarmCenterRows')) $('alarmCenterRows').innerHTML=(data.active||[]).map(a=>{
      const sig=a.key||a.message||''; const ack=a.acknowledged?`<span class="pill ok">ACK</span><br><small>${esc((a.ack||{}).iso||'')}</small>`:'<span class="muted">-</span>';
      const btn=a.acknowledged?`<button onclick="clearAlarmAck('${encodeURIComponent(a.alarm_id||'')}')">Clear</button>`:`<button onclick="ackAlarm('${encodeURIComponent(a.alarm_id||'')}')">Acknowledge</button>`;
      return `<tr><td>${esc(a.device)}</td><td>${pill(a.severity||'alarm')}</td><td>${esc(sig)}</td><td><code>${esc(a.value??'')}</code></td><td>${ack}</td><td>${btn}</td></tr>`;
    }).join('') || '<tr><td colspan="6" class="muted">No active alarms/errors detected from runtime snapshot.</td></tr>';
    if($('alarmCenterStats')) $('alarmCenterStats').textContent=JSON.stringify({top_by_device:data.top_by_device, top_by_signal:data.top_by_signal, ack_path:data.ack_path, note:data.note},null,2);
    if($('alarmCenterRaw')) $('alarmCenterRaw').textContent=JSON.stringify(data,null,2);
  }catch(e){ if($('alarmCenterRaw')) $('alarmCenterRaw').textContent=String(e); }
}
async function ackAlarm(encodedId){ const alarm_id=decodeURIComponent(encodedId||''); const note=prompt('ACK note / operator remark:', '') || ''; await postJson('/api/alarm-center/ack', {alarm_id, note}, 'alarmCenterRaw'); await loadAlarmCenter(); }
async function clearAlarmAck(encodedId){ const alarm_id=decodeURIComponent(encodedId||''); await postJson('/api/alarm-center/ack/clear', {alarm_id}, 'alarmCenterRaw'); await loadAlarmCenter(); }
async function clearAlarmAckAll(){ if(!confirm('Clear all Alarm Center ACK records?')) return; await postJson('/api/alarm-center/ack/clear', {}, 'alarmCenterRaw'); await loadAlarmCenter(); }

function registerDeviceNames(type){
  const cfgBms=(PROJECT?.bms_devices||PROJECT?.devices||[]).map(x=>x.name||x.id).filter(Boolean);
  const cfgPcs=Object.keys(PROJECT?.pcs_configs||{}).concat((PROJECT?.pcs_devices||[]).map(x=>x.name||x.id)).filter(Boolean);
  if(type==='pcs') return Array.from(new Set([...pcsRows().map(x=>x.name).filter(Boolean), ...cfgPcs])).sort();
  const live=Object.keys(((SNAP?.device_states||{}).bms)||{}).concat(bmsRows().map(x=>x.name).filter(Boolean));
  return Array.from(new Set([...live,...cfgBms])).sort();
}
function populateRegisterDevices(){
  if(!SNAP) return;
  const type=$('regDeviceType')?.value || 'bms';
  const names=registerDeviceNames(type);
  const old=$('regDevice')?.value || '';
  if($('regDevice')){ $('regDevice').innerHTML=names.map(n=>`<option value="${esc(n)}">${esc(n)}</option>`).join(''); if(names.includes(old)) $('regDevice').value=old; }
  const bms=registerDeviceNames('bms');
  const oldw=$('regWriteDevice')?.value || '';
  if($('regWriteDevice')){ $('regWriteDevice').innerHTML=bms.map(n=>`<option value="${esc(n)}">${esc(n)}</option>`).join(''); if(bms.includes(oldw)) $('regWriteDevice').value=oldw; }
}
function toggleRegisterMode(){
  const m=$('regReadMode')?.value || 'continuous';
  if($('regContinuousFields')) $('regContinuousFields').style.display = m==='continuous' ? '' : 'none';
  if($('regStridedFields')) $('regStridedFields').style.display = m==='continuous' ? 'none' : '';
}
function renderRegisterRows(data){
  const rows=data.rows||[];
  if($('regRows')) $('regRows').innerHTML=rows.map(r=>{ const p=r.point||{}; return `<tr><td><code>${esc(r.address_hex)}</code></td><td>${esc(r.raw)}</td><td>${esc(r.scaled)}</td><td>${esc(p.name||p.key||'')}</td><td>${esc(p.unit||'')}</td><td>${esc(p.access||'')}</td></tr>`; }).join('') || '<tr><td colspan="6" class="muted">No data</td></tr>';
  if($('regReadJson')) $('regReadJson').textContent=JSON.stringify(data,null,2);
}
async function registerRead(){
  const body={device_type:$('regDeviceType').value, device:$('regDevice').value, register_type:$('regType').value, mode:$('regReadMode').value, start:$('regStart').value, count:parseInt($('regCount').value||'1'), step:$('regStep').value, quantity:parseInt($('regQuantity').value||'1'), length:parseInt($('regLength').value||'1')};
  if($('regReadJson')) $('regReadJson').textContent='Reading...';
  try{ const r=await fetch('/api/register/read',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}); const j=await r.json(); renderRegisterRows(j); }catch(e){ if($('regReadJson')) $('regReadJson').textContent=String(e); }
}
async function registerLookup(){
  const body={device_type:$('regDeviceType').value, device:$('regDevice').value, address:$('regStart').value};
  try{ const r=await fetch('/api/register/lookup',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}); const j=await r.json(); $('regLookupResult').textContent=JSON.stringify(j,null,2); }catch(e){ $('regLookupResult').textContent=String(e); }
}
async function registerWriteBms(){
  const device=$('regWriteDevice').value; const address=parseAddrForApi($('regWriteAddress').value); const value=parseInt($('regWriteValue').value||'0');
  if(!device) return;
  if(!requireExecute(`Write BMS register ${$('regWriteAddress').value}=${value} on ${device}?`)) return;
  await postJson('/api/bms/register-write', {device, scope:'single', address, value, confirm_text:'EXECUTE'}, 'regWriteResult');
}
function copyRegisterResult(){ try{ navigator.clipboard.writeText($('regReadJson').textContent||''); }catch(e){} }

async function loadReleaseCenter(){
  const box=$('releaseNotesBox'); if(box) box.textContent='Loading...';
  try{
    const r=await fetch('/api/release/manifest',{cache:'no-store'}); const data=await r.json();
    if($('releaseCards')) $('releaseCards').innerHTML=[['Schema',data.api_schema||'-','accent'],['Files',(data.files||[]).filter(f=>f.exists).length+'/'+(data.files||[]).length,''],['BMS',((data.snapshot||{}).summary||{}).bms_total??'-',''],['PCS',((data.snapshot||{}).summary||{}).pcs_total??'-','']].map(([l,v,c])=>`<div class="card"><div class="label">${esc(l)}</div><div class="value ${c}">${esc(v)}</div></div>`).join('');
    if($('releaseFileRows')) $('releaseFileRows').innerHTML=(data.files||[]).map(f=>`<tr><td>${esc(f.kind)}</td><td><code>${esc(f.path)}</code></td><td>${f.exists?pill('ok'):pill('missing')}</td><td>${esc(f.size_bytes||0)}</td></tr>`).join('') || '<tr><td colspan="4" class="muted">No files</td></tr>';
    if(box) box.textContent=data.release_notes || JSON.stringify(data,null,2);
  }catch(e){ if(box) box.textContent=String(e); }
}
function renderActive(){ if(!SNAP) return; if(CURRENT_PAGE==='runtimecenter') loadRuntimeCenter(); else if(CURRENT_PAGE==='overview') renderOverview(); else if(CURRENT_PAGE==='devices') renderDevices(); else if(CURRENT_PAGE==='project') renderProjectConfig(); else if(CURRENT_PAGE==='ops'){ populateOpsBmsDevices(); renderBmsControl(); renderBmsPresetRegisters(); } else if(CURRENT_PAGE==='pcs'){ populatePcsSelected(); renderPCS(); } else if(CURRENT_PAGE==='strategy') loadStrategyCenter(); else if(CURRENT_PAGE==='analyzer'){ loadAnalyzerFiles(); if(document.querySelector('#analyzer-history.active')) loadDiagnosisHistory(); } else if(CURRENT_PAGE==='registerdebug'){ populateRegisterDevices(); } else if(CURRENT_PAGE==='release'){ loadReleaseCenter(); } else if(CURRENT_PAGE==='health'){ loadHealthMonitor(); } else if(CURRENT_PAGE==='parity'){ loadParityAudit(); } else if(CURRENT_PAGE==='uiactions'){ loadUiActionMatrix(); } else if(CURRENT_PAGE==='lts'){ loadLtsAudit(); } else if(CURRENT_PAGE==='curves') renderCurve(); else if(CURRENT_PAGE==='settings'){ renderSettings(); if(!$('runtimeSettingsRows')?.children.length) loadRuntimeSettings(); } else if(CURRENT_PAGE==='site' && !$('siteConfigEditor').value) loadSiteConfig(); else if(CURRENT_PAGE==='alarmcenter') loadAlarmCenter(); else if(CURRENT_PAGE==='alarms') populateAlarmDevices(); else if(CURRENT_PAGE==='clusters'){ renderClusters(); if(!$('powerMapStatusRows')?.children.length) loadPowerMapStatus(); if(!$('pmEditorRows')?.children.length) loadPowerMapEditor(); } else if(CURRENT_PAGE==='commands') renderCommands(); updateRuntimeFooter(); }


async function loadLtsAudit(){
  const raw=$('ltsRaw'); if(raw) raw.textContent='Running 9.x LTS final audit...';
  try{
    const r=await fetch('/api/lts/final',{cache:'no-store'}); const data=await r.json();
    const cls=data.readiness==='lts_ready'?'ok':'warn'; const sum=data.summary||{};
    if($('ltsCards')) $('ltsCards').innerHTML=`
      <div class="card"><div class="label">Readiness</div><div class="value ${cls}">${esc(data.readiness||'-')}</div></div>
      <div class="card"><div class="label">Health</div><div class="value ${String(sum.health_status)==='fault'?'bad':(String(sum.health_status)==='warning'?'warn':'ok')}">${esc(sum.health_status||'-')} ${esc(sum.health_score??'')}</div></div>
      <div class="card"><div class="label">Parity</div><div class="value ${Number(sum.parity_percent||0)>=95?'ok':'warn'}">${esc(sum.parity_percent??'-')}%</div></div>
      <div class="card"><div class="label">Packaging Missing</div><div class="value ${(sum.missing_packaging_items||0)?'bad':'ok'}">${esc(sum.missing_packaging_items??0)}</div></div>
      <div class="card"><div class="label">Consistency Issues</div><div class="value ${(sum.consistency_issues||0)?'warn':'ok'}">${esc(sum.consistency_issues??0)}</div></div>`;
    const control=((data.control_closure||{}).checklist)||[];
    if($('ltsControlRows')) $('ltsControlRows').innerHTML=control.map(x=>`<tr><td>${esc(x.area||'')}</td><td>${esc(x.feature||'')}</td><td>${pill(x.status||'unknown')}</td><td><code>${esc((x.api||[]).join(' · '))}</code></td><td>${esc(x.safety||'')}</td></tr>`).join('') || '<tr><td colspan="5" class="muted">No rows</td></tr>';
    const files=((data.packaging||{}).files)||[];
    if($('ltsPackagingRows')) $('ltsPackagingRows').innerHTML=files.map(x=>`<tr><td>${esc(x.relative_path||x.kind||'')}</td><td>${x.exists?pill('ok'):pill('missing')}</td><td><code>${esc(x.path||((x.checked||[])[0])||'')}</code></td></tr>`).join('') || '<tr><td colspan="3" class="muted">No rows</td></tr>';
    if(raw) raw.textContent=JSON.stringify(data,null,2);
  }catch(e){ if(raw) raw.textContent=String(e); }
}

async function loadParityAudit(){
  const raw=$('parityRaw'); if(raw) raw.textContent='Running parity audit...';
  try{
    const r=await fetch('/api/ui-web-parity',{cache:'no-store'}); const data=await r.json();
    const cov=data.coverage||{}; const cls=(data.readiness==='ready'?'ok':'warn');
    if($('parityCards')) $('parityCards').innerHTML=`
      <div class="card"><div class="label">Readiness</div><div class="value ${cls}">${esc(data.readiness||'-')}</div></div>
      <div class="card"><div class="label">Coverage</div><div class="value ${cls}">${esc(cov.percent??'-')}%</div></div>
      <div class="card"><div class="label">Covered</div><div class="value ok">${esc(cov.covered??0)} / ${esc(cov.total??0)}</div></div>
      <div class="card"><div class="label">Missing</div><div class="value ${(cov.missing||0)?'bad':'ok'}">${esc(cov.missing??0)}</div></div>`;
    if($('parityRows')) $('parityRows').innerHTML=(data.checklist||[]).map(x=>`<tr><td>${esc(x.area||'')}</td><td>${esc(x.ui_feature||'')}</td><td>${esc(x.web_page||'')}</td><td>${pill(x.status||'unknown')}</td><td><code>${esc((x.api||[]).join(' · '))}</code>${x.safety?`<br><span class="warn">${esc(x.safety)}</span>`:''}${x.note?`<br><span class="muted">${esc(x.note)}</span>`:''}</td></tr>`).join('') || '<tr><td colspan="5" class="muted">No checklist rows.</td></tr>';
    if(raw) raw.textContent=JSON.stringify(data,null,2);
  }catch(e){ if(raw) raw.textContent=String(e); }
}

async function loadHealthMonitor(){
  try{
    const r=await fetch('/api/health-monitor',{cache:'no-store'}); const data=await r.json();
    const status=data.status||'unknown';
    const cls=status==='fault'?'bad':(status==='warning'?'warn':'ok');
    const sum=data.summary||{}, proc=data.process||{}, queues=data.queues||{}, curves=data.curve_cache||{};
    if($('healthCards')) $('healthCards').innerHTML=`
      <div class="card"><div class="label">Health</div><div class="value ${cls}">${esc(status)}</div></div>
      <div class="card"><div class="label">Score</div><div class="value ${cls}">${esc(data.score??'-')}</div></div>
      <div class="card"><div class="label">Workers</div><div class="value">BMS ${esc((data.workers||{}).bms_running??0)}/${esc((data.workers||{}).bms_total??0)} · PCS ${esc((data.workers||{}).pcs_running??0)}/${esc((data.workers||{}).pcs_total??0)}</div></div>
      <div class="card"><div class="label">Command Queue</div><div class="value ${queues.runtime_command_queue?'warn':''}">${esc(queues.runtime_command_queue??0)}</div></div>
      <div class="card"><div class="label">Threads / Memory</div><div class="value">${esc(proc.thread_count??'-')} / ${esc(proc.memory_mb??'-')} MB</div></div>
      <div class="card"><div class="label">Curve Cache</div><div class="value">${esc(curves.series??0)} series / ${esc(curves.samples??0)} samples</div></div>
      <div class="card"><div class="label">BMS Online</div><div class="value">${esc(sum.bms_online??0)} / ${esc(sum.bms_total??0)}</div></div>
      <div class="card"><div class="label">PCS Online</div><div class="value">${esc(sum.pcs_online??0)} / ${esc(sum.pcs_total??0)}</div></div>`;
    if($('healthIssues')) $('healthIssues').innerHTML=(data.issues||[]).map(i=>`<tr><td>${pill(i.severity||'info')}</td><td>${esc(i.area||'')}</td><td>${esc(i.message||'')}</td></tr>`).join('') || '<tr><td colspan="3" class="ok">No health issues detected</td></tr>';
    const fr=data.freshness||{}; const rows=[...(fr.error_devices||[]), ...(fr.offline_devices||[]), ...(fr.stale_devices||[])];
    if($('healthFreshness')) $('healthFreshness').innerHTML=rows.map(x=>`<tr><td>${esc(x.kind||'')}</td><td>${esc(x.device||'')}</td><td>${x.online?pill('online'):pill('offline')}</td><td>${esc(x.age_s??'')}</td><td>${esc(x.error||'')}</td></tr>`).join('') || '<tr><td colspan="5" class="ok">No stale/offline/error device rows</td></tr>';
    if($('healthRaw')) $('healthRaw').textContent=JSON.stringify(data,null,2);
  }catch(e){ if($('healthRaw')) $('healthRaw').textContent=String(e); }
}

async function shutdownRuntimeFromWeb(){
  if(!confirm('Shutdown ESS-AIO Runtime now? This stops polling, CSV recording, workers, strategy and Web API.')) return;
  const box=$('runtimeShutdownBox'); if(box) box.textContent='Requesting shutdown...';
  try{
    const r=await fetch('/api/runtime/shutdown',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({source:'web-runtime-page',confirmed:true})});
    const j=await r.json();
    if(box) box.textContent=JSON.stringify(j,null,2)+'\n\nRuntime is stopping. Closing this browser tab alone would not stop Runtime; this shutdown request does.';
    setTimeout(()=>{ refreshNow(true); }, 1000);
  }catch(e){ if(box) box.textContent=String(e); }
}


function updateRuntimeFooter(){
  const active=document.querySelector('.page.active'); if(!active) return;
  document.querySelectorAll('.runtime-page-footer').forEach(x=>x.remove());
  const summary=SNAP?.summary||{}; const div=document.createElement('div'); div.className='runtime-page-footer';
  div.innerHTML=`<span><b>Runtime</b> ${esc(SNAP?.api_schema||'-')}</span><span><b>Uptime</b> ${esc(SNAP?.uptime_s||0)}s</span><span><b>BMS</b> ${esc(summary.bms_online??0)}/${esc(summary.bms_total??0)}</span><span><b>PCS</b> ${esc(summary.pcs_online??0)}/${esc(summary.pcs_total??0)}</span><span><b>Commands</b> ${esc((SNAP?.command_acks||[]).length)}</span>`;
  active.appendChild(div);
}
async function refreshNow(force=false){ if(!force && document.hidden) return; try{ const endpoint = force && CURRENT_PAGE==='release' ? '/api/snapshot' : '/api/snapshot/compact'; const r=await fetch(endpoint, {cache:'no-store'}); const s=await r.json(); if(!force && SNAP && SNAP.snapshot_id===s.snapshot_id) return; SNAP=s; $('subtitle').innerHTML=`${esc(s.api_schema)} · uptime ${esc(s.uptime_s)}s · ${s.compact?'compact':'full'} snapshot <code>${esc(s.snapshot_id||'-')}</code>`; if(CURRENT_PAGE==='overview') renderOverview(); else renderActive(); }catch(e){ $('subtitle').innerHTML=`<span class="bad">Runtime unavailable: ${esc(e)}</span>`; } }
setInterval(()=>{ if($('auto').checked && !document.hidden) refreshNow(false); }, 2500);
document.addEventListener('visibilitychange', ()=>{ if(!document.hidden && $('auto').checked) refreshNow(true); });
refreshNow(true);
</script>
</body>
</html>
"""

def create_fastapi_app(bridge: RuntimeApiBridge):
    app = FastAPI(title="ESS-AIO Runtime API", version="0.88")

    async def _json_or_empty(request: Request) -> dict[str, Any]:
        try:
            data = await request.json()
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _first_nonempty(*values: Any, default: str = "") -> str:
        for value in values:
            if value is None:
                continue
            text = str(value).strip()
            if text:
                return text
        return default

    def _confirmation_required(action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        action = str(action or "").strip()
        high = {
            "bms_hv", "bms_hv_all", "bms_command",
            "pcs_command", "pcs_fleet_command",
            "strategy_start", "cluster_target_power",
        }
        medium = {"strategy_settings", "runtime_restore", "site_config_put", "site_config_save"}
        risk = "high" if action in high else ("medium" if action in medium else "low")
        token = str(payload.get("confirm_text") or payload.get("confirm_token") or "").strip()
        if risk == "high" and token != "EXECUTE":
            return {
                "ok": False,
                "status": "confirmation_required",
                "risk": risk,
                "action": action,
                "required_confirm_text": "EXECUTE",
                "message": "This runtime command can write to equipment or change dispatch state. Send confirm_text='EXECUTE' to run it.",
            }
        return {"ok": True, "risk": risk, "confirmed": bool(token)}

    def _risk_kwargs(action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        chk = _confirmation_required(action, payload)
        return {"_risk": chk.get("risk", "low"), "_confirmed": chk.get("confirmed", False)}

    @app.get("/", response_class=HTMLResponse)
    def dashboard() -> HTMLResponse:
        return HTMLResponse(_runtime_dashboard_html())

    @app.get("/runtime-center", response_class=HTMLResponse)
    @app.get("/overview", response_class=HTMLResponse)
    @app.get("/devices", response_class=HTMLResponse)
    @app.get("/project", response_class=HTMLResponse)
    @app.get("/ops", response_class=HTMLResponse)
    @app.get("/alarm-center", response_class=HTMLResponse)
    @app.get("/alarms", response_class=HTMLResponse)
    @app.get("/pcs", response_class=HTMLResponse)
    @app.get("/strategy", response_class=HTMLResponse)
    @app.get("/analyzer", response_class=HTMLResponse)
    @app.get("/curves", response_class=HTMLResponse)
    @app.get("/settings", response_class=HTMLResponse)
    @app.get("/site", response_class=HTMLResponse)
    @app.get("/clusters", response_class=HTMLResponse)
    @app.get("/commands", response_class=HTMLResponse)
    @app.get("/health-monitor", response_class=HTMLResponse)
    @app.get("/registerdebug", response_class=HTMLResponse)
    @app.get("/release", response_class=HTMLResponse)
    @app.get("/parity", response_class=HTMLResponse)
    @app.get("/runtime", response_class=HTMLResponse)
    @app.get("/lts", response_class=HTMLResponse)
    @app.get("/ui-actions", response_class=HTMLResponse)
    @app.get("/legacy-buttons", response_class=HTMLResponse)
    def web_ems_pages() -> HTMLResponse:
        return HTMLResponse(_runtime_dashboard_html())


    @app.get("/shutdown", response_class=HTMLResponse)
    def shutdown_page() -> HTMLResponse:
        return HTMLResponse(_runtime_shutdown_html())

    @app.get("/api")
    def root() -> dict[str, Any]:
        return {"ok": True, "service": "ESS-AIO Runtime", "phase": 7.7, "api_schema": API_SCHEMA_VERSION, "dashboard": "/", "docs": "/docs", "health": "/api/health", "snapshot": "/api/snapshot", "soak": "/api/soak/status", "metrics": "/api/runtime/metrics", "health_monitor": "/api/health-monitor", "restore_plan": "/api/runtime/restore-plan", "shutdown": "/api/runtime/shutdown", "shutdown_page": "/shutdown", "parity_audit": "/api/ui-web-parity", "lts_final": "/api/lts/final"}

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "service": "ESS-AIO Runtime", "phase": 7.7, "api_schema": API_SCHEMA_VERSION, "pid": __import__("os").getpid(), "uptime_s": round(time.time() - bridge._started_ts, 1)}

    @app.get("/api/runtime/watchdog")
    def runtime_watchdog() -> dict[str, Any]:
        return {
            "ok": True,
            "api_schema": API_SCHEMA_VERSION,
            "pid": __import__("os").getpid(),
            "uptime_s": round(time.time() - bridge._started_ts, 1),
            "snapshot_ok": True,
        }

    @app.get("/api/runtime/state")
    def runtime_state_get() -> dict[str, Any]:
        return bridge.runtime_state_status()

    @app.post("/api/runtime/state/clear")
    def runtime_state_clear() -> dict[str, Any]:
        return bridge.enqueue("runtime_state_clear", timeout_s=2.0)

    @app.get("/api/runtime/metrics")
    def runtime_metrics() -> dict[str, Any]:
        return bridge.runtime_metrics()

    @app.get("/api/runtime/restore-plan")
    def runtime_restore_plan() -> dict[str, Any]:
        return bridge.restore_plan()

    @app.post("/api/runtime/restore")
    def runtime_restore(req: RuntimeRestoreRequest) -> dict[str, Any]:
        return bridge.enqueue(
            "runtime_restore",
            restore_bms=bool(req.restore_bms),
            restore_pcs=bool(req.restore_pcs),
            restore_csv=bool(req.restore_csv),
            restore_strategy=bool(req.restore_strategy),
            timeout_s=10.0,
        )

    @app.get("/api/runtime/info")
    def runtime_info() -> dict[str, Any]:
        snap = bridge.snapshot()
        return {
            "ok": True,
            "service": "ESS-AIO Runtime",
            "phase": 7.7,
            "api_schema": API_SCHEMA_VERSION,
            "uptime_s": round(time.time() - bridge._started_ts, 1),
            "cluster_count": len(snap.get("clusters", []) or []),
            "bms_running_count": len((snap.get("workers", {}) or {}).get("bms_running", []) or []),
            "pcs_running_count": len((snap.get("workers", {}) or {}).get("pcs_running", []) or []),
            "strategy_count": len((snap.get("workers", {}) or {}).get("strategies", []) or []),
        }

    @app.get("/api/runtime/settings")
    def runtime_settings_get() -> dict[str, Any]:
        return bridge.enqueue("runtime_settings", timeout_s=3.0)

    @app.post("/api/runtime/settings")
    async def runtime_settings_post(request: Request) -> dict[str, Any]:
        payload = await _json_or_empty(request)
        return bridge.enqueue("save_runtime_settings", settings=payload, timeout_s=5.0)

    @app.get("/api/power-map/status")
    def power_map_status_get() -> dict[str, Any]:
        return bridge.enqueue("power_map_status", timeout_s=3.0)

    @app.get("/api/snapshot")
    def snapshot() -> dict[str, Any]:
        return bridge.snapshot()

    @app.get("/api/snapshot/compact")
    def snapshot_compact() -> dict[str, Any]:
        return bridge.compact_snapshot()

    @app.get("/api/performance/status")
    def performance_status() -> dict[str, Any]:
        return bridge.performance_status()

    @app.get("/api/health-monitor")
    def health_monitor() -> dict[str, Any]:
        return bridge.health_monitor()

    @app.get("/api/health-monitor/export.csv")
    def health_monitor_export_csv() -> Response:
        return Response(
            bridge.health_monitor_csv(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": "attachment; filename=ess_aio_health_monitor.csv"},
        )

    @app.get("/api/curves/live")
    def curves_live(signal: str = "soc", device_type: str = "all", device: str = "", multi: bool = True, limit: int = 600) -> dict[str, Any]:
        return bridge.curve_history(signal=signal, device_type=device_type, device=device, multi=multi, limit=limit)


    @app.get("/api/runtime-center")
    def runtime_center() -> dict[str, Any]:
        return bridge.runtime_center()

    @app.get("/api/alarm-center")
    def alarm_center(limit: int = 200, severity: str = "", query: str = "", include_ack: bool = True) -> dict[str, Any]:
        return bridge.alarm_center(limit=limit, severity=severity, query=query, include_ack=include_ack)

    @app.post("/api/alarm-center/ack")
    async def alarm_center_ack(request: Request) -> dict[str, Any]:
        payload = await _json_or_empty(request)
        return bridge.alarm_acknowledge(payload.get("alarm_id", ""), payload.get("note", ""))

    @app.post("/api/alarm-center/ack/clear")
    async def alarm_center_ack_clear(request: Request) -> dict[str, Any]:
        payload = await _json_or_empty(request)
        return bridge.alarm_clear_ack(payload.get("alarm_id", ""))

    @app.get("/api/alarm-center/export.csv")
    def alarm_center_export_csv(limit: int = 1000) -> Response:
        csv_text = bridge.alarm_center_csv(limit=limit)
        return Response(content=csv_text, media_type="text/csv", headers={"Content-Disposition": "attachment; filename=ess_aio_alarm_center.csv"})

    @app.get("/api/strategy-center")
    def strategy_center() -> dict[str, Any]:
        return bridge.strategy_center()

    @app.get("/api/strategy/config")
    def strategy_config_get() -> dict[str, Any]:
        return bridge._read_strategy_config_payload()

    @app.put("/api/strategy/config")
    async def strategy_config_put(request: Request) -> dict[str, Any]:
        payload = await _json_or_empty(request)
        payload.pop("confirm_text", None); payload.pop("confirm_token", None)
        return bridge._write_strategy_config_payload(payload)

    @app.get("/api/soak/status")
    def soak_status() -> dict[str, Any]:
        return bridge.soak_status()

    @app.post("/api/soak/start")
    def soak_start(req: SoakStartRequest) -> dict[str, Any]:
        return bridge.soak_start(label=req.label or "soak-test", interval_s=float(req.interval_s or 60.0))

    @app.post("/api/soak/stop")
    def soak_stop() -> dict[str, Any]:
        return bridge.soak_stop()

    @app.get("/api/soak/report")
    def soak_report(limit: int = 5000) -> dict[str, Any]:
        return bridge.soak_report(limit=limit)

    @app.get("/api/commands/recent")
    def commands_recent(limit: int = 50) -> dict[str, Any]:
        return bridge.command_acks_recent(limit)

    @app.get("/api/commands/audit")
    def commands_audit(limit: int = 200) -> dict[str, Any]:
        return bridge.command_audit_recent(limit)

    @app.get("/api/commands/audit-summary")
    def commands_audit_summary(limit: int = 5000) -> dict[str, Any]:
        return bridge.command_audit_summary(limit)

    @app.get("/api/commands/{command_id}")
    def command_ack(command_id: str) -> dict[str, Any]:
        return bridge.command_ack(command_id)

    @app.get("/api/commands/{command_id}/verify")
    def command_ack_verify(command_id: str) -> dict[str, Any]:
        return bridge.verify_command_ack(command_id)

    @app.post("/api/runtime/shutdown")
    def runtime_shutdown() -> dict[str, Any]:
        return bridge.enqueue("runtime_shutdown", timeout_s=2.0)

    @app.post("/api/bms/start-all")
    def bms_start_all() -> dict[str, Any]:
        return bridge.enqueue("start_all_bms", timeout_s=2.0)

    @app.post("/api/bms/stop-all")
    def bms_stop_all() -> dict[str, Any]:
        return bridge.enqueue("stop_all_bms", timeout_s=5.0)

    @app.post("/api/pcs/connect-all")
    def pcs_connect_all() -> dict[str, Any]:
        return bridge.enqueue("connect_all_pcs", timeout_s=2.0)

    @app.post("/api/pcs/stop-all")
    def pcs_stop_all() -> dict[str, Any]:
        return bridge.enqueue("stop_all_pcs", timeout_s=5.0)

    @app.post("/api/strategy/start")
    async def strategy_start(request: Request) -> dict[str, Any]:
        payload = await _json_or_empty(request)
        chk = _confirmation_required("strategy_start", payload)
        if not chk.get("ok"):
            return chk
        return bridge.enqueue("start_strategy", cluster=_first_nonempty(payload.get("cluster")), **_risk_kwargs("strategy_start", payload), timeout_s=5.0)

    @app.post("/api/strategy/stop")
    def strategy_stop(req: ClusterRequest) -> dict[str, Any]:
        return bridge.enqueue("stop_strategy", cluster=req.cluster, timeout_s=8.0)


    @app.post("/api/strategy/start-all")
    async def strategy_start_all(request: Request):
        payload = await _json_or_empty(request)
        conf = _confirmation_required("strategy_start", payload)
        if not conf.get("ok"):
            return conf
        return bridge.enqueue("start_all_strategies", _risk="high", _confirmed=True, timeout_s=30.0)

    @app.post("/api/strategy/stop-all")
    async def strategy_stop_all():
        return bridge.enqueue("stop_all_strategies", timeout_s=30.0)

    @app.post("/api/cluster/strategy-settings")
    async def cluster_strategy_settings(request: Request) -> dict[str, Any]:
        payload = await _json_or_empty(request)
        payload.pop("confirm_text", None); payload.pop("confirm_token", None)
        return bridge.enqueue("set_cluster_strategy_settings", **payload, _risk="medium", _confirmed=True, timeout_s=5.0)

    @app.post("/api/cluster/target-power")
    async def cluster_target_power(request: Request) -> dict[str, Any]:
        payload = await _json_or_empty(request)
        chk = _confirmation_required("cluster_target_power", payload)
        if not chk.get("ok"):
            return chk
        return bridge.enqueue("set_cluster_target_power", cluster=_first_nonempty(payload.get("cluster")), power_kw=float(payload.get("power_kw") or 0), **_risk_kwargs("cluster_target_power", payload), timeout_s=5.0)

    @app.post("/api/bms/start")
    async def bms_start(request: Request, device: str = "") -> dict[str, Any]:
        payload = await _json_or_empty(request)
        dev = _first_nonempty(payload.get("device"), payload.get("name"), device)
        return bridge.enqueue("start_bms", device=dev, timeout_s=3.0)

    @app.post("/api/bms/stop")
    async def bms_stop(request: Request, device: str = "") -> dict[str, Any]:
        payload = await _json_or_empty(request)
        dev = _first_nonempty(payload.get("device"), payload.get("name"), device)
        return bridge.enqueue("stop_bms", device=dev, timeout_s=5.0)

    @app.post("/api/bms/version")
    async def bms_version(request: Request) -> dict[str, Any]:
        payload = await _json_or_empty(request)
        return bridge.enqueue("read_bms_version", device=_first_nonempty(payload.get("device"), payload.get("name")), sbmu_count=int(payload.get("sbmu_count") or 1), timeout_s=10.0)

    @app.post("/api/bms/racks/read")
    async def bms_racks_read(request: Request) -> dict[str, Any]:
        payload = await _json_or_empty(request)
        return bridge.enqueue("read_bms_racks", device=_first_nonempty(payload.get("device"), payload.get("name")), count=int(payload.get("count") or 16), timeout_s=25.0)

    @app.post("/api/bms/racks/apply-mask")
    async def bms_racks_apply_mask(request: Request) -> dict[str, Any]:
        payload = await _json_or_empty(request)
        conf = _confirmation_required("bms_command", payload)
        if not conf.get("ok"):
            return conf
        return bridge.enqueue("apply_bms_rack_mask", device=_first_nonempty(payload.get("device"), payload.get("name")), changes=list(payload.get("changes") or []), _risk="high", _confirmed=True, timeout_s=20.0)

    @app.post("/api/bms/command")
    async def bms_command(request: Request, command: str = "", scope: str = "", device: str = "") -> dict[str, Any]:
        payload = await _json_or_empty(request)
        chk = _confirmation_required("bms_command", payload)
        if not chk.get("ok"):
            return chk
        return bridge.enqueue(
            "bms_command",
            command=_first_nonempty(payload.get("command"), command),
            scope=_first_nonempty(payload.get("scope"), scope, default="single"),
            device=_first_nonempty(payload.get("device"), payload.get("name"), device),
            **_risk_kwargs("bms_command", payload),
            timeout_s=5.0,
        )


    @app.post("/api/register/read")
    async def register_read(req: RegisterReadRequest):
        return bridge.enqueue("register_read", device_type=req.device_type, device=req.device, register_type=req.register_type, mode=req.mode, start=req.start, count=req.count, step=req.step, quantity=req.quantity, length=req.length, timeout_s=20.0)

    @app.post("/api/register/lookup")
    async def register_lookup(req: RegisterLookupRequest):
        return bridge.enqueue("register_lookup", device_type=req.device_type, device=req.device, address=req.address, timeout_s=5.0)

    @app.post("/api/bms/register-write")
    async def bms_register_write(req: BmsRegisterWriteRequest, request: Request):
        payload = await _json_or_empty(request)
        conf = _confirmation_required("bms_command", payload)
        if not conf.get("ok"):
            return conf
        return bridge.enqueue("bms_register_write", device=req.device, scope=req.scope, address=req.address, value=req.value, _risk="high", _confirmed=True)

    @app.post("/api/bms/rtc-write")
    async def bms_rtc_write(req: BmsRtcWriteRequest, request: Request):
        payload = await _json_or_empty(request)
        conf = _confirmation_required("bms_command", payload)
        if not conf.get("ok"):
            return conf
        return bridge.enqueue("bms_rtc_write", device=req.device, scope=req.scope, year=req.year, month=req.month, day=req.day, hour=req.hour, minute=req.minute, second=req.second, _risk="high", _confirmed=True)

    @app.post("/api/bms/hv")
    async def bms_hv(request: Request, device: str = "", mode: str = "", timeout: float | None = None, poll_interval: float | None = None) -> dict[str, Any]:
        payload = await _json_or_empty(request)
        chk = _confirmation_required("bms_hv", payload)
        if not chk.get("ok"):
            return chk
        return bridge.enqueue(
            "bms_hv",
            device=_first_nonempty(payload.get("device"), payload.get("name"), device),
            mode=_first_nonempty(payload.get("mode"), mode, default="on"),
            timeout=float(payload.get("timeout", timeout if timeout is not None else 30.0)),
            poll_interval=float(payload.get("poll_interval", poll_interval if poll_interval is not None else 1.0)),
            ignore_pcs_precheck=bool(payload.get("ignore_pcs_precheck", True)),
            **_risk_kwargs("bms_hv", payload),
            timeout_s=5.0,
        )

    @app.post("/api/bms/hv-all")
    async def bms_hv_all(request: Request, mode: str = "", timeout: float | None = None, poll_interval: float | None = None) -> dict[str, Any]:
        payload = await _json_or_empty(request)
        chk = _confirmation_required("bms_hv_all", payload)
        if not chk.get("ok"):
            return chk
        return bridge.enqueue(
            "bms_hv_all",
            mode=_first_nonempty(payload.get("mode"), mode, default="on"),
            timeout=float(payload.get("timeout", timeout if timeout is not None else 30.0)),
            poll_interval=float(payload.get("poll_interval", poll_interval if poll_interval is not None else 1.0)),
            ignore_pcs_precheck=bool(payload.get("ignore_pcs_precheck", True)),
            **_risk_kwargs("bms_hv_all", payload),
            timeout_s=5.0,
        )

    @app.post("/api/bms/heartbeat/start-all")
    def bms_hb_start_all() -> dict[str, Any]:
        return bridge.enqueue("start_bms_heartbeats", timeout_s=3.0)

    @app.post("/api/bms/heartbeat/stop-all")
    def bms_hb_stop_all() -> dict[str, Any]:
        return bridge.enqueue("stop_bms_heartbeats", timeout_s=3.0)

    @app.post("/api/bms/038b/start")
    def bms_038b_start() -> dict[str, Any]:
        return bridge.enqueue("start_bms_038b_cycle", timeout_s=3.0)

    @app.post("/api/bms/038b/stop")
    def bms_038b_stop() -> dict[str, Any]:
        return bridge.enqueue("stop_bms_038b_cycle", timeout_s=3.0)

    @app.post("/api/pcs/connect")
    async def pcs_connect(request: Request, pcs: str = "", name: str = "") -> dict[str, Any]:
        payload = await _json_or_empty(request)
        pcs_name = _first_nonempty(payload.get("pcs"), payload.get("name"), payload.get("device"), pcs, name)
        return bridge.enqueue("connect_pcs", pcs=pcs_name, timeout_s=3.0)

    @app.post("/api/pcs/stop")
    async def pcs_stop(request: Request, pcs: str = "", name: str = "") -> dict[str, Any]:
        payload = await _json_or_empty(request)
        pcs_name = _first_nonempty(payload.get("pcs"), payload.get("name"), payload.get("device"), pcs, name)
        return bridge.enqueue("stop_pcs", pcs=pcs_name, timeout_s=5.0)

    @app.post("/api/pcs/command")
    async def pcs_command(request: Request, pcs: str = "", method: str = "", value: float | None = None) -> dict[str, Any]:
        payload = await _json_or_empty(request)
        raw_value = payload.get("value", value)
        chk = _confirmation_required("pcs_command", payload)
        if not chk.get("ok"):
            return chk
        return bridge.enqueue(
            "pcs_command",
            pcs=_first_nonempty(payload.get("pcs"), payload.get("name"), payload.get("device"), pcs),
            method=_first_nonempty(payload.get("method"), payload.get("command"), method),
            value=raw_value,
            **_risk_kwargs("pcs_command", payload),
            timeout_s=5.0,
        )

    @app.post("/api/pcs/fleet-command")
    async def pcs_fleet_command(request: Request, method: str = "", value: float | None = None) -> dict[str, Any]:
        payload = await _json_or_empty(request)
        raw_value = payload.get("value", value)
        chk = _confirmation_required("pcs_fleet_command", payload)
        if not chk.get("ok"):
            return chk
        return bridge.enqueue(
            "pcs_fleet_command",
            method=_first_nonempty(payload.get("method"), payload.get("command"), method),
            value=raw_value,
            **_risk_kwargs("pcs_fleet_command", payload),
            timeout_s=5.0,
        )

    @app.get("/api/site/config")
    def site_config_get() -> dict[str, Any]:
        return bridge.enqueue("get_site_config", timeout_s=3.0)

    @app.post("/api/site/config")
    async def site_config_put(request: Request) -> dict[str, Any]:
        payload = await _json_or_empty(request)
        return bridge.enqueue("import_site_config", config=payload, timeout_s=5.0)

    @app.post("/api/site/save")
    def site_config_save() -> dict[str, Any]:
        return bridge.enqueue("save_site_config", timeout_s=5.0)

    @app.post("/api/site/delete-cluster")
    async def site_config_delete_cluster(request: Request, cluster: str = "") -> dict[str, Any]:
        payload = await _json_or_empty(request)
        cluster_name = _first_nonempty(payload.get("cluster"), payload.get("name"), cluster)
        return bridge.enqueue("delete_cluster", cluster=cluster_name, timeout_s=5.0)



    @app.get("/api/project/config")
    def project_config_get() -> dict[str, Any]:
        return bridge.enqueue("project_config", timeout_s=3.0)

    @app.get("/api/project/profiles")
    def project_profile_options() -> dict[str, Any]:
        return bridge._project_profile_options()

    @app.post("/api/project/bms/upsert")
    async def project_bms_upsert(request: Request) -> dict[str, Any]:
        payload = await _json_or_empty(request)
        return bridge.enqueue("upsert_bms_config", config=payload, timeout_s=5.0)

    @app.post("/api/project/bms/remove")
    async def project_bms_remove(request: Request) -> dict[str, Any]:
        payload = await _json_or_empty(request)
        return bridge.enqueue("remove_bms_config", device=_first_nonempty(payload.get("device"), payload.get("name")), timeout_s=5.0)

    @app.post("/api/project/pcs/upsert")
    async def project_pcs_upsert(request: Request) -> dict[str, Any]:
        payload = await _json_or_empty(request)
        return bridge.enqueue("upsert_pcs_config", config=payload, timeout_s=5.0)

    @app.post("/api/project/pcs/remove")
    async def project_pcs_remove(request: Request) -> dict[str, Any]:
        payload = await _json_or_empty(request)
        return bridge.enqueue("remove_pcs_config", pcs=_first_nonempty(payload.get("pcs"), payload.get("name")), timeout_s=5.0)

    @app.get("/api/project/validate")
    def project_validate() -> dict[str, Any]:
        return bridge.enqueue("project_validate", timeout_s=3.0)

    @app.get("/api/csv/status")
    def csv_status() -> dict[str, Any]:
        return bridge.enqueue("csv_status", timeout_s=2.0)

    @app.post("/api/csv/bms/start")
    async def csv_bms_start(request: Request) -> dict[str, Any]:
        payload = await _json_or_empty(request)
        devices = payload.get("devices") or payload.get("names") or []
        return bridge.enqueue("start_bms_csv", devices=devices if isinstance(devices, list) else [devices], timeout_s=5.0)

    @app.post("/api/csv/bms/stop")
    async def csv_bms_stop(request: Request) -> dict[str, Any]:
        payload = await _json_or_empty(request)
        devices = payload.get("devices") or payload.get("names") or []
        return bridge.enqueue("stop_bms_csv", devices=devices if isinstance(devices, list) else [devices], timeout_s=5.0)

    @app.post("/api/csv/pcs/start")
    async def csv_pcs_start(request: Request) -> dict[str, Any]:
        payload = await _json_or_empty(request)
        devices = payload.get("devices") or payload.get("names") or payload.get("pcs") or []
        return bridge.enqueue("start_pcs_csv", devices=devices if isinstance(devices, list) else [devices], timeout_s=5.0)

    @app.post("/api/csv/pcs/stop")
    async def csv_pcs_stop(request: Request) -> dict[str, Any]:
        payload = await _json_or_empty(request)
        devices = payload.get("devices") or payload.get("names") or payload.get("pcs") or []
        return bridge.enqueue("stop_pcs_csv", devices=devices if isinstance(devices, list) else [devices], timeout_s=5.0)

    @app.get("/api/logs/status")
    def logs_status() -> dict[str, Any]:
        return bridge.enqueue("log_status", timeout_s=2.0)

    @app.get("/api/logs/operation/recent")
    def operation_log_recent(max_lines: int = 300) -> dict[str, Any]:
        return bridge.enqueue("operation_log_recent", max_lines=max_lines, timeout_s=3.0)


    @app.post("/api/analyzer/upload")
    async def analyzer_upload(file: UploadFile = File(...), kind: str = "") -> dict[str, Any]:
        data = await file.read()
        return bridge.save_analyzer_upload(file.filename or "upload.bin", data, kind=kind)

    @app.get("/api/analyzer/files")
    def analyzer_files() -> dict[str, Any]:
        return bridge.analyzer_files()

    @app.post("/api/analyzer/modbus")
    def analyzer_modbus(req: PacketAnalyzeRequest) -> dict[str, Any]:
        return bridge.analyze_modbus_capture_job(req.path, timeout_seconds=req.timeout_seconds, limit=req.limit)

    @app.post("/api/analyzer/joint")
    def analyzer_joint(req: JointAnalyzeRequest) -> dict[str, Any]:
        return bridge.analyze_can_modbus_joint_job(req.asc_path, req.modbus_path, req.dbc_path, req.mapping_path, tolerance_s=req.tolerance_s, limit=req.limit)

    @app.get("/api/diagnosis/history")
    def diagnosis_history(limit: int = 100, kind: str = "") -> dict[str, Any]:
        return bridge.diagnosis_history(limit=limit, kind=kind)

    @app.post("/api/diagnosis/history/clear")
    def diagnosis_history_clear() -> dict[str, Any]:
        return bridge.diagnosis_history_clear()

    @app.get("/api/diagnosis/history/export.csv")
    def diagnosis_history_export_csv(limit: int = 1000) -> Response:
        csv_text = bridge.diagnosis_history_csv(limit=limit)
        return Response(content=csv_text, media_type="text/csv", headers={"Content-Disposition": "attachment; filename=ess_aio_diagnosis_history.csv"})


    @app.get("/api/device/{kind}/{name}/snapshot")
    def device_snapshot(kind: str, name: str) -> dict[str, Any]:
        return bridge.device_snapshot(kind, name)

    @app.get("/api/device/bms/{name}/alarms")
    def device_bms_alarms(name: str) -> dict[str, Any]:
        return bridge.device_alarms(name)

    @app.get("/api/runtime/separation-audit")
    def runtime_separation_audit() -> dict[str, Any]:
        return bridge.separation_audit()

    @app.get("/api/ui-web-parity")
    def ui_web_parity() -> dict[str, Any]:
        return bridge.ui_web_parity_audit()

    @app.get("/api/ui-web-parity/export.csv")
    def ui_web_parity_export_csv() -> Response:
        return Response(content=bridge.ui_web_parity_csv(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": "attachment; filename=ess_aio_ui_web_parity.csv"})

    @app.get("/api/ui-button-matrix")
    def ui_button_matrix() -> dict[str, Any]:
        return {"ok": True, "api_schema": API_SCHEMA_VERSION, "note": "The full button list is rendered in the Web UI Buttons page. File-dialog buttons are intentionally visible but return web_path_required in headless runtime mode.", "web_page": "/ui-actions"}

    @app.post("/api/ui-action")
    def ui_action(req: UiActionRequest) -> dict[str, Any]:
        return bridge.enqueue("ui_action", action=req.action, params=req.params, confirm_text=req.confirm_text)


    @app.get("/api/lts/final")
    def lts_final() -> dict[str, Any]:
        return bridge.lts_final_audit()

    @app.get("/api/lts/final/export.csv")
    def lts_final_export_csv() -> Response:
        return Response(content=bridge.lts_final_csv(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": "attachment; filename=ess_aio_v9_9_lts_audit.csv"})

    @app.get("/api/lts/packaging-check")
    def lts_packaging_check() -> dict[str, Any]:
        return bridge.packaging_check()

    @app.get("/api/lts/control-closure")
    def lts_control_closure() -> dict[str, Any]:
        return bridge.control_closure_audit()

    @app.get("/api/lts/data-consistency")
    def lts_data_consistency() -> dict[str, Any]:
        return bridge.data_consistency_check()

    @app.get("/api/release/manifest")
    def release_manifest() -> dict[str, Any]:
        return bridge.release_manifest()

    @app.get("/api/release/snapshot.json")
    def release_snapshot_json() -> Response:
        return Response(content=bridge.release_snapshot_json(), media_type="application/json", headers={"Content-Disposition": "attachment; filename=ess_aio_release_snapshot.json"})

    @app.get("/api/release/export.zip")
    def release_export_zip() -> Response:
        data = bridge.release_export_zip_bytes()
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return Response(content=data, media_type="application/zip", headers={"Content-Disposition": f"attachment; filename=ESS-AIO_release_snapshot_{stamp}.zip"})

    return app


def run_uvicorn_in_thread(app: Any, *, host: str = "127.0.0.1", port: int = 8765) -> threading.Thread:
    import uvicorn

    def _run() -> None:
        uvicorn.run(app, host=host, port=port, log_level="warning")

    thread = threading.Thread(target=_run, name="ESS-AIO-Runtime-API", daemon=True)
    thread.start()
    return thread
