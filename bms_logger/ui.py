from __future__ import annotations

import sys
import threading
from .service import BmsPcsService
from .app_facade import AppFacade
from .controllers import DeviceController, PcsController, ProfileController, StrategyController, AuditController, ServiceActionController
from .strategy_engine import StrategyEngine
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Signal, QObject, QTimer
from PySide6.QtWidgets import QApplication, QLabel, QMainWindow, QMessageBox

from .alarm_parser import AlarmParser
from .hv_controller import HvWorkflowWorker
from .recorder import AlarmRecorder, CsvRecorder
from .ui_control import UiControlMixin
from .ui_data import UiDataMixin
from .ui_device import UiDeviceMixin
from .ui_layout import UiLayoutMixin
from .worker import DeviceWorker, HeartbeatWorker, PcsPollingWorker
from .fleet_manager import FleetManager
from .cluster_strategy_runtime import ClusterStrategyWorker
from .models import Site, Cluster, Device
from .drivers import DEFAULT_BMS_DRIVER, DEFAULT_PCS_DRIVER
from .scheduler import TaskStatusStore
from .version import APP_TITLE, APP_VERSION
from .release_manager import ensure_profile
from .template_manager import TemplateManager
from .paths import resource_path, user_data_dir
from .runtime_client import RuntimeApiClient



class UiBridge(QObject):
    log_message = Signal(str)
    control_log_message = Signal(str)
    data_received = Signal(str, dict)
    error_received = Signal(str, str)
    task_status_received = Signal(str, dict)
    pcs_data_received = Signal(str, dict)
    pcs_error_received = Signal(str, str)
    heartbeat_written = Signal(str, int)
    heartbeat_error = Signal(str, str)


