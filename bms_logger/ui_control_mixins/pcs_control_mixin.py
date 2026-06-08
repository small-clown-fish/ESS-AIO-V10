from __future__ import annotations

from typing import Optional

from PySide6.QtWidgets import QMessageBox, QTableWidgetItem

from ..pcs_client import PcsClient


class PcsControlMixin:
    def _execute_pcs_command(
        self,
        action_name: str,
        pcs_method_name: str,
        confirm: bool = True,
    ) -> None:
        device_name = self._get_selected_control_device()
        if not device_name:
            return

        if confirm:
            reply = QMessageBox.question(
                self,
                f"Confirm {action_name}",
                f"Execute PCS action '{action_name}' for current selected device context ({device_name})?",
            )
            if reply != QMessageBox.Yes:
                return

        if getattr(self, "_runtime_should_handle_command", lambda: False)():
            pcs_name = self._get_selected_pcs_name() if hasattr(self, "_get_selected_pcs_name") else ""
            if not pcs_name:
                QMessageBox.warning(self, "Runtime API", "Select one PCS first.")
                return
            result = self._runtime_post("/api/pcs/command", {"pcs": pcs_name, "method": pcs_method_name}, timeout_s=5.0) if hasattr(self, "_runtime_post") else {"ok": False, "error": "runtime helper missing"}
            if result.get("ok"):
                self.control_state_label.setText("Queued")
                self.last_control_result_label.setText(f"Runtime PCS {action_name} queued")
                self.control_log(f"[RUNTIME_UI] PCS {action_name} sent to runtime: {pcs_name}")
                self.poll_runtime_snapshot() if hasattr(self, "poll_runtime_snapshot") else None
            else:
                QMessageBox.warning(self, "Runtime API", f"PCS {action_name} failed: {result.get('error', result)}")
            return

        self.control_state_label.setText("Executing")
        self.last_control_result_label.setText(f"PCS {action_name}")
        self.control_log(f"[CONTROL] {device_name}: PCS {action_name} started")

        if hasattr(self, "app_facade"):
            result = self.app_facade.execute_pcs_command_for_device(
                device_name=device_name,
                method_name=pcs_method_name,
                action_name=action_name,
                precheck=False,
            )
            if result.ok:
                self.control_state_label.setText("Done")
                self.last_control_result_label.setText(f"PCS {action_name} success")
                self.control_log(f"[CONTROL] {device_name}: {result.message}")
                QMessageBox.information(self, "Success", f"PCS {action_name} success")
            else:
                self.control_state_label.setText("Failed")
                self.last_control_result_label.setText(result.message)
                self.control_log(f"[CONTROL] {device_name}: PCS {action_name} failed - {result.message}")
                QMessageBox.warning(self, "Failed", result.message)
            return

        # Fallback path for tests/older app wiring: still use the same FleetDeviceWorker queue.
        pcs_name = self._get_selected_pcs_name()
        if not pcs_name:
            self.control_state_label.setText("Failed")
            self.last_control_result_label.setText("No PCS selected")
            QMessageBox.warning(self, "Failed", "No PCS selected")
            return
        def pcs_factory(name: str):
            return self.create_pcs_client_for_pcs_name(name)
        self.fleet_manager.start_pcs_command_workers([pcs_name], pcs_factory, interval_s=float(getattr(self, "heartbeat_interval", 1.0)))
        count = self.fleet_manager.enqueue_pcs_command([pcs_name], pcs_method_name, label=action_name)
        if count == 1:
            self.control_state_label.setText("Queued")
            self.last_control_result_label.setText(f"PCS {action_name} queued")
            self.control_log(f"[CONTROL] PCS {action_name} queued on FleetDeviceWorker: {pcs_name}")
        else:
            self.control_state_label.setText("Failed")
            self.last_control_result_label.setText("PCS command not queued")
            QMessageBox.warning(self, "Failed", f"PCS {action_name} not queued for {pcs_name}")
        return

    def handle_pcs_start(self) -> None:
        self._execute_pcs_command("Start", "start", confirm=True)

    def handle_pcs_stop(self) -> None:
        self._execute_pcs_command("Stop", "stop", confirm=True)

    def handle_pcs_reset_fault(self) -> None:
        self._execute_pcs_command("Reset Fault", "reset_fault", confirm=True)

    def handle_pcs_hv_on(self) -> None:
        self._execute_pcs_command("HV On", "hv_on", confirm=True)

    def handle_pcs_hv_off(self) -> None:
        self._execute_pcs_command("HV Off", "hv_off", confirm=True)

    def handle_pcs_close_dc_breaker(self) -> None:
        self._execute_pcs_command("Close DC Breaker", "close_dc_breaker", confirm=True)

    def handle_pcs_open_dc_breaker(self) -> None:
        self._execute_pcs_command("Open DC Breaker", "open_dc_breaker", confirm=True)

    def _format_pcs_breaker_state(self, pcs_client: PcsClient) -> str:
        try:
            if pcs_client.is_dc_breaker_open():
                return "Open"
            if pcs_client.is_dc_breaker_closed():
                return "Closed"
            return "Unknown"
        except Exception as exc:
            return f"Error: {exc}"

    def handle_refresh_pcs_status(self) -> None:
        device_name = self._get_selected_control_device()
        if not device_name:
            return

        if hasattr(self, "app_facade"):
            result = self.app_facade.read_pcs_status_for_device(device_name)
            if not result.ok:
                self.control_log(f"[CONTROL] {device_name}: {result.message}")
                QMessageBox.critical(self, "Error", result.message)
                return

            for key, value in dict(result.value or {}).items():
                if key in self.pcs_status_labels:
                    self.pcs_status_labels[key].setText(str(value))
            self.control_log(f"[CONTROL] {device_name}: PCS status refreshed")
            return

        pcs_client = self.create_pcs_client()
        try:
            if not pcs_client.connect():
                self.control_log(f"[CONTROL] {device_name}: PCS status refresh failed - connect failed")
                QMessageBox.critical(self, "Error", "PCS connect failed")
                return

            values = {}
            for key, func in [
                ("online", lambda: "Online" if pcs_client.is_online() else "Offline"),
                ("run_status", lambda: str(pcs_client.get_run_status())),
                ("fault_status", lambda: str(pcs_client.get_fault_status())),
                ("alarm_status", lambda: str(pcs_client.get_alarm_status())),
                ("dc_breaker", lambda: self._format_pcs_breaker_state(pcs_client)),
                ("active_power", lambda: str(pcs_client.get_active_power())),
                ("mode", lambda: str(pcs_client.get_mode())),
                ("remote_local", lambda: str(pcs_client.get_remote_local_status())),
            ]:
                try:
                    values[key] = func()
                except Exception as exc:
                    values[key] = f"Error: {exc}"

            for key, value in values.items():
                self.pcs_status_labels[key].setText(value)

            self.control_log(f"[CONTROL] {device_name}: PCS status refreshed")

        except Exception as exc:
            self.control_log(f"[CONTROL] {device_name}: PCS status refresh exception - {exc}")
            QMessageBox.critical(self, "Error", f"PCS status refresh exception:\n{exc}")

        finally:
            try:
                pcs_client.close()
            except Exception:
                pass


    def _pcs_live_point_names(self, pcs_client: PcsClient) -> list[str]:
        preferred = [
            "heartbeat",
            "set_active_power",
            "run_status",
            "remote_local_status",
            "ac_breaker_status",
            "dc_breaker_status",
            "active_power",
            "discharge_active_power",
            "reactive_power",
            "inductive_reactive_power",
            "dc_voltage",
            "dc_current",
            "ac_voltage",
            "ac_current",
            "frequency",
            "power_factor",
            "fault_status",
            "alarm_status",
            "mode",
            "online_status",
        ]
        points = getattr(pcs_client, "points", {}) or {}
        names = [name for name in preferred if name in points]
        # Add remaining commonly useful non-command measurements without flooding the UI.
        for name in sorted(points.keys()):
            if name in names:
                continue
            cfg = points.get(name, {}) or {}
            access = str(cfg.get("access", "")).upper()
            if name.endswith("_cmd") or access in {"WO", "RW"}:
                continue
            if len(names) >= 40:
                break
            names.append(name)
        return names

    def _format_pcs_point_meaning(self, cfg: dict, raw: object) -> str:
        enum = cfg.get("enum") or cfg.get("values") or {}
        key = str(int(raw)) if isinstance(raw, (int, float)) and float(raw).is_integer() else str(raw)
        if isinstance(enum, dict) and key in enum:
            return str(enum[key])
        remark = cfg.get("remark") or cfg.get("description") or ""
        return str(remark)

    def handle_refresh_pcs_live_status(self) -> None:
        if hasattr(self, "_is_main_page_visible") and not self._is_main_page_visible("Control"):
            return
        device_name = self._get_selected_control_device()
        if not device_name:
            return
        if getattr(self, "performance_mode_enabled", True) and getattr(self, "_pcs_live_refresh_busy", False):
            return
        self._pcs_live_refresh_busy = True
        pcs_name = self._get_selected_pcs_name()
        pcs_client = self.create_pcs_client_for_pcs_name(pcs_name)
        rows = []
        try:
            if not pcs_client.connect():
                raise RuntimeError("PCS connect failed")
            for point_name in self._pcs_live_point_names(pcs_client):
                cfg = (pcs_client.points or {}).get(point_name, {}) or {}
                address = cfg.get("address", "")
                unit = cfg.get("unit", "")
                title = cfg.get("name_cn") or cfg.get("name_en") or cfg.get("description") or point_name
                try:
                    raw = pcs_client.read_raw(point_name)
                    try:
                        value = pcs_client.read_value(point_name)
                    except Exception:
                        value = raw
                    meaning = self._format_pcs_point_meaning(cfg, raw)
                    rows.append([point_name, address, title, raw, value, unit, meaning])
                except Exception as exc:
                    rows.append([point_name, address, title, "-", "-", unit, f"ERROR: {exc}"])
        except Exception as exc:
            self._pcs_live_refresh_busy = False
            self.control_log(f"[PCS] Live status refresh failed: {exc}")
            if hasattr(self, "pcs_live_table"):
                self.pcs_live_table.setRowCount(1)
                for col, value in enumerate(["ERROR", "-", "PCS live refresh", "-", "-", "", str(exc)]):
                    self.pcs_live_table.setItem(0, col, QTableWidgetItem(str(value)))
            return
        finally:
            try:
                pcs_client.close()
            except Exception:
                pass
            self._pcs_live_refresh_busy = False

        if not hasattr(self, "pcs_live_table"):
            return
        sig = (pcs_name, tuple(tuple(str(c) for c in r) for r in rows))
        if getattr(self, "_last_pcs_live_signature", None) == sig:
            return
        self._last_pcs_live_signature = sig
        self.pcs_live_table.setUpdatesEnabled(False)
        try:
            self.pcs_live_table.setRowCount(len(rows))
            for row_idx, row in enumerate(rows):
                for col, value in enumerate(row):
                    text = self._pcs_fmt_cell_value(value) if hasattr(self, "_pcs_fmt_cell_value") else str(value)
                    item = self.pcs_live_table.item(row_idx, col)
                    if item is None:
                        self.pcs_live_table.setItem(row_idx, col, QTableWidgetItem(text))
                    elif item.text() != text:
                        item.setText(text)
        finally:
            self.pcs_live_table.setUpdatesEnabled(True)
        self.control_log(f"[PCS] Live registers refreshed: pcs={pcs_name}, rows={len(rows)}")

    def handle_toggle_pcs_live_auto_refresh(self, checked: bool) -> None:
        if not hasattr(self, "pcs_live_timer"):
            return
        if checked:
            interval_ms = int(float(self.pcs_live_interval_spin.value()) * 1000) if hasattr(self, "pcs_live_interval_spin") else 2000
            self.pcs_live_timer.start(max(500, interval_ms))
            self.handle_refresh_pcs_live_status()
            self.control_log("[PCS] Live register auto refresh started")
        else:
            self.pcs_live_timer.stop()
            self.control_log("[PCS] Live register auto refresh stopped")

    def handle_start_pcs_heartbeat(self) -> None:
        # PCS heartbeat is kept behind the FleetDeviceWorker queue. It no longer
        # opens a direct connect/write/close loop from the UI timer.
        pcs_name = self._get_selected_pcs_name()
        if not pcs_name:
            QMessageBox.information(self, "PCS Heartbeat", "No PCS selected.")
            return

        def pcs_factory(name: str):
            return self.create_pcs_client_for_pcs_name(name)

        self.fleet_manager.start_pcs_heartbeats(
            [pcs_name],
            pcs_factory,
            interval_s=float(getattr(self, "heartbeat_interval", 1.0)),
            heartbeat_enabled=True,
        )
        if hasattr(self, "pcs_hb_state_label"):
            self.pcs_hb_state_label.setText(f"PCS HB queued: {pcs_name}")
        self.control_log(f"[PCS] Heartbeat worker started for {pcs_name} through FleetDeviceWorker queue")

    def handle_stop_pcs_heartbeat(self) -> None:
        pcs_name = self._get_selected_pcs_name()
        stopped = 0
        if pcs_name and hasattr(self.fleet_manager, "stop_named"):
            stopped = self.fleet_manager.stop_named("PCS", [pcs_name])
        elif hasattr(self, "pcs_heartbeat_timer"):
            self.pcs_heartbeat_timer.stop()
        if hasattr(self, "pcs_hb_state_label"):
            self.pcs_hb_state_label.setText("PCS HB: stopped")
        self.control_log(f"[PCS] Heartbeat stopped for {pcs_name or 'selected PCS'}; workers stopped={stopped}")

    def handle_pcs_heartbeat_tick(self) -> None:
        # Kept for old QTimer signal compatibility. Direct PCS heartbeat writes
        # are intentionally disabled; heartbeat is handled by FleetDeviceWorker.
        return


    def _set_selected_pcs_power_value(self, *, method_name: str, value: float, unit: str, label: str) -> None:
        device_name = self._get_selected_control_device()
        if not device_name:
            return
        pcs_name = self._get_selected_pcs_name()
        self.control_state_label.setText("Executing")
        self.last_control_result_label.setText(label)
        self.control_log(f"[CONTROL] {device_name}: {label} started for PCS={pcs_name}, value={value}{unit}")

        try:
            if hasattr(self, "app_facade"):
                if method_name == "set_active_power":
                    result = self.app_facade.set_pcs_power(pcs_name, value, precheck=False)
                elif method_name == "set_reactive_power" and hasattr(self.app_facade, "set_pcs_reactive_power"):
                    result = self.app_facade.set_pcs_reactive_power(pcs_name, value, precheck=False)
                else:
                    raise RuntimeError(f"Unsupported power method: {method_name}")
                if result.ok:
                    self.control_state_label.setText("Done")
                    self.last_control_result_label.setText(f"{label} success")
                    self.control_log(f"[CONTROL] {device_name}: {result.message}")
                    QMessageBox.information(self, "Success", f"{label} success")
                else:
                    self.control_state_label.setText("Failed")
                    self.last_control_result_label.setText(result.message)
                    self.control_log(f"[CONTROL] {device_name}: {label} failed - {result.message}")
                    QMessageBox.warning(self, "Failed", result.message)
                return

            def pcs_factory(name: str):
                return self.create_pcs_client_for_pcs_name(name)
            self.fleet_manager.start_pcs_command_workers([pcs_name], pcs_factory, interval_s=float(getattr(self, "heartbeat_interval", 1.0)))
            count = self.fleet_manager.enqueue_pcs_command([pcs_name], method_name, float(value), label=f"{label}={value}{unit}")
            if count != 1:
                raise RuntimeError(f"PCS command not queued for {pcs_name}")
            self.control_state_label.setText("Queued")
            self.last_control_result_label.setText(f"{label} queued")
            self.control_log(f"[CONTROL] {label} queued on FleetDeviceWorker, PCS={pcs_name}, value={value}{unit}")
            return
        except Exception as exc:
            self.control_state_label.setText("Failed")
            self.last_control_result_label.setText(str(exc))
            self.control_log(f"[CONTROL] {device_name}: {label} exception - {exc}")
            QMessageBox.critical(self, "Error", f"{label} exception:\n{exc}")

    def handle_set_pcs_active_power(self) -> None:
        value = float(self.pcs_active_power_spin.value()) if hasattr(self, "pcs_active_power_spin") else 0.0
        self._set_selected_pcs_power_value(method_name="set_active_power", value=value, unit="kW", label="Set Active Power")

    def handle_set_pcs_reactive_power(self) -> None:
        value = float(self.pcs_reactive_power_spin.value()) if hasattr(self, "pcs_reactive_power_spin") else 0.0
        self._set_selected_pcs_power_value(method_name="set_reactive_power", value=value, unit="kvar", label="Set Reactive Power")


    def _sync_pcs_control_tab_visibility(self) -> None:
        """Show/hide PCS Control tab immediately after Runtime Settings apply."""
        tabs = getattr(self, "control_inner_tabs", None)
        page = getattr(self, "pcs_control_page", None)
        if tabs is None or page is None:
            return
        enabled = bool(getattr(self, "pcs_control_ui_enabled", True))
        try:
            current_idx = tabs.indexOf(page)
        except Exception:
            current_idx = -1
        if enabled and current_idx < 0:
            self.pcs_control_tab_index = tabs.addTab(page, "PCS Control")
        elif (not enabled) and current_idx >= 0:
            tabs.removeTab(current_idx)
            self.pcs_control_tab_index = -1

    def _restart_runtime_timers_after_apply(self) -> None:
        """Apply timer intervals without restarting communication workers."""
        timer_specs = [
            ("fleet_status_timer", max(1000, float(getattr(self, "status_refresh_interval", 5.0)) * 1000)),
            ("_log_flush_timer", max(300, int(getattr(self, "log_flush_interval_ms", 1000)))),
            ("status_refresh_timer", max(1000, float(getattr(self, "status_refresh_interval", 5.0)) * 1000)),
            ("ui_refresh_timer", max(500, float(getattr(self, "ui_refresh_interval", 3.0)) * 1000)),
            ("curve_refresh_timer", max(1000, float(getattr(self, "curve_refresh_interval", 5.0)) * 1000)),
        ]
        for attr, interval in timer_specs:
            timer = getattr(self, attr, None)
            if timer is None:
                continue
            try:
                timer.setInterval(int(interval))
            except Exception:
                pass

    def _qt_widget_alive(self, widget) -> bool:
        """Return False when a PySide wrapper points to a deleted C++ QObject."""
        if widget is None:
            return False
        try:
            from shiboken6 import isValid  # type: ignore
            return bool(isValid(widget))
        except Exception:
            # In tests or non-Qt environments, fall back to a best-effort check.
            return True

    def _safe_spin_value(self, attr: str, default):
        widget = getattr(self, attr, None)
        if not self._qt_widget_alive(widget):
            return default
        try:
            return widget.value()
        except RuntimeError:
            # Qt object was deleted between the validity check and the call.
            try:
                setattr(self, attr, None)
            except Exception:
                pass
            return default
        except Exception:
            return default

    def _safe_combo_text(self, attr: str, default: str = "") -> str:
        widget = getattr(self, attr, None)
        if not self._qt_widget_alive(widget):
            return default
        try:
            return str(widget.currentText())
        except RuntimeError:
            try:
                setattr(self, attr, None)
            except Exception:
                pass
            return default
        except Exception:
            return default

    def _safe_line_text(self, attr: str, default: str = "") -> str:
        widget = getattr(self, attr, None)
        if not self._qt_widget_alive(widget):
            return default
        try:
            return str(widget.text())
        except RuntimeError:
            try:
                setattr(self, attr, None)
            except Exception:
                pass
            return default
        except Exception:
            return default

    def _safe_label_set_text(self, attr: str, text: str) -> None:
        widget = getattr(self, attr, None)
        if not self._qt_widget_alive(widget):
            return
        try:
            widget.setText(text)
        except RuntimeError:
            try:
                setattr(self, attr, None)
            except Exception:
                pass
        except Exception:
            pass

    def apply_runtime_params(self) -> None:
        """Apply only live runtime widgets; tolerate hidden/rebuilt Settings pages.

        Some settings widgets can be deleted by Qt when pages/tabs are rebuilt.
        Accessing a stale PySide wrapper raises:
        ``RuntimeError: Internal C++ object already deleted``.
        This method therefore reads widgets through safe helpers and keeps the
        previous runtime value when a widget is no longer alive.
        """
        self.fake_mode = self._safe_combo_text(
            "fake_mode_combo", "Fake" if getattr(self, "fake_mode", False) else "Real"
        ) == "Fake"
        self.pcs_control_ui_enabled = self._safe_combo_text(
            "pcs_control_ui_combo", "Enabled" if getattr(self, "pcs_control_ui_enabled", True) else "Disabled"
        ) == "Enabled"
        self.runtime_api_enabled = self._safe_combo_text(
            "runtime_api_mode_combo", "Runtime API Client" if getattr(self, "runtime_api_enabled", False) else "Local / Direct"
        ) == "Runtime API Client"
        self.runtime_dominant_mode = self._safe_combo_text(
            "runtime_dominant_combo", "Enabled" if getattr(self, "runtime_dominant_mode", True) else "Disabled"
        ) == "Enabled"
        self.runtime_api_url = self._safe_line_text("runtime_api_url_edit", getattr(self, "runtime_api_url", "http://127.0.0.1:8765")).strip() or "http://127.0.0.1:8765"
        self.runtime_snapshot_poll_interval_ms = int(
            self._safe_spin_value("runtime_snapshot_poll_spin", getattr(self, "runtime_snapshot_poll_interval_ms", 2000))
        )

        self.heartbeat_interval = float(self._safe_spin_value("heartbeat_interval_spin", getattr(self, "heartbeat_interval", 1.0)))
        self.hv_step_timeout = float(self._safe_spin_value("hv_timeout_spin", getattr(self, "hv_step_timeout", 30.0)))
        self.hv_poll_interval = float(self._safe_spin_value("hv_poll_interval_spin", getattr(self, "hv_poll_interval", 1.0)))
        self.pcs_zero_power_threshold = float(self._safe_spin_value("pcs_zero_power_spin", getattr(self, "pcs_zero_power_threshold", 0.1)))

        # Legacy cutoff widgets used to live in Runtime Settings.  Cluster
        # Strategy now owns cutoff thresholds, but keep these values safely for
        # backward compatibility and old workflows.
        self.charge_cutoff_max_cell_voltage = float(
            self._safe_spin_value("charge_cutoff_voltage_spin", getattr(self, "charge_cutoff_max_cell_voltage", 3550.0))
        )
        self.discharge_cutoff_min_cell_voltage = float(
            self._safe_spin_value("discharge_cutoff_voltage_spin", getattr(self, "discharge_cutoff_min_cell_voltage", 2800.0))
        )
        self.cutoff_mode = self._safe_combo_text("cutoff_mode_combo", getattr(self, "cutoff_mode", "Alarm Only"))
        self.cutoff_trigger_confirm_count = int(
            self._safe_spin_value("cutoff_trigger_confirm_spin", getattr(self, "cutoff_trigger_confirm_count", 3))
        )
        self.cutoff_recover_confirm_count = int(
            self._safe_spin_value("cutoff_recover_confirm_spin", getattr(self, "cutoff_recover_confirm_count", 3))
        )
        self.alarm_history_window_before_minutes = int(
            self._safe_spin_value("alarm_window_before_spin", getattr(self, "alarm_history_window_before_minutes", 5))
        )
        self.alarm_history_window_after_minutes = int(
            self._safe_spin_value("alarm_window_after_spin", getattr(self, "alarm_history_window_after_minutes", 5))
        )

        # Legacy standalone derating / power tracking / PCS fault protection are
        # hidden in the current UI because Cluster Strategy owns power/cutoff
        # behavior now.  Keep numeric values loaded for backward compatibility,
        # but force these legacy loops off so Apply Runtime Params cannot enable
        # unexpected background control.
        self.power_derating_enabled = False
        self.derating_margin_mv = float(self._safe_spin_value("derating_margin_spin", getattr(self, "derating_margin_mv", 0.0)))
        self.derating_power_kw = float(self._safe_spin_value("derating_power_spin", getattr(self, "derating_power_kw", 0.0)))
        self.power_tracking_enabled = False
        self.power_tracking_tolerance_kw = float(
            self._safe_spin_value("power_tracking_tolerance_spin", getattr(self, "power_tracking_tolerance_kw", 0.5))
        )
        self.power_tracking_confirm_count = int(
            self._safe_spin_value("power_tracking_confirm_spin", getattr(self, "power_tracking_confirm_count", 3))
        )
        self.power_tracking_auto_retry = False
        self.power_tracking_retry_interval = int(
            self._safe_spin_value("power_retry_interval_spin", getattr(self, "power_tracking_retry_interval", 10))
        )
        self.power_tracking_max_retry = int(
            self._safe_spin_value("power_retry_max_spin", getattr(self, "power_tracking_max_retry", 3))
        )
        self.pcs_fault_protection_mode = "Disabled"
        self.pcs_fault_protection_enabled = False
        self.pcs_fault_confirm_count = int(
            self._safe_spin_value("pcs_fault_confirm_spin", getattr(self, "pcs_fault_confirm_count", 3))
        )

        self.worker_start_stagger_seconds = float(
            self._safe_spin_value("worker_stagger_spin", getattr(self, "worker_start_stagger_seconds", 0.5))
        )
        self.performance_mode_enabled = self._safe_combo_text(
            "performance_mode_combo", "Enabled" if getattr(self, "performance_mode_enabled", True) else "Disabled"
        ) == "Enabled"
        self.large_site_mode_enabled = self._safe_combo_text(
            "large_site_mode_combo", "Enabled" if getattr(self, "large_site_mode_enabled", True) else "Disabled"
        ) == "Enabled"
        self.max_parallel_bms_io = int(
            self._safe_spin_value("max_parallel_bms_io_spin", getattr(self, "max_parallel_bms_io", 10))
        )
        try:
            from ..worker import DeviceWorker
        except Exception:
            try:
                from bms_logger.worker import DeviceWorker
            except Exception:
                DeviceWorker = None  # type: ignore
        if DeviceWorker is not None:
            DeviceWorker.configure_global_io_limit(self.max_parallel_bms_io if getattr(self, "large_site_mode_enabled", True) else 0)

        self.ui_refresh_interval = float(self._safe_spin_value("ui_refresh_interval_spin", getattr(self, "ui_refresh_interval", 3.0)))
        self.curve_refresh_interval = float(self._safe_spin_value("curve_refresh_interval_spin", getattr(self, "curve_refresh_interval", 5.0)))
        self.status_refresh_interval = float(self._safe_spin_value("status_refresh_interval_spin", getattr(self, "status_refresh_interval", 5.0)))
        self.log_flush_interval_ms = int(self._safe_spin_value("log_flush_interval_spin", getattr(self, "log_flush_interval_ms", 1000)))

        self._restart_runtime_timers_after_apply()
        if hasattr(self, "_apply_runtime_api_mode"):
            self._apply_runtime_api_mode()

        self.control_log(
            "[PARAM] Runtime parameters applied: "
            f"mode={'Fake' if self.fake_mode else 'Real'}, "
            f"pcs_control_ui={'Enabled' if self.pcs_control_ui_enabled else 'Disabled'}, "
            f"runtime_api={'Enabled' if getattr(self, 'runtime_api_enabled', False) else 'Disabled'}, "
            f"runtime_url={getattr(self, 'runtime_api_url', '')}, "
            f"heartbeat={self.heartbeat_interval}s, "
            f"hv_timeout={self.hv_step_timeout}s, "
            f"hv_poll={self.hv_poll_interval}s, "
            f"pcs_zero_power={self.pcs_zero_power_threshold}kW, "
            f"charge_cutoff={self.charge_cutoff_max_cell_voltage}mV, "
            f"discharge_cutoff={self.discharge_cutoff_min_cell_voltage}mV, "
            f"cutoff_mode={self.cutoff_mode}, "
            f"worker_stagger={self.worker_start_stagger_seconds}s, "
            f"performance_mode={'Enabled' if getattr(self, 'performance_mode_enabled', True) else 'Disabled'}, "
            f"large_site_mode={'Enabled' if getattr(self, 'large_site_mode_enabled', True) else 'Disabled'}, "
            f"max_parallel_bms_io={getattr(self, 'max_parallel_bms_io', 10)}, "
            f"ui_refresh={self.ui_refresh_interval}s, "
            f"curve_refresh={getattr(self, 'curve_refresh_interval', 5.0)}s, "
            f"status_refresh={getattr(self, 'status_refresh_interval', 5.0)}s"
        )
        self._sync_pcs_control_tab_visibility()
        self.save_runtime_config()

        summary = (
            f"Performance={'ON' if getattr(self, 'performance_mode_enabled', True) else 'OFF'}, "
            f"LargeSite={'ON' if getattr(self, 'large_site_mode_enabled', True) else 'OFF'}, "
            f"PCS Control={'ON' if getattr(self, 'pcs_control_ui_enabled', True) else 'OFF'}, "
            f"RuntimeAPI={'ON' if getattr(self, 'runtime_api_enabled', False) else 'OFF'}, "
            f"UI={getattr(self, 'ui_refresh_interval', 3.0)}s, "
            f"Status={getattr(self, 'status_refresh_interval', 5.0)}s, "
            f"Curve={getattr(self, 'curve_refresh_interval', 5.0)}s, "
            "Legacy derating/tracking/protection=OFF"
        )
        self._safe_label_set_text("runtime_apply_status_label", f"Applied: {summary}")
        if self._qt_widget_alive(getattr(self, "runtime_apply_status_label", None)):
            try:
                from PySide6.QtCore import QTimer
                QTimer.singleShot(5000, lambda: self._safe_label_set_text("runtime_apply_status_label", "-"))
            except Exception:
                pass
        QMessageBox.information(self, "Runtime Settings", f"Runtime parameters applied successfully.\n\n{summary}")

    # ------------------------------------------------------------------
    # Fleet-scale controls: 24 BMS + 48 PCS heartbeat and broadcast PCS commands
    # ------------------------------------------------------------------
    def _fleet_bms_names(self) -> list[str]:
        return [str(d.get("name", "")).strip() for d in getattr(self, "devices", []) if str(d.get("name", "")).strip()]

    def _fleet_pcs_names(self) -> list[str]:
        names: list[str] = []
        for name, cfg in getattr(self, "pcs_configs", {}).items():
            if not str(name).strip():
                continue
            if not isinstance(cfg, dict):
                continue
            if not bool(cfg.get("enabled", False)):
                continue
            if not str(cfg.get("host", "")).strip():
                continue
            points = cfg.get("points", {}) or {}
            if "heartbeat" not in points:
                continue
            names.append(str(name).strip())
        return names

    def handle_start_fleet_heartbeats(self) -> None:
        # Safer fleet heartbeat behavior: do not create separate BMS/PCS
        # reconnecting heartbeat workers. BMS heartbeat is queued only on
        # already-running BMS polling workers. PCS heartbeat remains disabled
        # by default because this site is currently using strategy/command
        # control without PCS heartbeat.
        bms_names = self._online_bms_worker_names() if hasattr(self, "_online_bms_worker_names") else []
        if not bms_names:
            QMessageBox.information(self, "Fleet Heartbeat", "No running BMS polling workers. Start BMS monitoring first.")
            return
        started = 0
        for name in bms_names:
            if self._start_bms_queue_heartbeat(name):
                started += 1
        if hasattr(self, "pcs_hb_state_label"):
            self.pcs_hb_state_label.setText(f"BMS HB queued: {started}")
        self.last_heartbeat_status = f"BMS HB queued: {started}"
        self.refresh_global_status_bar()
        self.control_log(f"[FLEET] Queued BMS heartbeat on existing polling workers: {started}/{len(bms_names)}. PCS heartbeat not started.")

    def handle_stop_fleet_heartbeats(self) -> None:
        stopped = 0
        if hasattr(self, "bms_queue_heartbeat_timers"):
            for name in list(self.bms_queue_heartbeat_timers.keys()):
                if self._stop_bms_queue_heartbeat(name):
                    stopped += 1
        if hasattr(self, "pcs_hb_state_label"):
            self.pcs_hb_state_label.setText("BMS HB: stopped")
        self.last_heartbeat_status = "Stopped"
        self.refresh_global_status_bar()
        self.control_log(f"[FLEET] BMS queued heartbeats stopped: {stopped}. Command workers were left running.")

    def _ensure_pcs_fleet_running(self) -> list[str]:
        pcs_names = self._fleet_pcs_names()
        if not pcs_names:
            return []
        def pcs_factory(name: str):
            return self.create_pcs_client_for_pcs_name(name)
        self.fleet_manager.start_pcs_heartbeats(pcs_names, pcs_factory, interval_s=float(getattr(self, "heartbeat_interval", 1.0)))
        return pcs_names

    def handle_fleet_pcs_start(self) -> None:
        if getattr(self, "_runtime_should_handle_command", lambda: False)():
            result = self._runtime_post("/api/pcs/fleet-command", {"method": "start"}, timeout_s=5.0) if hasattr(self, "_runtime_post") else {"ok": False, "error": "runtime helper missing"}
            if result.get("ok"):
                self.control_log(f"[RUNTIME_UI] Fleet PCS start sent to runtime: {result.get('queued', 0)}/{result.get('total', 0)}")
                self.poll_runtime_snapshot() if hasattr(self, "poll_runtime_snapshot") else None
            else:
                QMessageBox.warning(self, "Runtime API", f"Fleet PCS start failed: {result.get('error', result)}")
            return
        pcs_names = self._ensure_pcs_fleet_running()
        count = self.fleet_manager.enqueue_pcs_command(pcs_names, "start", label="PCS start")
        self.control_log(f"[FLEET] Queued PCS start for {count}/{len(pcs_names)} PCS")

    def handle_fleet_pcs_stop(self) -> None:
        if getattr(self, "_runtime_should_handle_command", lambda: False)():
            result = self._runtime_post("/api/pcs/fleet-command", {"method": "stop"}, timeout_s=5.0) if hasattr(self, "_runtime_post") else {"ok": False, "error": "runtime helper missing"}
            if result.get("ok"):
                self.control_log(f"[RUNTIME_UI] Fleet PCS stop sent to runtime: {result.get('queued', 0)}/{result.get('total', 0)}")
                self.poll_runtime_snapshot() if hasattr(self, "poll_runtime_snapshot") else None
            else:
                QMessageBox.warning(self, "Runtime API", f"Fleet PCS stop failed: {result.get('error', result)}")
            return
        pcs_names = self._ensure_pcs_fleet_running()
        # Stop should be fast and broadcast through existing persistent clients.
        count = self.fleet_manager.enqueue_pcs_command(pcs_names, "stop", label="PCS stop")
        self.control_log(f"[FLEET] Queued PCS stop for {count}/{len(pcs_names)} PCS")

    def handle_fleet_set_active_power(self) -> None:
        value = float(self.pcs_active_power_spin.value()) if hasattr(self, "pcs_active_power_spin") else 0.0
        if getattr(self, "_runtime_should_handle_command", lambda: False)():
            result = self._runtime_post("/api/pcs/fleet-command", {"method": "set_active_power", "value": value}, timeout_s=5.0) if hasattr(self, "_runtime_post") else {"ok": False, "error": "runtime helper missing"}
            if result.get("ok"):
                self.control_log(f"[RUNTIME_UI] Fleet active power sent to runtime: {value:.1f} kW, {result.get('queued', 0)}/{result.get('total', 0)}")
                self.poll_runtime_snapshot() if hasattr(self, "poll_runtime_snapshot") else None
            else:
                QMessageBox.warning(self, "Runtime API", f"Fleet active power failed: {result.get('error', result)}")
            return
        pcs_names = self._ensure_pcs_fleet_running()
        count = self.fleet_manager.enqueue_pcs_command(pcs_names, "set_active_power", value, label=f"set_active_power={value:.1f}kW")
        self.control_log(f"[FLEET] Queued set active power {value:.1f} kW for {count}/{len(pcs_names)} PCS")

    def handle_fleet_set_reactive_power(self) -> None:
        value = float(self.pcs_reactive_power_spin.value()) if hasattr(self, "pcs_reactive_power_spin") else 0.0
        if getattr(self, "_runtime_should_handle_command", lambda: False)():
            result = self._runtime_post("/api/pcs/fleet-command", {"method": "set_reactive_power", "value": value}, timeout_s=5.0) if hasattr(self, "_runtime_post") else {"ok": False, "error": "runtime helper missing"}
            if result.get("ok"):
                self.control_log(f"[RUNTIME_UI] Fleet reactive power sent to runtime: {value:.1f} kvar, {result.get('queued', 0)}/{result.get('total', 0)}")
                self.poll_runtime_snapshot() if hasattr(self, "poll_runtime_snapshot") else None
            else:
                QMessageBox.warning(self, "Runtime API", f"Fleet reactive power failed: {result.get('error', result)}")
            return
        pcs_names = self._ensure_pcs_fleet_running()
        count = self.fleet_manager.enqueue_pcs_command(pcs_names, "set_reactive_power", value, label=f"set_reactive_power={value:.1f}kvar")
        self.control_log(f"[FLEET] Queued set reactive power {value:.1f} kvar for {count}/{len(pcs_names)} PCS")

    def handle_fleet_status_summary(self) -> None:
        snapshots = self.fleet_manager.snapshots()
        total = len(snapshots)
        online = sum(1 for s in snapshots.values() if s.get("online"))
        errors = sum(1 for s in snapshots.values() if s.get("last_error"))
        bms = sum(1 for k in snapshots if k.startswith("BMS:"))
        pcs = sum(1 for k in snapshots if k.startswith("PCS:"))
        msg = f"Fleet workers={total}, online={online}, errors={errors}, BMS={bms}, PCS={pcs}"
        self.control_log(f"[FLEET] {msg}")
        if hasattr(self, "pcs_hb_state_label"):
            self.pcs_hb_state_label.setText(f"Fleet: {online}/{total} online")
