from __future__ import annotations

import json
import csv
from collections import deque
from pathlib import Path
from typing import Any, Dict

from PySide6.QtCharts import QChart, QLineSeries
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QFileDialog, QMessageBox, QTableWidgetItem, QInputDialog
from PySide6.QtGui import QColor





class DataReceiveMixin:

    def _enqueue_bms_snapshot_from_worker(self, device_name: str, snapshot: Dict[str, Any]) -> None:
        """Thread-safe BMS snapshot handoff used by DeviceWorker.

        In large-site mode we coalesce snapshots by device and let the Qt main
        thread process the newest value on a timer. This avoids one queued Qt
        signal per device per poll cycle.
        """
        lock = getattr(self, "_pending_bms_snapshot_lock", None)
        pending = getattr(self, "_pending_bms_snapshots", None)
        if lock is None or pending is None:
            # Fallback for tests/older wiring.
            try:
                self.bridge.data_received.emit(device_name, snapshot)
            except Exception:
                pass
            return
        try:
            with lock:
                pending[str(device_name)] = dict(snapshot)
        except Exception:
            try:
                self.bridge.data_received.emit(device_name, snapshot)
            except Exception:
                pass

    def _flush_pending_bms_snapshots(self) -> None:
        lock = getattr(self, "_pending_bms_snapshot_lock", None)
        pending = getattr(self, "_pending_bms_snapshots", None)
        if lock is None or pending is None:
            return
        try:
            with lock:
                if not pending:
                    return
                max_items = int(getattr(self, "_pending_bms_snapshot_max_per_flush", 80))
                items = list(pending.items())[:max_items]
                for key, _ in items:
                    pending.pop(key, None)
        except Exception:
            return

        for device_name, snapshot in items:
            try:
                self.on_data_received(device_name, snapshot)
            except Exception as exc:
                try:
                    self.log(f"[ERROR] BMS snapshot flush failed for {device_name}: {exc}")
                except Exception:
                    pass

    def _current_main_page_text(self) -> str:
        try:
            item = self.nav_list.currentItem()
            return item.text() if item is not None else ""
        except Exception:
            return ""

    def _is_main_page_visible(self, *names: str) -> bool:
        current = self._current_main_page_text().lower()
        return any(str(name).lower() in current for name in names)


    def _on_main_page_changed(self, index: int) -> None:
        """Refresh only the newly visible heavy page once.

        Hidden pages deliberately do not repaint in large-site/performance mode.
        When the operator navigates to a page, catch it up from latest snapshots.
        """
        _ = index
        try:
            page = self._current_main_page_text().lower()
            device = None
            if "detail" in page:
                device = getattr(self, "current_detail_device", None)
                if device:
                    self.refresh_details(device)
            elif "alarm" in page and "analysis" not in page:
                device = getattr(self, "current_alarm_device", None)
                if device:
                    self.refresh_alarms(device)
            elif "curve" in page:
                device = getattr(self, "current_curve_device", None)
                if device:
                    self.refresh_curves(device)
            elif "driver" in page:
                if hasattr(self, "driver_points_device_combo"):
                    device = self.driver_points_device_combo.currentText()
                    if device:
                        self.refresh_driver_points(device)
            elif "overview" in page:
                self.refresh_overview()
            elif "pcs devices" in page:
                if hasattr(self, "refresh_pcs_view"):
                    self.refresh_pcs_view(full=False)
        except Exception as exc:
            try:
                self.log(f"[WARN] Visible page refresh skipped: {exc}")
            except Exception:
                pass

    def _table_set_text_if_changed(self, table, row: int, col: int, text: object, color_value: object | None = None) -> None:
        item = table.item(row, col)
        value = str(text)
        if item is None:
            item = QTableWidgetItem(value)
            table.setItem(row, col, item)
        elif item.text() != value:
            item.setText(value)
        if color_value is not None and hasattr(self, "_set_table_item_color"):
            self._set_table_item_color(item, str(color_value))

    def on_error_received(self, device_name: str, error: str) -> None:
        import time
        now = time.time()
        last_ui = getattr(self, "_last_error_ui_time", {}).get(device_name, 0.0) if hasattr(self, "_last_error_ui_time") else 0.0
        if not hasattr(self, "_last_error_ui_time"):
            self._last_error_ui_time = {}
        update_ui = (now - last_ui) >= float(getattr(self, "ui_refresh_interval", 1.0))
        if update_ui:
            self._last_error_ui_time[device_name] = now

        row = self.device_rows.get(device_name)
        if row is not None and update_ui:
            run_item = QTableWidgetItem("Error")
            self._set_table_item_color(run_item, "Error")
            self.device_table.setItem(row, 11, run_item)

            online_item = QTableWidgetItem("Offline")
            self._set_table_item_color(online_item, "Offline")
            self.device_table.setItem(row, 12, online_item)

        self.last_error_message = f"{device_name}: {error}"
        if update_ui:
            self.refresh_global_status_bar()

        idx = self.sample_index[device_name]
        self.sample_index[device_name] += 1
        self.series_buffers[device_name]["online"].append((idx, 0.0))

        if update_ui and self.current_curve_device == device_name and self._is_main_page_visible("Curves"):
            self.refresh_curves(device_name)

        if update_ui:
            self.log(f"[ERROR] {device_name}: {error}")

    def on_heartbeat_written(self, device_name: str, value: int) -> None:
        # Heartbeat can arrive at 1 Hz per device. Do not repaint global status on
        # every tick; only update the selected device labels and throttle global UI.
        import time
        now = time.time()
        if self.current_control_device == device_name:
            self.heartbeat_state_label.setText(f"Running ({value})")
            self.control_state_label.setText("Running")
            self.last_control_result_label.setText(f"Heartbeat={value}")

        self.last_heartbeat_status = f"Running ({value})"
        last = float(getattr(self, "_last_hb_status_bar_refresh", 0.0))
        if now - last >= 3.0:
            self._last_hb_status_bar_refresh = now
            self.refresh_global_status_bar()

    def on_heartbeat_error(self, device_name: str, error: str) -> None:
        import time
        if self.current_control_device == device_name:
            self.heartbeat_state_label.setText("Error")
            self.control_state_label.setText("Failed")
            self.last_control_result_label.setText(error)

        self.last_heartbeat_status = "Error"
        self.last_error_message = f"{device_name}: {error}"
        now = time.time()
        last = float(getattr(self, "_last_hb_error_status_bar_refresh", 0.0))
        if now - last >= 3.0:
            self._last_hb_error_status_bar_refresh = now
            self.refresh_global_status_bar()

        self.control_log(f"[CONTROL] {device_name}: {error}")

    def on_hv_workflow_log(self, device_name: str, message: str) -> None:
        if "SUCCESS" in message:
            self.last_hv_status = "Success"
        elif "FAIL" in message:
            self.last_hv_status = "Failed"
        elif "TIMEOUT" in message:
            self.last_hv_status = "Timeout"
        elif "CANCELLED" in message:
            self.last_hv_status = "Cancelled"
        else:
            self.last_hv_status = "Running"

        self.refresh_global_status_bar()
        self.control_log(f"[{device_name}] {message}")

    def on_hv_workflow_finished(self, device_name: str, mode: str, success: bool) -> None:
        self.hv_workers.pop(device_name, None)

        mode_text = "HV ON" if mode == "on" else "HV OFF"

        if success:
            self.control_state_label.setText("Done")
            self.last_control_result_label.setText(f"{mode_text} success")
            self.last_hv_status = "Success"
            self.control_log(f"[CONTROL] {device_name}: {mode_text} workflow finished successfully")
        else:
            self.control_state_label.setText("Failed")
            self.last_control_result_label.setText(f"{mode_text} failed")
            self.last_hv_status = "Failed"
            self.control_log(f"[CONTROL] {device_name}: {mode_text} workflow finished with failure")

        self.refresh_global_status_bar()


    def on_data_received(self, device_name: str, snapshot: Dict[str, Any]) -> None:
        import time
        if getattr(self, "_runtime_snapshot_client_mode", lambda: False)():
            # In Runtime Dominant mode the UI must not process local worker data,
            # write CSV, or run local service callbacks. Runtime snapshots are
            # ingested by _apply_runtime_snapshot instead.
            return
        # Mark receive time for cluster strategy BMS response-time supervision.
        # The original protocol timestamp may be generated by the device and is not
        # reliable enough for local timeout decisions.
        snapshot["_received_ts"] = time.time()
        # CSV recording is manual. Polling/connecting a BMS does not write CSV
        # until the operator clicks Start BMS CSV.
        recorder = self.recorders.get(device_name)
        if recorder and device_name in getattr(self, "bms_csv_recording_devices", set()):
            recorder.write_row(snapshot)

        alarm_recorder = self.alarm_recorders.get(device_name)
        need_alarm_parse = (
            bool(alarm_recorder and device_name in getattr(self, "bms_csv_recording_devices", set()))
            or (self.current_alarm_device == device_name and self._is_main_page_visible("Alarms"))
        )
        parsed_alarm = None
        if need_alarm_parse:
            parser = self.get_alarm_parser_for_device(device_name) if hasattr(self, "get_alarm_parser_for_device") else self.alarm_parser
            parsed_alarm = parser.parse_snapshot(snapshot)

        if alarm_recorder and device_name in getattr(self, "bms_csv_recording_devices", set()):
            alarm_recorder.write_row(device_name, snapshot, parsed_alarm or {})

        self.latest_snapshots[device_name] = snapshot
        online_state = self.evaluate_bms_online_state(device_name, snapshot)
        self.service.on_snapshot(device_name, snapshot, self)

        # In Windows performance mode, table repainting is throttled separately from
        # data collection. Strategy still receives every snapshot via latest_snapshots.
        now_for_table = time.time()
        last_table = getattr(self, "_last_device_table_refresh_time", {}).get(device_name, 0.0) if hasattr(self, "_last_device_table_refresh_time") else 0.0
        table_interval = float(getattr(self, "ui_refresh_interval", 3.0)) if getattr(self, "performance_mode_enabled", True) else 0.5
        devices_page_visible = self._is_main_page_visible("Devices")
        hidden_table_interval = max(15.0, table_interval * 5.0)
        update_table = (now_for_table - last_table) >= (table_interval if devices_page_visible else hidden_table_interval)
        if not hasattr(self, "_last_device_table_refresh_time"):
            self._last_device_table_refresh_time = {}
        if update_table:
            self._last_device_table_refresh_time[device_name] = now_for_table

        row = self.device_rows.get(device_name)
        if row is not None and update_table:
            self.device_table.setUpdatesEnabled(False)
            try:
                def _set_text(col: int, value: object) -> None:
                    item = self.device_table.item(row, col)
                    text = str(value)
                    if item is None:
                        self.device_table.setItem(row, col, QTableWidgetItem(text))
                    elif item.text() != text:
                        item.setText(text)

                _set_text(5, snapshot.get("bms_status", "-"))
                _set_text(6, snapshot.get("number_of_racks", "-"))
                _set_text(7, snapshot.get("soc", "-"))
                _set_text(8, snapshot.get("system_voltage", "-"))
                _set_text(9, snapshot.get("system_current", "-"))
                _set_text(10, snapshot.get("system_power", "-"))

                item = self.device_table.item(row, 11)
                if item is None or item.text() != "Running":
                    run_item = QTableWidgetItem("Running")
                    self._set_table_item_color(run_item, "Running")
                    self.device_table.setItem(row, 11, run_item)

                item = self.device_table.item(row, 12)
                if item is None or item.text() != online_state:
                    online_item = QTableWidgetItem(online_state)
                    self._set_table_item_color(online_item, online_state)
                    self.device_table.setItem(row, 12, online_item)
            finally:
                self.device_table.setUpdatesEnabled(True)

        self.recent_buffers[device_name].append(snapshot)

        idx = self.sample_index[device_name]
        self.sample_index[device_name] += 1

        # v3.0 phase 3: store numeric driver points for dynamic plotting.
        # Windows performance guard: on 24 BMS with large point tables, appending
        # every point for every hidden device can become more expensive than the
        # Modbus polling itself. Keep full resolution for the currently visible
        # device/page, and downsample hidden devices in performance mode.
        points = snapshot.get("points", {}) if isinstance(snapshot.get("points"), dict) else snapshot
        perf_mode = bool(getattr(self, "performance_mode_enabled", True))
        visible_for_dynamic = (
            self.current_curve_device == device_name and self._is_main_page_visible("Curves")
        ) or (
            hasattr(self, "driver_points_device_combo")
            and self.driver_points_device_combo.currentText() == device_name
            and self._is_main_page_visible("Driver Points")
        )
        dynamic_sample_stride = 1 if (not perf_mode or visible_for_dynamic) else int(getattr(self, "hidden_dynamic_point_stride", 5))
        update_dynamic_points = (dynamic_sample_stride <= 1) or (idx % max(1, dynamic_sample_stride) == 0)
        if update_dynamic_points:
            for point_key, point_value in points.items():
                if point_key in {"timestamp", "raw", "point_meta", "points"}:
                    continue
                try:
                    numeric_value = float(point_value)
                except (TypeError, ValueError):
                    continue
                self.dynamic_point_buffers[device_name][str(point_key)].append((idx, numeric_value))

        # Heavy UI refresh is throttled; strategy/latest_snapshots still update at full sampling rate.

        self.series_buffers[device_name]["soc"].append((idx, float(snapshot.get("soc", 0))))
        self.series_buffers[device_name]["system_voltage"].append(
            (idx, float(snapshot.get("system_voltage", 0)))
        )
        self.series_buffers[device_name]["system_current"].append(
            (idx, float(snapshot.get("system_current", 0)))
        )

        online_value = 1.0 if online_state == "Online" else 0.0
        self.series_buffers[device_name]["online"].append((idx, online_value))

        if self.current_curve_device is None:
            self.current_curve_device = device_name
            self.curve_device_combo.setCurrentText(device_name)
            self.curve_device_label.setText(f"Current device: {device_name}")

        if self.current_detail_device is None:
            self.current_detail_device = device_name
            self.detail_device_combo.setCurrentText(device_name)
            self.detail_device_label.setText(f"Current device: {device_name}")

        if self.current_alarm_device is None:
            self.current_alarm_device = device_name
            self.alarm_device_combo.setCurrentText(device_name)
            self.alarm_device_label.setText(f"Current device: {device_name}")

        if self.current_control_device is None:
            self.current_control_device = device_name
            self.control_device_combo.setCurrentText(device_name)
            self.control_device_label.setText(f"Current device: {device_name}")

        now = time.time()
        last_ui = self._last_ui_refresh_time.get(device_name, 0.0) if hasattr(self, "_last_ui_refresh_time") else 0.0
        interval = float(getattr(self, "ui_refresh_interval", 1.0))
        if now - last_ui < interval:
            return
        if hasattr(self, "_last_ui_refresh_time"):
            self._last_ui_refresh_time[device_name] = now

        if self.current_curve_device == device_name and self._is_main_page_visible("Curves"):
            last_curve = getattr(self, "_last_curve_refresh_time", {}).get(device_name, 0.0) if hasattr(self, "_last_curve_refresh_time") else 0.0
            curve_interval = float(getattr(self, "curve_refresh_interval", 5.0)) if getattr(self, "performance_mode_enabled", True) else interval
            if now - last_curve >= curve_interval:
                if hasattr(self, "_last_curve_refresh_time"):
                    self._last_curve_refresh_time[device_name] = now
                self.refresh_curves(device_name)

        if self.current_detail_device == device_name and self._is_main_page_visible("Details"):
            self.refresh_details(device_name)

        if (
            hasattr(self, "driver_points_device_combo")
            and self.driver_points_device_combo.currentText() == device_name
            and self._is_main_page_visible("Driver Points")
        ):
            self.refresh_driver_points(device_name)

        if self.current_alarm_device == device_name and self._is_main_page_visible("Alarms"):
            self.refresh_alarms(device_name)

    def on_curve_device_changed(self, device_name: str) -> None:
        if device_name:
            self.refresh_curves(device_name)

    def on_detail_device_changed(self, device_name: str) -> None:
        if device_name:
            self.refresh_details(device_name)

    def on_alarm_device_changed(self, device_name: str) -> None:
        if device_name:
            self.refresh_alarms(device_name)

    def on_control_device_changed(self, device_name: str) -> None:
        if not device_name:
            return

        self.current_control_device = device_name
        self.control_device_label.setText(f"Current device: {device_name}")

        if device_name in self.heartbeat_workers:
            self.heartbeat_state_label.setText("Running")
            self.control_state_label.setText("Running")
        else:
            self.heartbeat_state_label.setText("Stopped")
            self.control_state_label.setText("Idle")

        for label in self.pcs_status_labels.values():
            label.setText("-")

        self.refresh_global_status_bar()

    def on_device_table_clicked(self, row: int, column: int) -> None:
        _ = column
        item = self.device_table.item(row, 0)
        if item is None:
            return

        device_name = item.text()
        if not device_name:
            return

        self.curve_device_combo.setCurrentText(device_name)
        if hasattr(self, "driver_points_device_combo"):
            self.driver_points_device_combo.setCurrentText(device_name)
        self.detail_device_combo.setCurrentText(device_name)
        self.alarm_device_combo.setCurrentText(device_name)
        self.control_device_combo.setCurrentText(device_name)

        self.refresh_curves(device_name)
        if hasattr(self, "driver_points_table"):
            self.refresh_driver_points(device_name)
        self.refresh_details(device_name)
        self.refresh_alarms(device_name)

    def evaluate_bms_online_state(self, device_name: str, snapshot: Dict[str, Any]) -> str:
        heartbeat = snapshot.get("bms_heartbeat")

        if heartbeat is None:
            return "Unknown"

        try:
            heartbeat_int = int(heartbeat)
        except (TypeError, ValueError):
            return "Unknown"

        last = self.bms_last_heartbeat.get(device_name)

        if last is None:
            self.bms_last_heartbeat[device_name] = heartbeat_int
            self.bms_heartbeat_same_count[device_name] = 0
            return "Online"

        delta = (heartbeat_int - last) % 256
        if 0 < delta <= 5:
            self.bms_last_heartbeat[device_name] = heartbeat_int
            self.bms_heartbeat_same_count[device_name] = 0
            return "Online"

        if heartbeat_int == last:
            same_count = self.bms_heartbeat_same_count.get(device_name, 0) + 1
            self.bms_heartbeat_same_count[device_name] = same_count

            if same_count >= 3:
                return "Stale"
            return "Online"

        self.bms_last_heartbeat[device_name] = heartbeat_int
        self.bms_heartbeat_same_count[device_name] = 0
        return "Stale"