class MainWindow(
    UiLayoutMixin,
    UiDeviceMixin,
    UiDataMixin,
    UiControlMixin,
    QMainWindow,
):
    DETAIL_FIELDS = [
        ("bms_heartbeat", "BMS Heartbeat"),
        ("bms_power_on", "BMS Power On"),
        ("bms_status", "BMS Status"),
        ("number_of_racks", "Number of Racks"),
        ("system_voltage", "System Voltage (V)"),
        ("system_current", "System Current (A)"),
        ("soc", "SOC (%)"),
        ("soh", "SOH (%)"),
        ("max_cell_voltage", "Max Cell Voltage (mV)"),
        ("min_cell_voltage", "Min Cell Voltage (mV)"),
        ("avg_cell_voltage", "Avg Cell Voltage (mV)"),
        ("max_cell_temperature", "Max Cell Temp (°C)"),
        ("min_cell_temperature", "Min Cell Temp (°C)"),
        ("avg_cell_temperature", "Avg Cell Temp (°C)"),
        ("max_charge_current_allowed", "Max Charge Current Allowed (A)"),
        ("max_discharge_current_allowed", "Max Discharge Current Allowed (A)"),
        ("max_charge_power_allowed", "Max Charge Power Allowed (kW)"),
        ("max_discharge_power_allowed", "Max Discharge Power Allowed (kW)"),
        ("system_power", "System Power (kW)"),
    ]

    def __init__(self) -> None:
        super().__init__()

        self.setWindowTitle(APP_TITLE)
        self.resize(1280, 860)

        font = self.font()
        font.setPointSize(16)
        self.setFont(font)

        self.bridge = UiBridge()
        self.bridge.log_message.connect(self.log)
        self.bridge.control_log_message.connect(self.control_log)
        self.bridge.data_received.connect(self.on_data_received)
        self.bridge.error_received.connect(self.on_error_received)
        self.bridge.task_status_received.connect(self.on_task_status_received)
        self.bridge.pcs_data_received.connect(self.on_pcs_data_received)
        self.bridge.pcs_error_received.connect(self.on_pcs_error_received)
        self.bridge.heartbeat_written.connect(self.on_heartbeat_written)
        self.bridge.heartbeat_error.connect(self.on_heartbeat_error)

        # Large-site mode: worker threads put latest snapshots into a cache.
        # The Qt main thread drains the cache on a timer instead of handling one
        # signal per device per polling cycle. This reduces Windows/PyInstaller UI
        # freezes when 40-60 devices are online.
        self._pending_bms_snapshots: Dict[str, Dict[str, Any]] = {}
        self._pending_bms_snapshot_lock = threading.Lock()
        self._pending_bms_snapshot_max_per_flush: int = 80

        self.device_workers: Dict[str, DeviceWorker] = {}
        self.pcs_workers: Dict[str, PcsPollingWorker] = {}
        self.task_status_store = TaskStatusStore()
        self.task_status_rows: Dict[str, int] = {}
        self.worker_start_stagger_seconds: float = 0.50
        self.large_site_mode_enabled: bool = True
        self.max_parallel_bms_io: int = 10
        DeviceWorker.configure_global_io_limit(self.max_parallel_bms_io)
        self.performance_mode_enabled: bool = True
        self.ui_refresh_interval: float = 3.0
        self.curve_refresh_interval: float = 5.0
        self.status_refresh_interval: float = 5.0
        self.log_flush_interval_ms: int = 1000
        self._last_curve_refresh_time: Dict[str, float] = {}
        self._last_status_refresh_time: float = 0.0
        self._last_ui_refresh_time: Dict[str, float] = {}
        self.hidden_dynamic_point_stride: int = 10
        self.max_driver_points_visible_rows: int = 300
        self.heartbeat_workers: Dict[str, HeartbeatWorker] = {}
        self.hv_workers: Dict[str, HvWorkflowWorker] = {}
        self.charge_discharge_workers: Dict[str, Any] = {}
        self.cluster_strategy_workers: Dict[str, ClusterStrategyWorker] = {}
        # CSV recorders are created only when the operator explicitly starts recording.
        # Polling/connection itself does NOT write CSV, which avoids Windows UI/disk lag.
        self.recorders: Dict[str, CsvRecorder] = {}
        self.pcs_recorders: Dict[str, CsvRecorder] = {}
        self.alarm_recorders: Dict[str, AlarmRecorder] = {}
        self.bms_csv_recording_devices: set[str] = set()
        self.pcs_csv_recording_devices: set[str] = set()
        self.service = BmsPcsService()
        self.device_controller = DeviceController(self)
        self.pcs_controller = PcsController(self)
        self.profile_controller = ProfileController(self)
        self.strategy_controller = StrategyController(self)
        self.audit_controller = AuditController(self)
        self.service_action_controller = ServiceActionController(self)
        self.app_facade = AppFacade(self)
        self.fleet_manager = FleetManager(
            log=lambda msg: self.bridge.log_message.emit(str(msg)),
            status_callback=lambda dn, status: self.bridge.task_status_received.emit(dn, status),
        )
        self.fleet_status_timer = QTimer(self)
        # Windows/PyInstaller: keep fleet status refresh lightweight. The UI does
        # not need 1 Hz full snapshot aggregation, and frequent refresh can make
        # Qt appear "Not responding" during reconnect storms.
        self.fleet_status_timer.setInterval(5000)
        self.fleet_status_timer.timeout.connect(self.refresh_fleet_heartbeat_status)
        self.fleet_status_timer.start()
        self._fleet_status_refresh_busy = False
        self._last_fleet_status_text = ""

        # Runtime/UI split defaults must exist before runtime_snapshot_timer is created.
        # app_runtime.py also instantiates MainWindow in hidden mode, so init order matters.
        self.runtime_api_enabled: bool = False
        self.runtime_dominant_mode: bool = True
        self.is_runtime_process: bool = False
        try:
            import os as _ess_runtime_os
            self.is_runtime_process = str(_ess_runtime_os.environ.get("ESS_AIO_RUNTIME_PROCESS", "")).strip().lower() in {"1", "true", "yes", "on"}
        except Exception:
            self.is_runtime_process = False
        self.runtime_api_url: str = "http://127.0.0.1:8765"
        self.runtime_api_client = RuntimeApiClient(self.runtime_api_url)
        self.runtime_snapshot_poll_interval_ms: int = 2000
        self.runtime_connection_status: str = "Disabled"
        self.runtime_watchdog_enabled: bool = True
        self.runtime_watchdog_interval_ms: int = 5000
        self._runtime_watchdog_failures: int = 0
        self._runtime_last_snapshot: Dict[str, Any] = {}
        self._runtime_last_snapshot_signature: str = ""
        self._runtime_last_device_state_signature: str = ""
        self.runtime_snapshot_only_mode: bool = True

        self.bms_snapshot_flush_timer = QTimer(self)
        self.bms_snapshot_flush_timer.setInterval(1000)
        self.bms_snapshot_flush_timer.timeout.connect(self._flush_pending_bms_snapshots)
        self.bms_snapshot_flush_timer.start()

        self.runtime_snapshot_timer = QTimer(self)
        self.runtime_snapshot_timer.setInterval(self.runtime_snapshot_poll_interval_ms)
        self.runtime_snapshot_timer.timeout.connect(self.poll_runtime_snapshot)

        self.runtime_watchdog_timer = QTimer(self)
        self.runtime_watchdog_timer.setInterval(self.runtime_watchdog_interval_ms)
        self.runtime_watchdog_timer.timeout.connect(self.check_runtime_watchdog)

        self.device_rows: Dict[str, int] = {}
        self.devices: List[Dict[str, Any]] = []
        # Keep user/project data outside the installation folder. This is important
        # for Windows/PyInstaller builds where Program Files/_MEIPASS may be read-only.
        self.profile_root = user_data_dir() / "profiles"
        self.current_profile_name: str = "default"
        self.current_profile_dir: Path = self.profile_root / self.current_profile_name
        self.current_profile_dir.mkdir(parents=True, exist_ok=True)
        self.startup_self_check_result = ensure_profile(self.current_profile_dir, resource_path("."))
        self.strategy_engine = StrategyEngine(self.current_profile_dir)
        self.template_manager = TemplateManager(self)
        self.fake_mode: bool = False
        self.bms_driver_key: str = DEFAULT_BMS_DRIVER
        self.pcs_driver_key: str = DEFAULT_PCS_DRIVER
        self.alarm_parser = AlarmParser(self.current_profile_dir / "alarm_map.json")
        self.pcs_configs: Dict[str, Dict[str, Any]] = {}
        self.current_pcs_name: str = ""
        self.latest_snapshots: Dict[str, Dict[str, Any]] = {}
        self.latest_pcs_snapshots: Dict[str, Dict[str, Any]] = {}
        self.packet_records = []
        self.debug_session: Dict[str, Any] = {"name": "Default Session", "started_at": "-", "ended_at": "-", "notes": ""}
        self.bms_last_heartbeat: Dict[str, int] = {}
        self.bms_heartbeat_same_count: Dict[str, int] = {}
        self.history_rows: list[dict[str, str]] = []
        self.history_csv_path: str = ""

        self.recent_buffers: Dict[str, deque] = defaultdict(lambda: deque(maxlen=300))
        self.series_buffers: Dict[str, Dict[str, deque]] = defaultdict(
            lambda: {
                "soc": deque(maxlen=300),
                "system_voltage": deque(maxlen=300),
                "system_current": deque(maxlen=300),
                "online": deque(maxlen=300),
            }
        )
        # v3.0 phase 3: dynamic point histories for driver-driven plotting.
        self.dynamic_point_buffers: Dict[str, Dict[str, deque]] = defaultdict(
            lambda: defaultdict(lambda: deque(maxlen=300))
        )
        self.selected_dynamic_points: list[str] = []
        # v3.7: CAN decoded signal histories share the curve system, but keep
        # a separate buffer so imported CAN logs do not pollute live BMS points.
        self.can_signal_buffers: Dict[str, deque] = defaultdict(lambda: deque(maxlen=20000))
        self.selected_can_signals: list[str] = []
        self.favorite_points: set[str] = set()
        self.sample_index: Dict[str, int] = defaultdict(int)

        self.current_curve_device: Optional[str] = None
        self.current_detail_device: Optional[str] = None
        self.current_alarm_device: Optional[str] = None
        self.current_control_device: Optional[str] = None

        self.site = Site(name="Default Site")
        self.default_cluster = Cluster(name="Cluster-1")
        self.site.clusters.append(self.default_cluster)

        self.pcs_config: Dict[str, Any] = self.load_pcs_config() or {}
        self.pcs_config.setdefault("driver", self.pcs_driver_key)
        # Do not create/bind a default PCS at startup. A site may be used for
        # BMS-only commissioning; PCS is added and connected manually by operator.
        if self.pcs_config.get("enabled") and self.pcs_config.get("name"):
            # Load PCS device instance so it is visible in the PCS Devices page,
            # but do NOT bind it to the default cluster and do NOT connect it.
            # BMS-only commissioning must never trigger PCS network traffic.
            self.current_pcs_name = str(self.pcs_config.get("name"))
            self.pcs_configs[self.current_pcs_name] = self.pcs_config


        self.last_error_message: str = "-"
        self.last_hv_status: str = "Idle"
        self.last_heartbeat_status: str = "Stopped"
        self.last_sampling_status: str = "Stopped"
        self.operation_log_file = None
        self.cutoff_alarm_states: Dict[str, Dict[str, bool]] = {}

        self.heartbeat_interval: float = 1.0
        self.hv_step_timeout: float = 30.0
        self.hv_poll_interval: float = 1.0
        self.pcs_zero_power_threshold: float = 0.1
        self.charge_cutoff_max_cell_voltage: float = 3650.0
        self.discharge_cutoff_min_cell_voltage: float = 2500.0

        self.power_derating_enabled: bool = False
        self.derating_margin_mv: float = 50.0
        self.derating_power_kw: float = 10.0
        self.derating_state: Dict[str, Dict[str, Any]] = {}
        self.last_user_power_kw: Dict[str, float] = {}

        self.power_tracking_enabled: bool = True
        self.power_tracking_tolerance_kw: float = 5.0
        self.power_tracking_confirm_count: int = 3
        self.power_tracking_counters: Dict[str, int] = {}

        self.power_tracking_auto_retry: bool = False
        self.power_tracking_retry_interval: int = 5  # 秒
        self.power_tracking_max_retry: int = 3

        self.power_tracking_retry_state: Dict[str, Dict[str, Any]] = {}

        self.pcs_fault_protection_enabled: bool = True
        self.pcs_fault_protection_mode: str = "Alarm Only"
        self.pcs_fault_confirm_count: int = 3
        self.pcs_control_ui_enabled: bool = True
        self.pcs_fault_counters: Dict[str, int] = {}

        # Runtime/UI split defaults are initialized before runtime_snapshot_timer.

        self.cutoff_mode: str = "Alarm Only"
        self.cutoff_action_latched: Dict[str, Dict[str, bool]] = {}

        self.cutoff_trigger_confirm_count: int = 3
        self.cutoff_recover_confirm_count: int = 3
        self.cutoff_counters: Dict[str, Dict[str, int]] = {}
        self.alarm_history_window_before_minutes: int = 5
        self.alarm_history_window_after_minutes: int = 5
        self.load_runtime_config()
        # Runtime/UI split phase 4: launcher can force the UI into API-client
        # mode without requiring the user to open Settings first.
        try:
            import os as _ess_aio_os
            if str(_ess_aio_os.environ.get("ESS_AIO_USE_RUNTIME_API", "")).strip().lower() in {"1", "true", "yes", "on"}:
                self.runtime_api_enabled = True
                self.runtime_dominant_mode = True
                self.runtime_api_url = str(_ess_aio_os.environ.get("ESS_AIO_RUNTIME_URL", self.runtime_api_url) or self.runtime_api_url).rstrip("/")
                self.runtime_api_client = RuntimeApiClient(self.runtime_api_url)
        except Exception:
            pass

        self.detail_value_labels: Dict[str, QLabel] = {}
        self.pcs_status_labels: Dict[str, QLabel] = {}

        self._build_ui()
        self._build_menu()
        self._apply_comfortable_style()

        self.auto_load_startup_configs()
        try:
            self.log(f"[INFO] User data dir: {user_data_dir()}")
            self.log(f"[INFO] Active profile dir: {self.current_profile_dir}")
            self.log(f"[INFO] Site config path: {self.get_profile_path('site_config.json')}")
        except Exception:
            pass

        self.refresh_global_status_bar()
        try:
            self.refresh_template_package_view()
        except Exception:
            pass
        if hasattr(self, "refresh_release_view"):
            self.refresh_release_view()

        self._apply_runtime_api_mode()


    # ------------------------------------------------------------------
    # Runtime/UI split phase 2 helpers
    # ------------------------------------------------------------------
    def _runtime_api_active(self) -> bool:
        # In the headless runtime process all commands must execute local workers.
        # In the UI process, Runtime Dominant Mode routes commands through HTTP API
        # so UI freezes/crashes do not stop polling/strategy/control.
        if bool(getattr(self, "is_runtime_process", False)):
            return False
        return bool(getattr(self, "runtime_api_enabled", False))

    def _runtime_should_handle_command(self) -> bool:
        return bool(self._runtime_api_active() and getattr(self, "runtime_dominant_mode", True))

    def _runtime_block_local_device_io(self, action: str = "local device operation", *, warn: bool = True) -> bool:
        """Return True when the UI process must not touch device IO locally.

        Runtime Dominant Mode means all Modbus polling/control/strategy work is
        owned by app_runtime.py.  UI code must not silently create local workers
        or direct clients, because that defeats runtime/UI separation and can race
        the runtime process.
        """
        if not self._runtime_should_handle_command():
            return False
        msg = f"Runtime Dominant Mode is enabled; blocked local UI device IO: {action}. Use Runtime API instead."
        try:
            self.log(f"[RUNTIME_UI][BLOCK] {msg}")
        except Exception:
            pass
        if warn:
            try:
                QMessageBox.warning(self, "Runtime Dominant", msg)
            except Exception:
                pass
        return True

    def _assert_local_device_io_allowed(self, action: str = "local device operation") -> None:
        if self._runtime_block_local_device_io(action, warn=False):
            raise RuntimeError(f"Blocked local UI device IO while Runtime Dominant Mode is enabled: {action}")

    def _runtime_client(self) -> RuntimeApiClient:
        url = str(getattr(self, "runtime_api_url", "http://127.0.0.1:8765") or "http://127.0.0.1:8765").rstrip("/")
        client = getattr(self, "runtime_api_client", None)
        if client is None or getattr(client, "base_url", "") != url:
            client = RuntimeApiClient(url)
            self.runtime_api_client = client
        return client

    def _runtime_post(self, path: str, payload: dict[str, Any] | None = None, *, timeout_s: float = 3.0) -> dict[str, Any]:
        return self._runtime_client().post(path, payload or {}, timeout_s=timeout_s)

    def _runtime_get(self, path: str, *, timeout_s: float = 1.5) -> dict[str, Any]:
        return self._runtime_client().get(path, timeout_s=timeout_s)

    def _apply_runtime_api_mode(self) -> None:
        enabled = self._runtime_api_active()
        dominant = bool(enabled and getattr(self, "runtime_dominant_mode", True))
        try:
            self.runtime_snapshot_timer.setInterval(int(getattr(self, "runtime_snapshot_poll_interval_ms", 2000)))
            if enabled and not self.runtime_snapshot_timer.isActive():
                self.runtime_snapshot_timer.start()
            elif not enabled and self.runtime_snapshot_timer.isActive():
                self.runtime_snapshot_timer.stop()
            if enabled and getattr(self, "runtime_watchdog_enabled", True):
                self.runtime_watchdog_timer.setInterval(int(getattr(self, "runtime_watchdog_interval_ms", 5000)))
                if not self.runtime_watchdog_timer.isActive():
                    self.runtime_watchdog_timer.start()
            elif self.runtime_watchdog_timer.isActive():
                self.runtime_watchdog_timer.stop()
        except Exception:
            pass

        # Phase 5.9: in Runtime Dominant mode the UI is a snapshot client.
        # Stop local runtime/UI timers that aggregate local worker state so the UI
        # cannot accidentally touch local workers or repaint based on stale local data.
        for timer_name in ("bms_snapshot_flush_timer", "fleet_status_timer"):
            try:
                timer = getattr(self, timer_name, None)
                if timer is None:
                    continue
                if dominant and timer.isActive():
                    timer.stop()
                elif (not dominant) and (not timer.isActive()):
                    timer.start()
            except Exception:
                pass

        if enabled:
            self.runtime_connection_status = "Runtime dominant snapshot client" if dominant else "Runtime API mode"
            try:
                self.log(f"[RUNTIME_UI] Runtime API mode enabled: {getattr(self, 'runtime_api_url', '')}; dominant={dominant}")
            except Exception:
                pass
        else:
            self.runtime_connection_status = "Local/direct mode"

    def _runtime_snapshot_client_mode(self) -> bool:
        return bool(self._runtime_api_active() and getattr(self, "runtime_dominant_mode", True) and getattr(self, "runtime_snapshot_only_mode", True))

    def check_runtime_watchdog(self) -> None:
        if not self._runtime_api_active():
            return
        info = self._runtime_get("/api/runtime/watchdog", timeout_s=1.0)
        if info.get("ok"):
            if getattr(self, "_runtime_watchdog_failures", 0):
                try:
                    self.log("[RUNTIME_UI] Runtime watchdog recovered")
                except Exception:
                    pass
            self._runtime_watchdog_failures = 0
            return
        self._runtime_watchdog_failures = int(getattr(self, "_runtime_watchdog_failures", 0) or 0) + 1
        self.runtime_connection_status = f"Runtime watchdog failed x{self._runtime_watchdog_failures}: {info.get('error', info)}"
        self.last_error_message = self.runtime_connection_status
        try:
            if self._runtime_watchdog_failures in {1, 3, 6} or self._runtime_watchdog_failures % 12 == 0:
                self.log(f"[RUNTIME_UI][WARN] {self.runtime_connection_status}")
        except Exception:
            pass
        try:
            self.refresh_global_status_bar()
        except Exception:
            pass


    def poll_runtime_snapshot(self) -> None:
        if not self._runtime_api_active():
            return
        snap = self._runtime_get("/api/snapshot", timeout_s=1.5)
        if not snap.get("ok"):
            self.runtime_connection_status = f"Runtime disconnected: {snap.get('error', '-') }"
            self.last_error_message = self.runtime_connection_status
            self.refresh_global_status_bar()
            return
        self.runtime_connection_status = "Runtime connected"
        self._apply_runtime_snapshot(snap)

    def _apply_runtime_snapshot(self, snap: dict[str, Any]) -> None:
        """Apply a lightweight runtime snapshot to UI caches.

        In phase 2 this keeps the UI as a monitor/client while preserving the
        existing local UI code.  It does not start local workers and it does not
        touch Modbus directly.
        """
        import json as _ess_json
        try:
            _sig_payload = {
                "snapshot_id": snap.get("snapshot_id", ""),
                "workers": snap.get("workers", {}),
                "device_states": snap.get("device_states", {}),
                "recording": snap.get("recording", {}),
                "last_command": snap.get("last_command", {}),
            }
            snapshot_signature = _ess_json.dumps(_sig_payload, sort_keys=True, default=str)
        except Exception:
            snapshot_signature = str(snap.get("snapshot_id", ""))
        only_meta_changed = snapshot_signature == getattr(self, "_runtime_last_snapshot_signature", "")
        self._runtime_last_snapshot_signature = snapshot_signature
        self._runtime_last_snapshot = dict(snap)
        bms = snap.get("bms", {}) if isinstance(snap.get("bms"), dict) else {}
        pcs = snap.get("pcs", {}) if isinstance(snap.get("pcs"), dict) else {}
        workers = snap.get("workers", {}) if isinstance(snap.get("workers"), dict) else {}
        device_states = snap.get("device_states", {}) if isinstance(snap.get("device_states"), dict) else {}
        task_status = snap.get("task_status", {}) if isinstance(snap.get("task_status"), dict) else {}
        recording = snap.get("recording", {}) if isinstance(snap.get("recording"), dict) else {}
        try:
            self.bms_csv_recording_devices = set(recording.get("bms_csv", []) or [])
            self.pcs_csv_recording_devices = set(recording.get("pcs_csv", []) or [])
            if hasattr(self, "update_bms_csv_status_label"):
                self.update_bms_csv_status_label()
            if hasattr(self, "update_pcs_csv_status_label"):
                self.update_pcs_csv_status_label()
        except Exception:
            pass

        # Replace UI runtime caches with the runtime process caches.
        self.latest_snapshots = {str(k): dict(v) for k, v in bms.items() if isinstance(v, dict)}
        self.latest_pcs_snapshots = {str(k): dict(v) for k, v in pcs.items() if isinstance(v, dict)}

        # Runtime-dominant UI still needs local chart/detail caches for display,
        # but those caches must be populated only from Runtime snapshots, not from
        # local Modbus workers or CSV writers.
        try:
            self._ingest_runtime_snapshots_for_display(bms)
        except Exception:
            pass

        # Mirror runtime task status into the client UI so connection failures show
        # Offline/Error instead of looking silently idle.
        try:
            if hasattr(self, "task_status_store"):
                for name, row in task_status.items():
                    if isinstance(row, dict):
                        self.task_status_store.update(
                            str(name),
                            status=str(row.get("status", "Idle")),
                            reads=int(row.get("reads", 0) or 0),
                            errors=int(row.get("errors", 0) or 0),
                            last_latency_ms=float(row.get("last_latency_ms", 0.0) or 0.0),
                            last_message=str(row.get("last_message", "-")),
                        )
        except Exception:
            pass

        bms_running = set(str(x) for x in workers.get("bms_running", []) or [])
        pcs_running = set(str(x) for x in workers.get("pcs_running", []) or [])
        strategies = set(str(x) for x in workers.get("strategies", []) or [])
        self.last_sampling_status = f"Runtime BMS: {len(bms_running)} running"
        if strategies:
            self.last_hv_status = f"Strategies: {len(strategies)}"

        # Update local device status tables from runtime snapshot without local workers.
        try:
            for dev in getattr(self, "devices", []) or []:
                name = str(dev.get("name", ""))
                row = self.device_rows.get(name)
                if row is None:
                    continue
                running = name in bms_running
                snap_dev = self.latest_snapshots.get(name, {})
                state = (device_states.get("bms", {}) or {}).get(name, {}) if isinstance(device_states.get("bms", {}), dict) else {}
                connection = str(state.get("connection") or ("online" if running and snap_dev else ("connecting" if running else "stopped")))
                status_text = str(state.get("status") or ("Running" if running else "Stopped"))
                message_text = str(state.get("last_message") or connection)
                for col, val in ((7, snap_dev.get("soc", "-")), (8, snap_dev.get("system_voltage", "-")), (9, snap_dev.get("system_current", "-")), (10, snap_dev.get("system_power", "-"))):
                    item = self.device_table.item(row, col)
                    if item is not None and item.text() != str(val):
                        item.setText(str(val))
                for col, val in ((11, status_text), (12, f"{connection}: {message_text}" if message_text != connection else connection)):
                    item = self.device_table.item(row, col)
                    if item is not None and item.text() != str(val):
                        item.setText(str(val))
        except Exception:
            pass


        try:
            pcs_state_map = device_states.get("pcs", {}) if isinstance(device_states.get("pcs", {}), dict) else {}
            table = getattr(self, "pcs_device_table", None)
            if table is not None and hasattr(self, "pcs_rows"):
                for name, row in getattr(self, "pcs_rows", {}).items():
                    state = pcs_state_map.get(str(name), {}) if isinstance(pcs_state_map, dict) else {}
                    if not state:
                        continue
                    status_text = str(state.get("status") or state.get("connection") or "-")
                    msg = str(state.get("last_message") or state.get("connection") or "-")
                    # Best-effort update: PCS tables have changed across versions, so only
                    # update common trailing columns if they exist.
                    for col, val in ((max(0, table.columnCount() - 2), status_text), (max(0, table.columnCount() - 1), msg)):
                        try:
                            item = table.item(int(row), int(col))
                            if item is not None and item.text() != str(val):
                                item.setText(str(val))
                        except Exception:
                            pass
        except Exception:
            pass

        try:
            # Runtime Dominant UI is snapshot-driven. If no meaningful state changed,
            # avoid repainting overview/status on every poll.
            if not only_meta_changed:
                self.refresh_overview()
            self.refresh_global_status_bar()
        except Exception:
            pass


    def _ingest_runtime_snapshots_for_display(self, bms: dict[str, Any]) -> None:
        """Populate UI-only display buffers from Runtime snapshots.

        This keeps Curves/Details/Alarms usable in Runtime Dominant mode without
        allowing the UI process to own Modbus polling, CSV recording, or strategy.
        """
        if not self._runtime_snapshot_client_mode():
            return
        import time
        now = time.time()
        for device_name, raw in (bms or {}).items():
            if not isinstance(raw, dict):
                continue
            device_name = str(device_name)
            snapshot = dict(raw)
            idx = self.sample_index[device_name]
            self.sample_index[device_name] += 1
            self.recent_buffers[device_name].append(snapshot)
            def _f(key: str) -> float:
                try:
                    return float(snapshot.get(key, 0) or 0)
                except Exception:
                    return 0.0
            self.series_buffers[device_name]["soc"].append((idx, _f("soc")))
            self.series_buffers[device_name]["system_voltage"].append((idx, _f("system_voltage")))
            self.series_buffers[device_name]["system_current"].append((idx, _f("system_current")))
            online = 1.0 if device_name in set((getattr(self, "_runtime_last_snapshot", {}) or {}).get("workers", {}).get("bms_running", []) or []) else 0.0
            self.series_buffers[device_name]["online"].append((idx, online))
            # Only build large dynamic point histories for the currently visible device.
            if self.current_curve_device == device_name and self._is_main_page_visible("Curves"):
                points = snapshot.get("points", {}) if isinstance(snapshot.get("points"), dict) else snapshot
                for key, value in points.items():
                    if key in {"timestamp", "raw", "point_meta", "points"}:
                        continue
                    try:
                        self.dynamic_point_buffers[device_name][str(key)].append((idx, float(value)))
                    except Exception:
                        pass

    def closeEvent(self, event) -> None:  # type: ignore[override]
        try:
            if getattr(self, "_runtime_api_active", lambda: False)():
                # UI is a client of a separate Runtime. Do not run local stop_all().
                # Offer a safe Runtime shutdown path for users who started the full launcher.
                try:
                    from PySide6.QtWidgets import QMessageBox
                    box = QMessageBox(self)
                    box.setWindowTitle("Close ESS-AIO")
                    box.setText("Close UI only, or also stop ESS-AIO Runtime?")
                    box.setInformativeText("Stopping Runtime will stop polling, CSV recording, strategy and the Web EMS server.")
                    close_only = box.addButton("Close UI Only", QMessageBox.ButtonRole.AcceptRole)
                    stop_runtime = box.addButton("Stop Runtime Too", QMessageBox.ButtonRole.DestructiveRole)
                    cancel = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
                    box.exec()
                    clicked = box.clickedButton()
                    if clicked is cancel:
                        event.ignore()
                        return
                    if clicked is stop_runtime:
                        result = self._runtime_client().shutdown()
                        try:
                            self.log(f"[RUNTIME_UI] Runtime shutdown requested on UI close: {result}")
                        except Exception:
                            pass
                except Exception:
                    pass
            else:
                self.stop_all()
        finally:
            # Flush async log queue without blocking the Qt UI for a long time.
            try:
                if hasattr(self, "shutdown_async_logging"):
                    self.shutdown_async_logging(timeout=1.0)
            except Exception:
                pass
        super().closeEvent(event)


def run() -> None:
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(True)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
