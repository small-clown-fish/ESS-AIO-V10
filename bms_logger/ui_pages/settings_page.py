from __future__ import annotations

from pathlib import Path
import csv
from PySide6.QtCharts import QChart, QChartView, QLineSeries, QValueAxis
from PySide6.QtCore import Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
    QMessageBox,
    QSizePolicy,
)

def _build_settings_tab(self, tabs: QTabWidget) -> None:
        settings_tab = QScrollArea()
        settings_tab.setWidgetResizable(True)

        settings_content = QWidget()
        settings_layout = QVBoxLayout(settings_content)
        settings_layout.setContentsMargins(14, 14, 14, 14)
        settings_layout.setSpacing(12)
        settings_tab.setWidget(settings_content)

        header_row = QHBoxLayout()
        title_label = QLabel("Runtime Settings")
        title_label.setObjectName("PageTitle")
        hint_label = QLabel("Only active runtime settings are shown by default. Legacy strategy/protection settings are kept in config but hidden from this page.")
        hint_label.setObjectName("PageHint")
        header_row.addWidget(title_label)
        header_row.addSpacing(16)
        header_row.addWidget(hint_label)
        header_row.addStretch()
        settings_layout.addLayout(header_row)

        self.fake_mode_combo = QComboBox()
        self.fake_mode_combo.addItems(["Real", "Fake"])
        self.fake_mode_combo.setCurrentText("Fake" if self.fake_mode else "Real")
        self.fake_mode_combo.setToolTip("Switch between real Modbus clients and local fake clients.")

        self.pcs_control_ui_combo = QComboBox()
        self.pcs_control_ui_combo.addItems(["Enabled", "Disabled"])
        self.pcs_control_ui_combo.setCurrentText("Enabled" if getattr(self, "pcs_control_ui_enabled", True) else "Disabled")
        self.pcs_control_ui_combo.setToolTip("Show or hide PCS Control tab in the Control page for release builds.")

        self.runtime_api_mode_combo = QComboBox()
        self.runtime_api_mode_combo.addItems(["Local / Direct", "Runtime API Client"])
        self.runtime_api_mode_combo.setCurrentText("Runtime API Client" if getattr(self, "runtime_api_enabled", False) else "Local / Direct")
        self.runtime_api_mode_combo.setToolTip("Runtime split: UI sends high-level commands to ESS-AIO-Runtime on localhost instead of creating local workers.")

        self.runtime_dominant_combo = QComboBox()
        self.runtime_dominant_combo.addItems(["Enabled", "Disabled"])
        self.runtime_dominant_combo.setCurrentText("Enabled" if getattr(self, "runtime_dominant_mode", True) else "Disabled")
        self.runtime_dominant_combo.setToolTip("When Runtime API mode is enabled, force UI controls to use runtime API and prevent local worker creation.")

        self.runtime_api_url_edit = QLineEdit(str(getattr(self, "runtime_api_url", "http://127.0.0.1:8765")))
        self.runtime_api_url_edit.setToolTip("Runtime API URL, normally http://127.0.0.1:8765")

        self.runtime_snapshot_poll_spin = QSpinBox()
        self.runtime_snapshot_poll_spin.setRange(500, 10000)
        self.runtime_snapshot_poll_spin.setSingleStep(500)
        self.runtime_snapshot_poll_spin.setValue(int(getattr(self, "runtime_snapshot_poll_interval_ms", 2000)))
        self.runtime_snapshot_poll_spin.setSuffix(" ms")

        self.heartbeat_interval_spin = QDoubleSpinBox()
        self.heartbeat_interval_spin.setRange(0.2, 60.0)
        self.heartbeat_interval_spin.setDecimals(1)
        self.heartbeat_interval_spin.setValue(self.heartbeat_interval)
        self.heartbeat_interval_spin.setSuffix(" s")

        self.hv_timeout_spin = QDoubleSpinBox()
        self.hv_timeout_spin.setRange(1.0, 600.0)
        self.hv_timeout_spin.setDecimals(1)
        self.hv_timeout_spin.setValue(self.hv_step_timeout)
        self.hv_timeout_spin.setSuffix(" s")

        self.hv_poll_interval_spin = QDoubleSpinBox()
        self.hv_poll_interval_spin.setRange(0.2, 30.0)
        self.hv_poll_interval_spin.setDecimals(1)
        self.hv_poll_interval_spin.setValue(self.hv_poll_interval)
        self.hv_poll_interval_spin.setSuffix(" s")

        self.pcs_zero_power_spin = QDoubleSpinBox()
        self.pcs_zero_power_spin.setRange(0.0, 1000.0)
        self.pcs_zero_power_spin.setDecimals(2)
        self.pcs_zero_power_spin.setValue(self.pcs_zero_power_threshold)
        self.pcs_zero_power_spin.setSuffix(" kW")

        self.charge_cutoff_voltage_spin = QDoubleSpinBox()
        self.charge_cutoff_voltage_spin.setRange(0.0, 10000.0)
        self.charge_cutoff_voltage_spin.setDecimals(1)
        self.charge_cutoff_voltage_spin.setValue(self.charge_cutoff_max_cell_voltage)
        self.charge_cutoff_voltage_spin.setSuffix(" mV")

        self.discharge_cutoff_voltage_spin = QDoubleSpinBox()
        self.discharge_cutoff_voltage_spin.setRange(0.0, 10000.0)
        self.discharge_cutoff_voltage_spin.setDecimals(1)
        self.discharge_cutoff_voltage_spin.setValue(self.discharge_cutoff_min_cell_voltage)
        self.discharge_cutoff_voltage_spin.setSuffix(" mV")

        self.cutoff_mode_combo = QComboBox()
        self.cutoff_mode_combo.addItems(["Disabled", "Alarm Only", "Stop PCS", "HV Off"])
        self.cutoff_mode_combo.setCurrentText(self.cutoff_mode)

        self.cutoff_trigger_confirm_spin = QSpinBox()
        self.cutoff_trigger_confirm_spin.setRange(1, 20)
        self.cutoff_trigger_confirm_spin.setValue(self.cutoff_trigger_confirm_count)

        self.cutoff_recover_confirm_spin = QSpinBox()
        self.cutoff_recover_confirm_spin.setRange(1, 20)
        self.cutoff_recover_confirm_spin.setValue(self.cutoff_recover_confirm_count)

        self.derating_enabled_combo = QComboBox()
        self.derating_enabled_combo.addItems(["Disabled", "Enabled"])
        self.derating_enabled_combo.setCurrentText("Enabled" if self.power_derating_enabled else "Disabled")

        self.derating_margin_spin = QDoubleSpinBox()
        self.derating_margin_spin.setRange(1.0, 500.0)
        self.derating_margin_spin.setValue(self.derating_margin_mv)
        self.derating_margin_spin.setSuffix(" mV")

        self.derating_power_spin = QDoubleSpinBox()
        self.derating_power_spin.setRange(-100000.0, 100000.0)
        self.derating_power_spin.setValue(self.derating_power_kw)
        self.derating_power_spin.setSuffix(" kW")

        self.power_tracking_enabled_combo = QComboBox()
        self.power_tracking_enabled_combo.addItems(["Disabled", "Enabled"])
        self.power_tracking_enabled_combo.setCurrentText("Enabled" if self.power_tracking_enabled else "Disabled")

        self.power_tracking_tolerance_spin = QDoubleSpinBox()
        self.power_tracking_tolerance_spin.setRange(0.1, 10000.0)
        self.power_tracking_tolerance_spin.setValue(self.power_tracking_tolerance_kw)
        self.power_tracking_tolerance_spin.setSuffix(" kW")

        self.power_tracking_confirm_spin = QSpinBox()
        self.power_tracking_confirm_spin.setRange(1, 20)
        self.power_tracking_confirm_spin.setValue(self.power_tracking_confirm_count)

        self.power_retry_enabled_combo = QComboBox()
        self.power_retry_enabled_combo.addItems(["Disabled", "Enabled"])
        self.power_retry_enabled_combo.setCurrentText("Enabled" if self.power_tracking_auto_retry else "Disabled")

        self.power_retry_interval_spin = QSpinBox()
        self.power_retry_interval_spin.setRange(1, 60)
        self.power_retry_interval_spin.setValue(self.power_tracking_retry_interval)
        self.power_retry_interval_spin.setSuffix(" s")

        self.power_retry_max_spin = QSpinBox()
        self.power_retry_max_spin.setRange(1, 10)
        self.power_retry_max_spin.setValue(self.power_tracking_max_retry)

        self.pcs_fault_protection_combo = QComboBox()
        self.pcs_fault_protection_combo.addItems(["Disabled", "Alarm Only", "Stop PCS", "HV Off"])
        self.pcs_fault_protection_combo.setCurrentText(self.pcs_fault_protection_mode)

        self.pcs_fault_confirm_spin = QSpinBox()
        self.pcs_fault_confirm_spin.setRange(1, 20)
        self.pcs_fault_confirm_spin.setValue(self.pcs_fault_confirm_count)

        self.alarm_window_before_spin = QSpinBox()
        self.alarm_window_before_spin.setRange(0, 120)
        self.alarm_window_before_spin.setValue(self.alarm_history_window_before_minutes)
        self.alarm_window_before_spin.setSuffix(" min")

        self.alarm_window_after_spin = QSpinBox()
        self.alarm_window_after_spin.setRange(0, 120)
        self.alarm_window_after_spin.setValue(self.alarm_history_window_after_minutes)
        self.alarm_window_after_spin.setSuffix(" min")

        self.worker_stagger_spin = QDoubleSpinBox()
        self.worker_stagger_spin.setRange(0.0, 10.0)
        self.worker_stagger_spin.setDecimals(2)
        self.worker_stagger_spin.setValue(self.worker_start_stagger_seconds)
        self.worker_stagger_spin.setSuffix(" s")

        self.performance_mode_combo = QComboBox()
        self.performance_mode_combo.addItems(["Enabled", "Disabled"])
        self.performance_mode_combo.setCurrentText("Enabled" if getattr(self, "performance_mode_enabled", True) else "Disabled")
        self.performance_mode_combo.setToolTip("Windows performance mode lowers UI/log/curve refresh pressure without changing Modbus sampling.")

        self.large_site_mode_combo = QComboBox()
        self.large_site_mode_combo.addItems(["Enabled", "Disabled"])
        self.large_site_mode_combo.setCurrentText("Enabled" if getattr(self, "large_site_mode_enabled", True) else "Disabled")
        self.large_site_mode_combo.setToolTip("Limits concurrent BMS Modbus IO and batches UI snapshot delivery for 40-60 device sites.")

        self.max_parallel_bms_io_spin = QSpinBox()
        self.max_parallel_bms_io_spin.setRange(1, 64)
        self.max_parallel_bms_io_spin.setValue(int(getattr(self, "max_parallel_bms_io", 10)))
        self.max_parallel_bms_io_spin.setToolTip("Maximum BMS connect/read/write operations allowed at the same time. 8-12 is recommended on Windows.")

        self.ui_refresh_interval_spin = QDoubleSpinBox()
        self.ui_refresh_interval_spin.setRange(0.2, 30.0)
        self.ui_refresh_interval_spin.setDecimals(1)
        self.ui_refresh_interval_spin.setValue(self.ui_refresh_interval)
        self.ui_refresh_interval_spin.setSuffix(" s")

        self.curve_refresh_interval_spin = QDoubleSpinBox()
        self.curve_refresh_interval_spin.setRange(1.0, 60.0)
        self.curve_refresh_interval_spin.setDecimals(1)
        self.curve_refresh_interval_spin.setValue(float(getattr(self, "curve_refresh_interval", 5.0)))
        self.curve_refresh_interval_spin.setSuffix(" s")

        self.status_refresh_interval_spin = QDoubleSpinBox()
        self.status_refresh_interval_spin.setRange(1.0, 60.0)
        self.status_refresh_interval_spin.setDecimals(1)
        self.status_refresh_interval_spin.setValue(float(getattr(self, "status_refresh_interval", 5.0)))
        self.status_refresh_interval_spin.setSuffix(" s")

        self.log_flush_interval_spin = QSpinBox()
        self.log_flush_interval_spin.setRange(300, 10000)
        self.log_flush_interval_spin.setSingleStep(100)
        self.log_flush_interval_spin.setValue(int(getattr(self, "log_flush_interval_ms", 1000)))
        self.log_flush_interval_spin.setSuffix(" ms")

        def make_card(title: str, rows: list[tuple[str, QWidget]]) -> QGroupBox:
            group = QGroupBox(title)
            form = QFormLayout(group)
            form.setContentsMargins(12, 10, 12, 10)
            form.setSpacing(7)
            form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
            for label, widget in rows:
                form.addRow(label, widget)
            return group

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(12)

        runtime_card = make_card("Runtime", [
            ("Mode", self.fake_mode_combo),
            ("Runtime API", self.runtime_api_mode_combo),
            ("Runtime Dominant", self.runtime_dominant_combo),
            ("Runtime URL", self.runtime_api_url_edit),
            ("Runtime Poll", self.runtime_snapshot_poll_spin),
            ("PCS Control UI", self.pcs_control_ui_combo),
            ("Heartbeat", self.heartbeat_interval_spin),
            ("HV Timeout", self.hv_timeout_spin),
            ("HV Poll", self.hv_poll_interval_spin),
            ("PCS Zero", self.pcs_zero_power_spin),
        ])

        cutoff_card = make_card("Cutoff Protection", [
            ("Charge Max", self.charge_cutoff_voltage_spin),
            ("Discharge Min", self.discharge_cutoff_voltage_spin),
            ("Mode", self.cutoff_mode_combo),
            ("Trigger Count", self.cutoff_trigger_confirm_spin),
            ("Recover Count", self.cutoff_recover_confirm_spin),
        ])

        derating_card = make_card("Power Derating", [
            ("Enabled", self.derating_enabled_combo),
            ("Margin", self.derating_margin_spin),
            ("Target Power", self.derating_power_spin),
        ])

        tracking_card = make_card("Power Tracking", [
            ("Enabled", self.power_tracking_enabled_combo),
            ("Tolerance", self.power_tracking_tolerance_spin),
            ("Confirm Count", self.power_tracking_confirm_spin),
            ("Auto Retry", self.power_retry_enabled_combo),
            ("Retry Interval", self.power_retry_interval_spin),
            ("Max Retry", self.power_retry_max_spin),
        ])

        pcs_card = make_card("PCS Protection", [
            ("Protection", self.pcs_fault_protection_combo),
            ("Confirm Count", self.pcs_fault_confirm_spin),
        ])

        history_card = make_card("History / Alarm Window", [
            ("Before", self.alarm_window_before_spin),
            ("After", self.alarm_window_after_spin),
        ])

        scheduler_card = make_card("Scheduler / Performance", [
            ("Performance Mode", self.performance_mode_combo),
            ("Large Site Mode", self.large_site_mode_combo),
            ("Max Parallel BMS IO", self.max_parallel_bms_io_spin),
            ("Start Stagger", self.worker_stagger_spin),
            ("UI Refresh", self.ui_refresh_interval_spin),
            ("Curve Refresh", self.curve_refresh_interval_spin),
            ("Status Refresh", self.status_refresh_interval_spin),
            ("Log Flush", self.log_flush_interval_spin),
        ])

        # Keep only currently active settings visible.  Older cutoff/derating/tracking
        # widgets are still created above so existing runtime_config values remain
        # loadable/savable, but they are no longer shown because Cluster Strategy
        # now owns those controls.
        grid.addWidget(runtime_card, 0, 0)
        grid.addWidget(scheduler_card, 0, 1)

        legacy_note = QLabel(
            "Legacy Cutoff / Derating / Power Tracking / PCS Protection settings are hidden here. "
            "Use Cluster Strategy for power/cutoff behavior; use PCS Control for PCS operations."
        )
        legacy_note.setWordWrap(True)
        legacy_note.setObjectName("PageHint")
        grid.addWidget(legacy_note, 1, 0, 1, 2)

        settings_layout.addLayout(grid)

        action_row = QHBoxLayout()
        action_row.addStretch()
        self.runtime_apply_status_label = QLabel("-")
        self.runtime_apply_status_label.setObjectName("PageHint")
        action_row.addWidget(self.runtime_apply_status_label)
        self.apply_runtime_params_btn = QPushButton("Apply Runtime Params")
        self.apply_runtime_params_btn.clicked.connect(self.apply_runtime_params)
        self.apply_runtime_params_btn.setMinimumWidth(190)
        action_row.addWidget(self.apply_runtime_params_btn)
        settings_layout.addLayout(action_row)
        settings_layout.addStretch()

        tabs.addTab(settings_tab, "Settings")

