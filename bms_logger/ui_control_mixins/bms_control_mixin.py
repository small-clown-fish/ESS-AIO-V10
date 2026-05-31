from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QMessageBox

from ..hv_controller import HvWorkflowController, HvWorkflowWorker
from ..modbus_client import BmsModbusClient
from ..pcs_client import PcsClient
from ..client_factory import create_bms_client, create_pcs_client
from ..worker import HeartbeatWorker


class BmsControlMixin:
    def _get_bms_polling_worker(self, device_name: str):
        worker = getattr(self, "device_workers", {}).get(device_name)
        if worker is not None and getattr(worker, "running", False) and hasattr(worker, "enqueue_command"):
            return worker
        return None

    def _ensure_bms_timer_store(self) -> None:
        if not hasattr(self, "bms_queue_heartbeat_timers"):
            self.bms_queue_heartbeat_timers = {}
        if not hasattr(self, "bms_queue_heartbeat_values"):
            self.bms_queue_heartbeat_values = {}
        if not hasattr(self, "bms_038b_timer"):
            self.bms_038b_timer = None

    def _enqueue_bms_worker_command(self, device_name: str, method_name: str, *args, label: str = "") -> bool:
        worker = self._get_bms_polling_worker(device_name)
        if worker is None:
            return False
        def _ok_callback(name, _result, _label=label or method_name):
            # Heartbeat success is intentionally not written into Control Log on
            # every cycle. The heartbeat label/status still updates via
            # heartbeat_written, while errors remain visible through
            # heartbeat_error. This prevents QTextEdit repaint/log storms.
            if str(_label).lower().startswith("heartbeat="):
                return
            self.control_log(f"[BMS][QUEUE] {name}: {_label} OK")

        return bool(worker.enqueue_command(
            method_name,
            *args,
            label=label or method_name,
            callback=_ok_callback,
            error_callback=lambda name, error: self.bridge.heartbeat_error.emit(name, error),
        ))

    def _online_bms_worker_names(self) -> list[str]:
        names: list[str] = []
        for name, worker in getattr(self, "device_workers", {}).items():
            try:
                if getattr(worker, "running", False) and hasattr(worker, "enqueue_command"):
                    names.append(str(name))
            except Exception:
                pass
        return names

    def _queue_all_online_bms_command(self, method_name: str, *args, label: str = "") -> tuple[int, int]:
        names = self._online_bms_worker_names()
        queued = 0
        for name in names:
            if self._enqueue_bms_worker_command(name, method_name, *args, label=label or method_name):
                queued += 1
        return queued, len(names)

    def _start_bms_queue_heartbeat(self, device_name: str) -> bool:
        self._ensure_bms_timer_store()
        worker = self._get_bms_polling_worker(device_name)
        if worker is None:
            return False
        if device_name in self.bms_queue_heartbeat_timers:
            return True
        self.bms_queue_heartbeat_values.setdefault(device_name, 0)
        timer = QTimer(self)
        timer.setInterval(max(200, int(float(getattr(self, "heartbeat_interval", 1.0)) * 1000)))

        def tick(name=device_name):
            # If the BMS polling worker has stopped, stop this heartbeat timer too.
            # Do NOT keep emitting "worker not running" every interval, otherwise
            # Control Log/Qt repaint will loop and Windows can become unresponsive.
            worker = self._get_bms_polling_worker(name)
            if worker is None:
                try:
                    self._stop_bms_queue_heartbeat(name)
                except Exception:
                    pass
                try:
                    self.bridge.heartbeat_error.emit(name, "BMS polling worker stopped; heartbeat timer stopped")
                except Exception:
                    pass
                return

            value = int(self.bms_queue_heartbeat_values.get(name, 0)) % 256
            ok = self._enqueue_bms_worker_command(name, "write_heartbeat", value, label=f"heartbeat={value}")
            if ok:
                self.bridge.heartbeat_written.emit(name, value)
                self.bms_queue_heartbeat_values[name] = (value + 1) % 256
            else:
                try:
                    self._stop_bms_queue_heartbeat(name)
                except Exception:
                    pass
                self.bridge.heartbeat_error.emit(name, "BMS heartbeat not queued; timer stopped")

        timer.timeout.connect(tick)
        self.bms_queue_heartbeat_timers[device_name] = timer
        timer.start()
        tick()
        return True

    def _stop_bms_queue_heartbeat(self, device_name: str) -> bool:
        self._ensure_bms_timer_store()
        timer = self.bms_queue_heartbeat_timers.pop(device_name, None)
        if timer is None:
            return False
        timer.stop()
        timer.deleteLater()
        return True

    def handle_start_heartbeat(self) -> None:
        device_name = self._get_selected_control_device()
        if not device_name:
            return

        # Preferred path: if BMS monitoring is running, reuse the polling worker's
        # own Modbus client and queue heartbeat writes behind reads. This avoids
        # a second TCP connection to the same BMS.
        if self._start_bms_queue_heartbeat(device_name):
            self.heartbeat_state_label.setText("Queued on BMS worker")
            self.control_state_label.setText("Running")
            self.last_control_result_label.setText("Heartbeat queued on polling worker")
            self.control_log(f"[CONTROL] {device_name}: Heartbeat queued on BMS polling worker")
            return

        self.heartbeat_state_label.setText("Not started")
        self.control_state_label.setText("Idle")
        self.last_control_result_label.setText("Start BMS monitoring first")
        self.control_log(f"[CONTROL][WARN] {device_name}: Heartbeat not started because BMS polling worker is not running. No separate reconnecting heartbeat connection was opened.")
        QMessageBox.information(
            self,
            "BMS Heartbeat",
            "Start BMS monitoring first. Heartbeat writes are queued on the existing BMS polling worker to avoid extra reconnect loops."
        )
        return

    def handle_stop_heartbeat(self) -> None:
        device_name = self._get_selected_control_device()
        if not device_name:
            return

        stopped_queue = self._stop_bms_queue_heartbeat(device_name)
        worker = self.heartbeat_workers.get(device_name)
        if worker:
            worker.stop()
            worker.join(timeout=3.0)
            self.heartbeat_workers.pop(device_name, None)

        if stopped_queue or worker:
            self.heartbeat_state_label.setText("Stopped")
            self.control_state_label.setText("Idle")
            self.last_control_result_label.setText("Heartbeat stopped")
            self.control_log(f"[CONTROL] {device_name}: Heartbeat stopped")
        else:
            self.heartbeat_state_label.setText("Stopped")
            self.control_state_label.setText("Idle")
            self.last_control_result_label.setText("Heartbeat not running")
            self.control_log(f"[CONTROL] {device_name}: Heartbeat not running")
        self.last_heartbeat_status = "Stopped"
        self.refresh_global_status_bar()

    def _write_ems_cmd(self, device_name: str, cmd_value: int, cmd_name: str, confirm: bool) -> None:
        if confirm:
            reply = QMessageBox.question(
                self,
                f"Confirm {cmd_name}",
                f"Write EMS cmd {cmd_value} ({cmd_name}) to {device_name}?",
            )
            if reply != QMessageBox.Yes:
                return

        if getattr(self, "_runtime_should_handle_command", lambda: False)():
            command = {1: "stay", 2: "power_on", 3: "power_off"}.get(int(cmd_value), "")
            result = self._runtime_post("/api/bms/command", {"scope": "single", "device": device_name, "command": command}, timeout_s=5.0) if hasattr(self, "_runtime_post") else {"ok": False, "error": "runtime helper missing"}
            if result.get("ok"):
                self.control_state_label.setText("Queued")
                self.last_control_result_label.setText(f"Runtime EMS cmd {cmd_value} queued")
                self.control_log(f"[RUNTIME_UI] {device_name}: EMS cmd {cmd_value} ({cmd_name}) sent to runtime")
                self.poll_runtime_snapshot() if hasattr(self, "poll_runtime_snapshot") else None
            else:
                QMessageBox.warning(self, "Runtime API", f"EMS cmd failed: {result.get('error', result)}")
            return

        if self._enqueue_bms_worker_command(device_name, "write_ems_cmd", int(cmd_value), label=f"EMS cmd {cmd_value} ({cmd_name})"):
            self.control_state_label.setText("Queued")
            self.last_control_result_label.setText(f"EMS cmd {cmd_value} queued")
            self.last_ems_cmd_result_label.setText("Queued on BMS worker")
            self.control_log(f"[CONTROL] {device_name}: EMS cmd {cmd_value} ({cmd_name}) queued on BMS polling worker")
            return

        self.control_state_label.setText("Not queued")
        self.last_control_result_label.setText("Start BMS monitoring first")
        self.last_ems_cmd_result_label.setText("No BMS worker")
        self.control_log(
            f"[CONTROL][WARN] {device_name}: EMS cmd {cmd_value} ({cmd_name}) not sent. "
            "Start BMS monitoring first so the command can reuse the BMS polling worker queue. "
            "No direct fallback connection was opened."
        )
        QMessageBox.information(
            self,
            "BMS command",
            "Start BMS monitoring first. Single and batch BMS commands are sent only through the existing BMS polling worker queue to avoid reconnect storms and transaction-id conflicts.",
        )
        return

    def handle_ems_cmd_stay(self) -> None:
        device_name = self._get_selected_control_device()
        if device_name:
            self._write_ems_cmd(device_name, 1, "Stay", confirm=False)

    def handle_ems_cmd_power_on(self) -> None:
        device_name = self._get_selected_control_device()
        if device_name:
            self._write_ems_cmd(device_name, 2, "Power On", confirm=True)

    def handle_ems_cmd_power_off(self) -> None:
        device_name = self._get_selected_control_device()
        if device_name:
            self._write_ems_cmd(device_name, 3, "Power Off", confirm=True)

    def handle_clear_fault(self) -> None:
        device_name = self._get_selected_control_device()
        if not device_name:
            return

        reply = QMessageBox.question(
            self,
            "Confirm Clear Fault",
            f"Send clear fault command to {device_name}?",
        )
        if reply != QMessageBox.Yes:
            return

        if getattr(self, "_runtime_should_handle_command", lambda: False)():
            result = self._runtime_post("/api/bms/command", {"scope": "single", "device": device_name, "command": "clear_fault"}, timeout_s=5.0) if hasattr(self, "_runtime_post") else {"ok": False, "error": "runtime helper missing"}
            if result.get("ok"):
                self.control_state_label.setText("Queued")
                self.last_control_result_label.setText("Runtime Clear Fault queued")
                self.control_log(f"[RUNTIME_UI] {device_name}: Clear Fault sent to runtime")
                self.poll_runtime_snapshot() if hasattr(self, "poll_runtime_snapshot") else None
            else:
                QMessageBox.warning(self, "Runtime API", f"Clear Fault failed: {result.get('error', result)}")
            return

        if self._enqueue_bms_worker_command(device_name, "clear_fault", label="Clear Fault"):
            self.control_state_label.setText("Queued")
            self.last_control_result_label.setText("Clear Fault queued")
            self.control_log(f"[CONTROL] {device_name}: Clear Fault queued on BMS polling worker")
            return

        self.control_state_label.setText("Not queued")
        self.last_control_result_label.setText("Start BMS monitoring first")
        self.control_log(
            f"[CONTROL][WARN] {device_name}: Clear Fault not sent. "
            "Start BMS monitoring first so the command can reuse the BMS polling worker queue. "
            "No direct fallback connection was opened."
        )
        QMessageBox.information(
            self,
            "Clear Fault",
            "Start BMS monitoring first. Single and batch BMS commands are sent only through the existing BMS polling worker queue to avoid reconnect storms and transaction-id conflicts.",
        )
        return

    def handle_clear_fault_all_online_bms(self) -> None:
        names = self._online_bms_worker_names()
        if not names:
            QMessageBox.information(self, "Clear Fault All", "No online/running BMS polling workers.")
            return
        reply = QMessageBox.question(self, "Confirm Clear Fault All", f"Clear fault for {len(names)} online BMS?")
        if reply != QMessageBox.Yes:
            return
        if getattr(self, "_runtime_should_handle_command", lambda: False)():
            result = self._runtime_post("/api/bms/command", {"scope": "all_online", "command": "clear_fault"}, timeout_s=5.0) if hasattr(self, "_runtime_post") else {"ok": False, "error": "runtime helper missing"}
            if result.get("ok"):
                self.last_control_result_label.setText(f"Runtime Clear Fault All queued: {result.get('queued', 0)}/{result.get('total', 0)}")
                self.control_log(f"[RUNTIME_UI] Clear Fault All sent to runtime: {result.get('queued', 0)}/{result.get('total', 0)}")
                self.poll_runtime_snapshot() if hasattr(self, "poll_runtime_snapshot") else None
            else:
                QMessageBox.warning(self, "Runtime API", f"Clear Fault All failed: {result.get('error', result)}")
            return
        queued, total = self._queue_all_online_bms_command("clear_fault", label="Clear Fault All")
        self.control_state_label.setText("Queued" if queued else "Idle")
        self.last_control_result_label.setText(f"Clear Fault queued: {queued}/{total}")
        self.control_log(f"[BMS][BULK] Clear Fault queued for online BMS: {queued}/{total}")

    def handle_power_on_all_online_bms(self) -> None:
        names = self._online_bms_worker_names()
        if not names:
            QMessageBox.information(self, "Power On All", "No online/running BMS polling workers.")
            return
        reply = QMessageBox.question(self, "Confirm Power On All", f"Write EMS Power On (2) to {len(names)} online BMS?")
        if reply != QMessageBox.Yes:
            return
        if getattr(self, "_runtime_should_handle_command", lambda: False)():
            result = self._runtime_post("/api/bms/command", {"scope": "all_online", "command": "power_on"}, timeout_s=5.0) if hasattr(self, "_runtime_post") else {"ok": False, "error": "runtime helper missing"}
            if result.get("ok"):
                self.last_control_result_label.setText(f"Runtime Power On All queued: {result.get('queued', 0)}/{result.get('total', 0)}")
                self.control_log(f"[RUNTIME_UI] Power On All sent to runtime: {result.get('queued', 0)}/{result.get('total', 0)}")
                self.poll_runtime_snapshot() if hasattr(self, "poll_runtime_snapshot") else None
            else:
                QMessageBox.warning(self, "Runtime API", f"Power On All failed: {result.get('error', result)}")
            return
        queued, total = self._queue_all_online_bms_command("write_ems_cmd", 2, label="EMS cmd 2 (Power On) All")
        self.control_state_label.setText("Queued" if queued else "Idle")
        self.last_control_result_label.setText(f"Power On queued: {queued}/{total}")
        if hasattr(self, "last_ems_cmd_result_label"):
            self.last_ems_cmd_result_label.setText(f"Power On queued: {queued}/{total}")
        self.control_log(f"[BMS][BULK] EMS Power On queued for online BMS: {queued}/{total}")


    def handle_power_off_all_online_bms(self) -> None:
        names = self._online_bms_worker_names()
        if not names:
            QMessageBox.information(self, "Power Off All", "No online/running BMS polling workers.")
            return
        reply = QMessageBox.question(self, "Confirm Power Off All", f"Write EMS Power Off (3) to {len(names)} online BMS?")
        if reply != QMessageBox.Yes:
            return
        if getattr(self, "_runtime_should_handle_command", lambda: False)():
            result = self._runtime_post("/api/bms/command", {"scope": "all_online", "command": "power_off"}, timeout_s=5.0) if hasattr(self, "_runtime_post") else {"ok": False, "error": "runtime helper missing"}
            if result.get("ok"):
                self.last_control_result_label.setText(f"Runtime Power Off All queued: {result.get('queued', 0)}/{result.get('total', 0)}")
                self.control_log(f"[RUNTIME_UI] Power Off All sent to runtime: {result.get('queued', 0)}/{result.get('total', 0)}")
                self.poll_runtime_snapshot() if hasattr(self, "poll_runtime_snapshot") else None
            else:
                QMessageBox.warning(self, "Runtime API", f"Power Off All failed: {result.get('error', result)}")
            return
        queued, total = self._queue_all_online_bms_command("write_ems_cmd", 3, label="EMS cmd 3 (Power Off) All")
        self.control_state_label.setText("Queued" if queued else "Idle")
        self.last_control_result_label.setText(f"Power Off queued: {queued}/{total}")
        if hasattr(self, "last_ems_cmd_result_label"):
            self.last_ems_cmd_result_label.setText(f"Power Off queued: {queued}/{total}")
        self.control_log(f"[BMS][BULK] EMS Power Off queued for online BMS: {queued}/{total}")

    def handle_stay_all_online_bms(self) -> None:
        names = self._online_bms_worker_names()
        if not names:
            QMessageBox.information(self, "Stay All", "No online/running BMS polling workers.")
            return
        if getattr(self, "_runtime_should_handle_command", lambda: False)():
            result = self._runtime_post("/api/bms/command", {"scope": "all_online", "command": "stay"}, timeout_s=5.0) if hasattr(self, "_runtime_post") else {"ok": False, "error": "runtime helper missing"}
            if result.get("ok"):
                self.last_control_result_label.setText(f"Runtime Stay queued: {result.get('queued', 0)}/{result.get('total', 0)}")
                self.control_log(f"[RUNTIME_UI] Stay All sent to runtime: {result.get('queued', 0)}/{result.get('total', 0)}")
                self.poll_runtime_snapshot() if hasattr(self, "poll_runtime_snapshot") else None
            else:
                QMessageBox.warning(self, "Runtime API", f"Stay All failed: {result.get('error', result)}")
            return
        queued, total = self._queue_all_online_bms_command("write_ems_cmd", 1, label="EMS cmd 1 (Stay) All")
        self.control_state_label.setText("Queued" if queued else "Idle")
        self.last_control_result_label.setText(f"Stay queued: {queued}/{total}")
        if hasattr(self, "last_ems_cmd_result_label"):
            self.last_ems_cmd_result_label.setText(f"Stay queued: {queued}/{total}")
        self.control_log(f"[BMS][BULK] EMS Stay queued for online BMS: {queued}/{total}")

    # ------------------------------------------------------------------
    # Fleet BMS heartbeat and 0x038B periodic insulation-monitor disable
    # ------------------------------------------------------------------
    def handle_start_all_bms_heartbeats(self) -> None:
        if getattr(self, "_runtime_should_handle_command", lambda: False)():
            result = self._runtime_post("/api/bms/heartbeat/start-all", timeout_s=3.0) if hasattr(self, "_runtime_post") else {"ok": False, "error": "runtime helper missing"}
            if result.get("ok"):
                self.control_log(f"[RUNTIME_UI] BMS heartbeat all sent to runtime: {result.get('started', 0)}/{result.get('total', 0)}")
            else:
                QMessageBox.warning(self, "Runtime API", f"BMS heartbeat start-all failed: {result.get('error', result)}")
            return
        # Only start heartbeat for BMS devices that already have a running polling
        # worker. This avoids creating heartbeat timers for stopped/offline devices
        # and prevents repeated "worker not running" log loops.
        bms_names = self._online_bms_worker_names()
        if not bms_names:
            QMessageBox.information(self, "BMS Heartbeat", "No running BMS polling workers. Start BMS monitoring first.")
            return

        queued = []
        for name in bms_names:
            if self._start_bms_queue_heartbeat(name):
                queued.append(name)
        self.heartbeat_state_label.setText(f"BMS HB queued: {len(queued)}/{len(bms_names)}")
        self.control_state_label.setText("Running" if queued else "Idle")
        self.last_control_result_label.setText(f"BMS HB queued: {len(queued)}/{len(bms_names)}")
        self.refresh_fleet_heartbeat_status()
        self.control_log(f"[BMS][QUEUE] Heartbeat queued on running polling workers: {len(queued)}/{len(bms_names)}")

    def handle_stop_all_bms_heartbeats(self) -> None:
        if getattr(self, "_runtime_should_handle_command", lambda: False)():
            result = self._runtime_post("/api/bms/heartbeat/stop-all", timeout_s=3.0) if hasattr(self, "_runtime_post") else {"ok": False, "error": "runtime helper missing"}
            if result.get("ok"):
                self.control_log(f"[RUNTIME_UI] BMS heartbeat stop-all sent to runtime: stopped={result.get('stopped', 0)}")
            else:
                QMessageBox.warning(self, "Runtime API", f"BMS heartbeat stop-all failed: {result.get('error', result)}")
            return
        self._ensure_bms_timer_store()
        queued_count = 0
        for name in list(self.bms_queue_heartbeat_timers.keys()):
            if self._stop_bms_queue_heartbeat(name):
                queued_count += 1
        fleet_count = self.fleet_manager.stop("BMS")
        for name, worker in list(getattr(self, "heartbeat_workers", {}).items()):
            try:
                worker.stop(); worker.join(timeout=2.0)
            except Exception:
                pass
            self.heartbeat_workers.pop(name, None)
        self.heartbeat_state_label.setText("Stopped")
        self.control_state_label.setText("Idle")
        self.last_control_result_label.setText(f"BMS HB stopped: queued={queued_count}, fleet={fleet_count}")
        self.last_heartbeat_status = "BMS HB stopped"
        self.refresh_global_status_bar()
        self.control_log(f"[BMS] Heartbeat stopped: queued={queued_count}, fleet={fleet_count}")

    def handle_start_bms_insulation_disable_cycle(self) -> None:
        if getattr(self, "_runtime_should_handle_command", lambda: False)():
            result = self._runtime_post("/api/bms/038b/start", timeout_s=3.0) if hasattr(self, "_runtime_post") else {"ok": False, "error": "runtime helper missing"}
            if result.get("ok"):
                self.control_log("[RUNTIME_UI] BMS 0x038B cycle start sent to runtime")
            else:
                QMessageBox.warning(self, "Runtime API", f"0x038B cycle start failed: {result.get('error', result)}")
            return
        bms_names = self._fleet_bms_names() if hasattr(self, "_fleet_bms_names") else [str(d.get("name", "")).strip() for d in getattr(self, "devices", []) if str(d.get("name", "")).strip()]
        if not bms_names:
            QMessageBox.information(self, "BMS 038B Cycle", "No BMS devices configured.")
            return
        interval_min = int(self.bms_insulation_interval_spin.value()) if hasattr(self, "bms_insulation_interval_spin") else 15
        self._ensure_bms_timer_store()
        if self.bms_038b_timer is not None:
            self.bms_038b_timer.stop(); self.bms_038b_timer.deleteLater()
        timer = QTimer(self)
        timer.setInterval(max(10_000, int(interval_min * 60_000)))

        def tick():
            queued = 0
            skipped = []
            for name in bms_names:
                if self._enqueue_bms_worker_command(name, "write_insulation_monitor_disable", label="write 0x038B=2"):
                    queued += 1
                else:
                    skipped.append(name)
            self.control_log(f"[BMS][QUEUE] 0x038B=2 queued: {queued}/{len(bms_names)}")
            if skipped:
                self.control_log(f"[BMS][QUEUE][WARN] 0x038B skipped because polling worker is not running: {', '.join(skipped[:8])}")

        timer.timeout.connect(tick)
        self.bms_038b_timer = timer
        timer.start()
        tick()
        if hasattr(self, "bms_insulation_state_label"):
            self.bms_insulation_state_label.setText(f"038B cycle queued / {interval_min} min")
        self.last_control_result_label.setText(f"038B cycle queued for {len(bms_names)} BMS")
        self.refresh_fleet_heartbeat_status()
        self.control_log(f"[BMS][QUEUE] Enabled periodic 0x038B=2 via BMS polling worker queue, interval={interval_min} min")

    def handle_stop_bms_insulation_disable_cycle(self) -> None:
        if getattr(self, "_runtime_should_handle_command", lambda: False)():
            result = self._runtime_post("/api/bms/038b/stop", timeout_s=3.0) if hasattr(self, "_runtime_post") else {"ok": False, "error": "runtime helper missing"}
            if result.get("ok"):
                self.control_log("[RUNTIME_UI] BMS 0x038B cycle stop sent to runtime")
            else:
                QMessageBox.warning(self, "Runtime API", f"0x038B cycle stop failed: {result.get('error', result)}")
            return
        self._ensure_bms_timer_store()
        stopped = 0
        if self.bms_038b_timer is not None:
            self.bms_038b_timer.stop(); self.bms_038b_timer.deleteLater(); self.bms_038b_timer = None
            stopped = 1
        fleet_count = self.fleet_manager.disable_bms_insulation_disable(None)
        if hasattr(self, "bms_insulation_state_label"):
            self.bms_insulation_state_label.setText("038B cycle: stopped")
        self.last_control_result_label.setText(f"038B cycle stopped: queued={stopped}, fleet={fleet_count}")
        self.control_log(f"[BMS] Disabled periodic 0x038B=2: queued_timer={stopped}, fleet={fleet_count}")

    def refresh_fleet_heartbeat_status(self) -> None:
        if getattr(self, "_runtime_snapshot_client_mode", lambda: False)():
            try:
                snap = getattr(self, "_runtime_last_snapshot", {}) or {}
                summary = snap.get("summary", {}) if isinstance(snap.get("summary"), dict) else {}
                parts = []
                if summary:
                    parts.append(f"BMS {summary.get('bms_online', 0)}/{summary.get('bms_total', 0)}")
                    parts.append(f"PCS {summary.get('pcs_online', 0)}/{summary.get('pcs_total', 0)}")
                text = " | ".join(parts) if parts else "Runtime snapshot"
                if text == getattr(self, "_last_fleet_status_text", ""):
                    return
                self._last_fleet_status_text = text
                self.last_heartbeat_status = text
                if hasattr(self, "heartbeat_state_label"):
                    self.heartbeat_state_label.setText(text)
                self.refresh_global_status_bar()
            except Exception:
                pass
            return
        if not hasattr(self, "fleet_manager"):
            return
        # Re-entry guard: on Windows, a reconnect storm can make one refresh take
        # longer than the timer interval. Never stack multiple UI refreshes.
        if getattr(self, "_fleet_status_refresh_busy", False):
            return
        self._fleet_status_refresh_busy = True
        try:
            snapshots = self.fleet_manager.snapshots()
            if not snapshots:
                return
            bms_online = bms_total = pcs_online = pcs_total = bms_038b = 0
            for key, value in snapshots.items():
                if key.startswith("BMS:"):
                    bms_total += 1
                    if value.get("online"):
                        bms_online += 1
                    if "bms_insulation_disable_038b" in (value.get("periodic_commands") or []):
                        bms_038b += 1
                elif key.startswith("PCS:"):
                    pcs_total += 1
                    if value.get("online"):
                        pcs_online += 1

            parts = []
            if bms_total:
                suffix = f", 038B={bms_038b}" if bms_038b else ""
                parts.append(f"BMS HB {bms_online}/{bms_total}{suffix}")
            if pcs_total:
                parts.append(f"PCS HB {pcs_online}/{pcs_total}")
            if not parts:
                return
            text = " | ".join(parts)
            # Avoid repainting the status bar/labels when nothing changed.
            if text == getattr(self, "_last_fleet_status_text", ""):
                return
            self._last_fleet_status_text = text
            self.last_heartbeat_status = text
            if hasattr(self, "heartbeat_state_label"):
                self.heartbeat_state_label.setText(parts[0])
            self.refresh_global_status_bar()
        finally:
            self._fleet_status_refresh_busy = False
