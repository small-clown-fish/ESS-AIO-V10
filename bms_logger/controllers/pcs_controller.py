from __future__ import annotations

from typing import Any, Callable, Optional

from ..action_result import ActionResult
from ..client_factory import create_pcs_client

# Backward-compatible name used by existing UI code.
ControllerResult = ActionResult


class PcsController:
    """Application-level PCS control facade.

    UI pages/controllers should use this class instead of constructing PCS clients directly.
    It keeps Fake/Real selection, multi-PCS config lookup, and cluster-bound PCS resolution in one place.
    All externally useful actions return ActionResult and are audit-logged when AuditController exists.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    def selected_pcs_name(self) -> str:
        app = self.app
        if hasattr(app, "control_pcs_combo") and app.control_pcs_combo.currentText().strip():
            return app.control_pcs_combo.currentText().strip()

        device_name = getattr(app, "current_control_device", None)
        if device_name:
            cluster = app.get_cluster_by_device(device_name)
            if cluster and cluster.pcs_device:
                return cluster.pcs_device.name

        return getattr(app, "current_pcs_name", "") or ""

    def resolve_pcs_name_for_context(self, device_name: str | None) -> str:
        app = self.app
        if device_name and device_name in getattr(app, "pcs_configs", {}):
            return device_name

        if device_name:
            cluster = app.get_cluster_by_device(device_name)
            if cluster and cluster.pcs_device:
                return cluster.pcs_device.name

        return self.selected_pcs_name()

    def get_config(self, pcs_name: str) -> dict[str, Any]:
        if not pcs_name:
            return {}
        return self.app.get_pcs_config_by_name(pcs_name)

    def create_client_for_pcs_name(self, pcs_name: str):
        if hasattr(self.app, "_assert_local_device_io_allowed"):
            self.app._assert_local_device_io_allowed(f"create PCS client for {pcs_name}")
        return create_pcs_client(self.get_config(pcs_name), fake_mode=getattr(self.app, "fake_mode", False))

    def create_client_for_device(self, device_name: str):
        return self.create_client_for_pcs_name(self.resolve_pcs_name_for_context(device_name))

    def create_selected_client(self):
        return self.create_client_for_pcs_name(self.selected_pcs_name())

    def _ensure_pcs_worker(self, pcs_name: str) -> bool:
        if hasattr(self.app, "_runtime_block_local_device_io") and self.app._runtime_block_local_device_io(f"start local PCS worker for {pcs_name}", warn=False):
            return False
        if not pcs_name:
            return False
        fleet_manager = getattr(self.app, "fleet_manager", None)
        if fleet_manager is None:
            return False

        def factory(name: str):
            return self.create_client_for_pcs_name(name)

        try:
            fleet_manager.start_pcs_command_workers([pcs_name], factory, interval_s=float(getattr(self.app, "heartbeat_interval", 1.0)))
            return True
        except Exception as exc:
            try:
                self.app.control_log(f"[PCS][QUEUE] Failed to start PCS worker for {pcs_name}: {exc}")
            except Exception:
                pass
            return False

    def _queue_pcs_command(self, pcs_name: str, method_name: str, *args: Any, action: str = "", target: str = "", **kwargs: Any) -> ActionResult:
        action = action or method_name
        target = target or pcs_name
        if not pcs_name:
            return self._result(False, "No PCS selected", action=action, target=target)
        fleet_manager = getattr(self.app, "fleet_manager", None)
        if fleet_manager is None:
            return self._result(False, "Fleet manager is not available", action=action, target=target)
        if not self._ensure_pcs_worker(pcs_name):
            return self._result(False, f"PCS worker unavailable: {pcs_name}", action=action, target=target)
        count = fleet_manager.enqueue_pcs_command([pcs_name], method_name, *args, label=action, **kwargs)
        if count == 1:
            return self._result(True, f"{action} queued on PCS worker: {pcs_name}", action=action, target=target, value={"pcs_name": pcs_name, "queued": True})
        return self._result(False, f"{action} not queued: PCS worker offline or queue full ({pcs_name})", action=action, target=target, value={"pcs_name": pcs_name, "queued": False})

    def execute_for_device(
        self,
        device_name: str,
        method_name: str,
        *,
        precheck: bool = True,
        action_name: str | None = None,
        log: Optional[Callable[[str], None]] = None,
    ) -> ActionResult:
        # Unified control path: single PCS actions are queued on the same
        # per-PCS FleetDeviceWorker used by Fleet and Cluster Strategy.
        # This prevents a second direct Modbus connection from racing with
        # strategy dispatch or heartbeat/status workers.
        pcs_name = self.resolve_pcs_name_for_context(device_name)
        action = action_name or method_name
        return self._queue_pcs_command(pcs_name, method_name, action=action, target=pcs_name)

    def set_power_for_device(self, device_name: str, target_power_kw: float, *, precheck: bool = True) -> ActionResult:
        pcs_name = self.resolve_pcs_name_for_context(device_name)
        return self.set_power_for_pcs(pcs_name, target_power_kw, precheck=precheck)

    def set_power_for_pcs(self, pcs_name: str, target_power_kw: float, *, precheck: bool = True) -> ActionResult:
        # Queue active power write through the persistent per-PCS worker.
        # Result means "queued"; actual Modbus success/failure is reported by
        # FleetDeviceWorker status/logs.
        return self._queue_pcs_command(
            pcs_name,
            "set_active_power",
            float(target_power_kw),
            action=f"set_active_power={float(target_power_kw):.1f}kW",
            target=pcs_name,
        )


    def set_reactive_power_for_device(self, device_name: str, target_reactive_kvar: float, *, precheck: bool = True) -> ActionResult:
        pcs_name = self.resolve_pcs_name_for_context(device_name)
        return self.set_reactive_power_for_pcs(pcs_name, target_reactive_kvar, precheck=precheck)

    def set_reactive_power_for_pcs(self, pcs_name: str, target_reactive_kvar: float, *, precheck: bool = True) -> ActionResult:
        # PcsClient.set_reactive_power() still performs 7909=1 then 7812 write,
        # but it now runs inside the per-PCS worker queue instead of a direct
        # UI/AppFacade connection.
        return self._queue_pcs_command(
            pcs_name,
            "set_reactive_power",
            float(target_reactive_kvar),
            action=f"set_reactive_power={float(target_reactive_kvar):.1f}kvar",
            target=pcs_name,
        )


    def read_status_for_device(self, device_name: str) -> ActionResult:
        pcs_name = self.resolve_pcs_name_for_context(device_name)
        client = self.create_client_for_pcs_name(pcs_name)
        action = "read_status"
        target = pcs_name
        try:
            if not client.connect():
                return self._result(False, f"PCS connect failed: {pcs_name}", action=action, target=target)

            values: dict[str, str] = {}
            readers = [
                ("online", lambda: "Online" if client.is_online() else "Offline"),
                ("run_status", lambda: str(client.get_run_status())),
                ("fault_status", lambda: str(client.get_fault_status())),
                ("alarm_status", lambda: str(client.get_alarm_status())),
                ("dc_breaker", lambda: self._format_breaker_state(client)),
                ("active_power", lambda: str(client.get_active_power())),
                ("mode", lambda: str(client.get_mode())),
                ("remote_local", lambda: str(client.get_remote_local_status())),
            ]
            for key, func in readers:
                try:
                    values[key] = func()
                except Exception as exc:
                    values[key] = f"Error: {exc}"

            return self._result(True, f"PCS status refreshed: {pcs_name}", action=action, target=target, value=values)

        except Exception as exc:
            return self._result(False, f"PCS status exception: {exc}", action=action, target=target, error=str(exc))

        finally:
            try:
                client.close()
            except Exception:
                pass

    def _result(
        self,
        ok: bool,
        message: str,
        *,
        action: str = "",
        target: str = "",
        value: Any = None,
        error: str = "",
    ) -> ActionResult:
        if ok:
            result = ActionResult.success(message, action=action, target=target, source="PcsController", value=value)
        else:
            result = ActionResult.failure(message, action=action, target=target, source="PcsController", value=value, error=error)
        self._audit_result(result)
        return result

    def _audit_result(self, result: ActionResult) -> None:
        audit = getattr(self.app, "audit_controller", None)
        if audit is not None:
            audit.log_result(result)

    @staticmethod
    def _format_breaker_state(client: Any) -> str:
        try:
            if client.is_dc_breaker_open():
                return "Open"
            if client.is_dc_breaker_closed():
                return "Closed"
            return "Unknown"
        except Exception as exc:
            return f"Error: {exc}"
