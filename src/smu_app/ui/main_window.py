"""PyQt6 + PyQtGraph Integrated SMU Desktop Control & Analysis Dashboard
(Tek QuickStart Modern 3-Column Layout + Dark Oscilloscope-Style High-Contrast Graph + Live KPI Readout Cards + Talker/Listener Dock).

Preserves 100% of Phase 1~3 capabilities, all 9 UX upgrades, 2450 real hardware support,
persistent export directory, Tek QuickStart 5-sheet Excel report, and live animated progress banner.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import sys
import numpy as np

from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QCloseEvent, QColor, QFont, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStatusBar,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)
import pyqtgraph as pg

from smu_app.analysis.auto_diagnosis import AutoDiagnosisEngine
from smu_app.analysis.iv_pulse_analyzer import (
    analyze_measurement_points,
    compute_dual_cursor_metrics,
    lttb_downsample,
)
from smu_app.core.command_spy import CommandSpy, SpyEntry
from smu_app.core.models import (
    ChannelConfig,
    DiagnosisItem,
    DiagnosisReport,
    LogicalChannel,
    MeasureFunction,
    MeasurementResult,
    MultiUnitSyncMode,
    OutputOffMode,
    RepeatMode,
    SenseMode,
    SourceFunction,
    SweepRecipe,
    SweepType,
    TerminalLocation,
    WaveformStep,
)
from smu_app.core.profiles import ProfileRegistry
from smu_app.core.visa_manager import VISAConnectionManager
from smu_app.drivers.base_smu import BaseTSPDriver, create_smu_driver
from smu_app.features.pulse_digitizer import (
    HighSpeedPulseDigitizerEngine,
    PulseDigitizerConfig,
)
from smu_app.features.sweep_engine import SweepExecutionEngine
from smu_app.features.waveform_builder import (
    EasyWaveformShape,
    WaveformBuilder,
)
from smu_app.safety.safety_manager import SafetyAndSOAValidator
from smu_app.simulator.virtual_smu import VirtualDUTType, VirtualSMUTransport
from smu_app.storage.exporter import DataExporter
from smu_app.storage.setup_manager import (
    SetupPersistenceManager,
    clamp_value_with_notice,
    find_manual_pdf_for_model,
)
from smu_app.sync.cluster_manager import ChannelRole, CombinedOutputConfig
from smu_app.ui.combining_wizard import OutputCombiningWizardDialog
from smu_app.ui.connection_wizard import (
    SMUConnectionWizardDialog,
    fit_and_center_window,
    get_available_screen_geometry,
    open_model_manual_pdf,
)
from smu_app.ui.detached_graph_dialog import DetachedGraphWindow
from smu_app.ui.diagnosis_dialog import AutoDiagnosisDialog
from smu_app.ui.multi_channel_dialog import MultiChannelConfigDialog
from smu_app.ui.theme import GLOBAL_STYLESHEET
from smu_app.ui.waveform_editor_dialog import WaveformEditorDialog


class MeasurementWorkerThread(QThread):
    """Background worker thread so the UI stays responsive, animates the progress bar,
    updates elapsed time, and keeps the ALL STOP button clickable during long hardware sweeps.
    """

    finished_ok = pyqtSignal(object)
    failed_err = pyqtSignal(str)

    def __init__(
        self,
        engine: SweepExecutionEngine,
        cfg: ChannelConfig,
        recipe: SweepRecipe,
        node_count: int,
        specific_variant: str | None,
        logical_channels: list[LogicalChannel],
        combined_config: CombinedOutputConfig | None,
        pulse_config: PulseDigitizerConfig | None,
        sync_mode: MultiUnitSyncMode = MultiUnitSyncMode.TSPLINK_HW,
        visa_mgr: VISAConnectionManager | None = None,
    ) -> None:
        super().__init__()
        self.engine = engine
        self.cfg = cfg
        self.recipe = recipe
        self.node_count = node_count
        self.specific_variant = specific_variant
        self.logical_channels = list(logical_channels)
        self.combined_config = combined_config
        self.pulse_config = pulse_config
        self.sync_mode = sync_mode
        self.visa_mgr = visa_mgr

    def run(self) -> None:
        try:
            res = self.engine.run_measurement(
                cfg=self.cfg,
                recipe=self.recipe,
                node_count=self.node_count,
                specific_variant=self.specific_variant,
                logical_channels=self.logical_channels,
                combined_config=self.combined_config,
                pulse_config=self.pulse_config,
                sync_mode=self.sync_mode,
                visa_mgr=self.visa_mgr,
            )
            self.finished_ok.emit(res)
        except Exception as exc:
            self.failed_err.emit(str(exc))


class KPIReadoutCard(QFrame):
    """Compact high-readability live measurement KPI readout card for GUI ver0.1."""

    def __init__(self, title: str, subtitle: str, unit_placeholder: str, accent_color: str = "#0284c7") -> None:
        super().__init__()
        self.setStyleSheet(
            "KPIReadoutCard { background-color: #ffffff; border: 1px solid #e2e8f0; border-radius: 5px; }"
        )
        v = QVBoxLayout(self)
        v.setContentsMargins(6, 4, 6, 4)
        v.setSpacing(2)

        lbl_t = QLabel(f"● {title}")
        lbl_t.setStyleSheet("font-weight: bold; color: #0f172a; font-size: 8.3pt; border: none;")
        v.addWidget(lbl_t)

        val_row = QHBoxLayout()
        val_row.setContentsMargins(0, 0, 0, 0)
        lbl_sub = QLabel(subtitle)
        lbl_sub.setStyleSheet("color: #64748b; font-size: 7.4pt; border: none;")
        val_row.addWidget(lbl_sub, 1)

        self.lbl_value = QLabel(unit_placeholder)
        self.lbl_value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.lbl_value.setStyleSheet(
            f"background-color: #f0fdf4; color: {accent_color}; border: 1px solid #bbf7d0; "
            "border-radius: 4px; padding: 1px 6px; font-family: Consolas, '맑은 고딕'; font-weight: bold; font-size: 8.6pt;"
        )
        val_row.addWidget(self.lbl_value)
        v.addLayout(val_row)

    def set_value(self, text: str) -> None:
        self.lbl_value.setText(text)


class SMUMainWindow(QMainWindow):
    """Modern Tek QuickStart-Inspired Integrated SMU Control, Live Graphing & Analysis Window."""

    _SERIES_COLORS = ["#38bdf8", "#4ade80", "#facc15", "#f472b6", "#c084fc", "#fb923c"]
    spy_entry_received = pyqtSignal(object)

    def __init__(self, show_startup_wizard: bool = False, prefer_simulator: bool = False) -> None:
        super().__init__()
        self.setStyleSheet(GLOBAL_STYLESHEET)

        self.registry = ProfileRegistry()
        self.spy = CommandSpy()
        self.visa_mgr = VISAConnectionManager(spy=self.spy)
        self.exporter = DataExporter()
        self.prefer_simulator = prefer_simulator
        self.active_resource_name: str = "SIM::2450::INSTR"
        self.driver: BaseTSPDriver = create_smu_driver("2450", spy=self.spy, registry=self.registry)
        self.engine = SweepExecutionEngine(self.driver)
        self.last_result: MeasurementResult | None = None
        self.run_history: list[MeasurementResult] = []
        self.single_dc_points: list = []
        self.custom_steps: list[WaveformStep] = WaveformBuilder.build_staircase_wave(0.0, 1.2, 13, 0.005, dual_return=True)
        self.logical_channels: list[LogicalChannel] = []
        self.multi_sync_mode: MultiUnitSyncMode = MultiUnitSyncMode.TSPLINK_HW
        self.combined_config: CombinedOutputConfig | None = None
        self.last_acal_temp_c: float = 23.0
        self.detached_windows: list[DetachedGraphWindow] = []
        self.suppress_range_popup: bool = False
        self.last_range_notice: str = ""
        self.last_checklist_status: list[dict] = []
        self._geometry_locked: bool = False
        self._meas_worker: MeasurementWorkerThread | None = None
        self._meas_elapsed_s: float = 0.0
        self._meas_timer = QTimer(self)
        self._meas_timer.setInterval(200)
        self._meas_timer.timeout.connect(self._on_measurement_timer_tick)
        self._is_measuring: bool = False
        self.model_buttons: dict[str, QPushButton] = {}
        self._log_visible: bool = True

        avail = get_available_screen_geometry(self)
        self.is_compact_screen = avail.width() <= 1600 or avail.height() <= 920
        self.setWindowTitle(
            f"Tektronix & Keithley SMU Advanced Suite v3.0 ({avail.width()}×{avail.height()})"
        )

        self._build_ui()
        fit_and_center_window(
            self,
            target_w_ratio=0.95 if self.is_compact_screen else 0.92,
            target_h_ratio=0.94 if self.is_compact_screen else 0.90,
            min_w=1080,
            min_h=660,
        )
        self._geometry_locked = True

        # Thread-safe Qt signal bridge so background worker threads (Mode B / HW sweep)
        # never touch QTableWidget/QBasicTimer from non-GUI threads.
        self.spy_entry_received.connect(self._on_spy_entry, Qt.ConnectionType.AutoConnection)
        self.spy.subscribe(self.spy_entry_received.emit)
        self._on_model_changed("2450")
        self._populate_waveform_table()
        self.refresh_waveform_preview()
        self.refresh_soa_chart()

        if SetupPersistenceManager.DEFAULT_LAST_SESSION_PATH.exists():
            try:
                saved_pref_dir = SetupPersistenceManager.get_saved_export_dir()
                self.load_setup_from_path(SetupPersistenceManager.DEFAULT_LAST_SESSION_PATH, silent=True)
                self.set_export_directory(saved_pref_dir, persist=False)
            except Exception:
                pass

        if show_startup_wizard:
            QTimer.singleShot(120, self.open_connection_wizard)

    def show_adaptive(self) -> None:
        avail = get_available_screen_geometry(self)
        if avail.width() <= 1600 or avail.height() <= 920:
            self.showMaximized()
        else:
            self.show()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._meas_timer.isActive():
            self._meas_timer.stop()
        if self._meas_worker is not None and self._meas_worker.isRunning():
            self._meas_worker.quit()
            self._meas_worker.wait(2000)
        try:
            self.spy_entry_received.disconnect(self._on_spy_entry)
        except Exception:
            pass
        try:
            self.save_setup_to_path(SetupPersistenceManager.DEFAULT_LAST_SESSION_PATH, silent=True)
        except Exception:
            pass
        super().closeEvent(event)

    # =========================================================================
    # UI Construction (Tek QuickStart-Inspired Modern Layout)
    # =========================================================================

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        m = 5 if self.is_compact_screen else 8
        root.setContentsMargins(m, m, m, m)
        root.setSpacing(5)

        # ---------------------------------------------------------------------
        # 1. GLOBAL DARK NAVY TOP HEADER BAR (Matches Tek QuickStart Image)
        # ---------------------------------------------------------------------
        header_bar = QFrame()
        header_bar.setStyleSheet(
            "QFrame { background-color: #0b2545; color: white; border-radius: 6px; }"
        )
        header_bar.setFixedHeight(44 if self.is_compact_screen else 48)
        h_layout = QHBoxLayout(header_bar)
        h_layout.setContentsMargins(10, 4, 10, 4)
        h_layout.setSpacing(7)

        # SSAITEK Logo Badge
        lbl_logo = QLabel()
        lbl_logo.setStyleSheet(
            "background-color: #ffffff; border: 1px solid #7dd3fc; border-radius: 4px; padding: 2px 6px;"
        )
        logo_path = Path(__file__).resolve().parents[3] / "configs" / "assets" / "ssaitek_logo.png"
        if logo_path.exists():
            pix = QPixmap(str(logo_path))
            if not pix.isNull():
                lbl_logo.setPixmap(pix.scaledToHeight(24, Qt.TransformationMode.SmoothTransformation))
            else:
                lbl_logo.setText("SSAITEK")
        else:
            lbl_logo.setText("SSAITEK")
            lbl_logo.setStyleSheet("background-color: white; color: #0284c7; font-weight: bold; padding: 3px 8px; border-radius: 4px;")
        h_layout.addWidget(lbl_logo)

        lbl_brand = QLabel("⚡ SMU PRO SUITE")
        lbl_brand.setFont(QFont("맑은 고딕", 11, QFont.Weight.Bold))
        lbl_brand.setStyleSheet("color: #38bdf8; border: none;")
        h_layout.addWidget(lbl_brand)

        lbl_v = QLabel("VISA 리소스:")
        lbl_v.setStyleSheet("color: #e2e8f0; font-weight: bold; border: none;")
        h_layout.addWidget(lbl_v)

        self.cmb_visa_bar = QComboBox()
        self.cmb_visa_bar.setEditable(True)
        self.cmb_visa_bar.setMinimumWidth(210)
        self.cmb_visa_bar.setMaximumWidth(340)
        self.cmb_visa_bar.setStyleSheet("background-color: white; color: #0f172a; padding: 3px 6px; border-radius: 4px;")
        self.cmb_visa_bar.currentIndexChanged.connect(self._on_top_visa_resource_changed)
        h_layout.addWidget(self.cmb_visa_bar)

        btn_rescan = QPushButton("🔄 검색")
        btn_rescan.setStyleSheet("background-color: #2563eb; color: white; font-weight: bold; border: none; padding: 5px 10px;")
        btn_rescan.clicked.connect(lambda: self._populate_top_visa_combo(self.cmb_model.currentText()))
        h_layout.addWidget(btn_rescan)

        self.chk_sim_mode = QCheckBox("시뮬레이션 모드")
        self.chk_sim_mode.setChecked(self.prefer_simulator)
        self.chk_sim_mode.setStyleSheet(
            "QCheckBox { background-color: transparent; color: #f8fafc; font-weight: bold; spacing: 5px; padding: 2px 4px; border: none; }"
            "QCheckBox::indicator { width: 15px; height: 15px; background-color: #ffffff; border: 1.5px solid #94a3b8; border-radius: 3px; }"
            "QCheckBox::indicator:checked { background-color: #38bdf8; border: 1.5px solid #ffffff; }"
        )
        self.chk_sim_mode.toggled.connect(self._on_sim_mode_toggled)
        h_layout.addWidget(self.chk_sim_mode)

        self.lbl_conn_badge = QLabel("[대기 중]")
        self.lbl_conn_badge.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.lbl_conn_badge.setStyleSheet(
            "color: #4ade80; font-weight: bold; font-size: 8.8pt; border: none; padding: 0 6px;"
        )
        h_layout.addWidget(self.lbl_conn_badge, stretch=1)

        self.btn_conn_wizard = QPushButton("📋 사전 준비 점검표")
        self.btn_conn_wizard.setStyleSheet("background-color: #1d4ed8; color: white; font-weight: bold; border: none; padding: 5px 10px;")
        self.btn_conn_wizard.clicked.connect(self.open_connection_wizard)
        h_layout.addWidget(self.btn_conn_wizard)

        self.btn_save_setup = QPushButton("💾 설정 저장")
        self.btn_save_setup.setStyleSheet("background-color: #334155; color: white; font-weight: bold; border: none; padding: 5px 9px;")
        self.btn_save_setup.clicked.connect(self.save_setup_dialog)
        h_layout.addWidget(self.btn_save_setup)

        self.btn_load_setup = QPushButton("📂 불러오기")
        self.btn_load_setup.setStyleSheet("background-color: #334155; color: white; font-weight: bold; border: none; padding: 5px 9px;")
        self.btn_load_setup.clicked.connect(self.load_setup_dialog)
        h_layout.addWidget(self.btn_load_setup)

        btn_fit = QPushButton("🖥 화면 맞춤")
        btn_fit.setStyleSheet("background-color: #475569; color: white; font-weight: bold; border: none; padding: 5px 9px;")
        btn_fit.clicked.connect(lambda: fit_and_center_window(self, 0.93, 0.91))
        h_layout.addWidget(btn_fit)

        self.btn_all_stop = QPushButton("🛑 긴급 정지 (ALL STOP)")
        self.btn_all_stop.setStyleSheet(
            "background-color: #dc2626; color: white; font-weight: bold; border: none; padding: 6px 12px; border-radius: 4px;"
        )
        self.btn_all_stop.clicked.connect(self.emergency_stop)
        h_layout.addWidget(self.btn_all_stop)

        root.addWidget(header_bar)

        # ---------------------------------------------------------------------
        # 2. 8-MODEL QUICK SELECTOR TAB STRIP (Matches Tek QuickStart Category Strip)
        # ---------------------------------------------------------------------
        strip_row = QHBoxLayout()
        strip_row.setSpacing(4)
        model_tags = [
            ("2450", "01. 2450 (200V/1A)"),
            ("2460", "02. 2460 (7A High-I)"),
            ("2461", "03. 2461 (10A Pulse)"),
            ("2470", "04. 2470 (1100V HV)"),
            ("2600B", "05. 2600B (1~2CH)"),
            ("2601B-PULSE", "06. 2601B-PULSE (10µs)"),
            ("2606B", "07. 2606B (4CH 1U)"),
            ("2651A", "08. 2651A (50A 2kW)"),
        ]
        for m_key, m_label in model_tags:
            btn = QPushButton(m_label)
            btn.setCheckable(True)
            btn.clicked.connect(lambda checked, mk=m_key: self.cmb_model.setCurrentText(mk))
            self.model_buttons[m_key] = btn
            strip_row.addWidget(btn)
        strip_row.addStretch(1)
        root.addLayout(strip_row)

        # ---------------------------------------------------------------------
        # 3. SELECTED INSTRUMENT INFO & QUICK ACTION BANNER (Matches Image Row 3)
        # ---------------------------------------------------------------------
        info_card = QFrame()
        info_card.setStyleSheet(
            "QFrame { background-color: #ffffff; border: 1px solid #cbd5e1; border-radius: 6px; }"
        )
        ic_v = QVBoxLayout(info_card)
        ic_v.setContentsMargins(10, 6, 10, 6)
        ic_v.setSpacing(4)

        ic_top = QHBoxLayout()
        self.lbl_model_title = QLabel("⚡ Keithley 2450 SourceMeter")
        self.lbl_model_title.setFont(QFont("맑은 고딕", 11, QFont.Weight.Bold))
        self.lbl_model_title.setStyleSheet("color: #0b2545; border: none;")
        ic_top.addWidget(self.lbl_model_title)

        # Keep cmb_model visible/functional as dropdown selector for compatibility & quick switching
        self.cmb_model = QComboBox()
        self.cmb_model.addItems(self.registry.list_models())
        self.cmb_model.setFixedWidth(120)
        self.cmb_model.currentTextChanged.connect(self._on_model_changed)
        ic_top.addWidget(self.cmb_model)

        ic_top.addWidget(QLabel("세부모델:"))
        self.cmb_variant = QComboBox()
        self.cmb_variant.setFixedWidth(135)
        self.cmb_variant.currentTextChanged.connect(self._update_sync_warning_banner)
        ic_top.addWidget(self.cmb_variant)

        ic_top.addWidget(QLabel("장비 대수(1~10):"))
        self.spn_nodes = QSpinBox()
        self.spn_nodes.setRange(1, 10)
        self.spn_nodes.setValue(1)
        self.spn_nodes.setFixedWidth(55)
        self.spn_nodes.valueChanged.connect(self._on_nodes_changed)
        ic_top.addWidget(self.spn_nodes)

        ic_top.addWidget(QLabel("시뮬레이션 DUT:"))
        self.cmb_dut = QComboBox()
        for d in VirtualDUTType:
            self.cmb_dut.addItem(d.value)
        self.cmb_dut.setFixedWidth(130)
        self.cmb_dut.currentTextChanged.connect(self._on_dut_changed)
        ic_top.addWidget(self.cmb_dut)

        self.chk_interlock = QCheckBox("Interlock(>42V)")
        self.chk_interlock.setChecked(True)
        self.chk_interlock.toggled.connect(self._on_interlock_toggled)
        ic_top.addWidget(self.chk_interlock)

        ic_top.addStretch(1)

        self.btn_waveform_popup = QPushButton("🌊 파형 편집기")
        self.btn_waveform_popup.setToolTip("쉬운 파형 생성기 & 임의 파형 스텝 편집기 전용 창을 엽니다.")
        self.btn_waveform_popup.setStyleSheet(
            "background-color: #eef2ff; color: #3730a3; border: 1px solid #a5b4fc; font-weight: bold; padding: 5px 10px;"
        )
        self.btn_waveform_popup.clicked.connect(self.open_waveform_editor_dialog)
        ic_top.addWidget(self.btn_waveform_popup)

        self.btn_multich_popup = QPushButton("🔗 다중 장비·채널 설정")
        self.btn_multich_popup.setToolTip("1~10대 다중 장비 및 채널별 개별 파형/소스 분리 설정 창을 엽니다.")
        self.btn_multich_popup.setStyleSheet(
            "background-color: #f0fdf4; color: #166534; border: 1px solid #86efac; font-weight: bold; padding: 5px 10px;"
        )
        self.btn_multich_popup.clicked.connect(self.open_multi_channel_dialog)
        ic_top.addWidget(self.btn_multich_popup)

        self.btn_combining = QPushButton("⚡ 출력 결합")
        self.btn_combining.setStyleSheet("background-color: #e0f2fe; color: #0369a1; border: 1px solid #7dd3fc; font-weight: bold; padding: 5px 10px;")
        self.btn_combining.clicked.connect(self.open_combining_wizard)
        ic_top.addWidget(self.btn_combining)

        self.btn_manual = QPushButton("📖 PDF 매뉴얼")
        self.btn_manual.setStyleSheet("background-color: #1e3a5f; color: white; font-weight: bold; padding: 5px 10px;")
        self.btn_manual.clicked.connect(self.open_current_model_manual)
        ic_top.addWidget(self.btn_manual)

        self.btn_idn_check = QPushButton("🔍 *IDN? 확인")
        self.btn_idn_check.setStyleSheet("background-color: #0284c7; color: white; font-weight: bold; padding: 5px 10px;")
        self.btn_idn_check.clicked.connect(self._quick_idn_check)
        ic_top.addWidget(self.btn_idn_check)

        ic_v.addLayout(ic_top)

        self.lbl_spec_badge = QLabel()
        self.lbl_spec_badge.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.lbl_spec_badge.setStyleSheet("color: #475569; font-size: 8.5pt; border: none;")
        ic_v.addWidget(self.lbl_spec_badge)

        root.addWidget(info_card)

        # Warning & Live Progress Banners
        self.lbl_warning_banner = QLabel()
        self.lbl_warning_banner.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.lbl_warning_banner.setStyleSheet(
            "background-color: #fff7ed; color: #c2410c; border: 1px solid #fb923c; border-radius: 4px; padding: 5px 8px; font-weight: bold;"
        )
        self.lbl_warning_banner.setVisible(False)
        root.addWidget(self.lbl_warning_banner)

        self.frm_progress_banner = QFrame()
        self.frm_progress_banner.setStyleSheet(
            "QFrame { background-color: #dbeafe; border: 1px solid #2563eb; border-radius: 5px; }"
        )
        self.frm_progress_banner.setFixedHeight(34)
        pb_layout = QHBoxLayout(self.frm_progress_banner)
        pb_layout.setContentsMargins(10, 2, 10, 2)
        pb_layout.setSpacing(10)
        self.lbl_running_status = QLabel("⏳ 측정 진행 중...")
        self.lbl_running_status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.lbl_running_status.setStyleSheet("color: #1e3a8a; font-weight: bold; border: none;")
        pb_layout.addWidget(self.lbl_running_status, 2)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setFixedWidth(240)
        self.progress_bar.setFixedHeight(16)
        pb_layout.addWidget(self.progress_bar)
        self.lbl_elapsed_time = QLabel("경과 시간: 0.0 s")
        self.lbl_elapsed_time.setStyleSheet("color: #1d4ed8; font-family: Consolas; font-weight: bold; border: none;")
        pb_layout.addWidget(self.lbl_elapsed_time)
        self.frm_progress_banner.setVisible(False)
        root.addWidget(self.frm_progress_banner)

        # ---------------------------------------------------------------------
        # 4. MAIN VERTICAL SPLITTER (Top = 3-Column Workspace | Bottom = Talker/Listener Dock)
        # ---------------------------------------------------------------------
        main_vsplit = QSplitter(Qt.Orientation.Vertical)

        # === 3-COLUMN HORIZONTAL SPLITTER (Matches Tek QuickStart 3-Bordered Panels) ===
        col_splitter = QSplitter(Qt.Orientation.Horizontal)

        # =====================================================================
        # COLUMN 1 (BLUE BORDER): 1. 입력이 필요한 사항 (Required Input Parameters)
        # =====================================================================
        col1_group = QGroupBox("1. 입력이 필요한 사항 (Required Input Parameters)")
        col1_group.setObjectName("colInput")
        col1_outer = QVBoxLayout(col1_group)
        col1_outer.setContentsMargins(6, 10, 6, 6)

        col1_scroll = QScrollArea()
        col1_scroll.setWidgetResizable(True)
        col1_inner = QWidget()
        col1_inner.setStyleSheet("background-color: #ffffff;")
        c1_layout = QVBoxLayout(col1_inner)
        c1_layout.setContentsMargins(2, 2, 4, 2)
        c1_layout.setSpacing(6)

        # Quick Single DC Source & Measure Section
        lbl_sec0 = QLabel("0. 단일 DC 즉시 소스 인가 & 1회 측정")
        lbl_sec0.setStyleSheet("color: #0284c7; font-weight: bold; padding-top: 2px;")
        c1_layout.addWidget(lbl_sec0)

        qdc_row = QHBoxLayout()
        self.spn_quick_dc_level = QDoubleSpinBox()
        self.spn_quick_dc_level.setDecimals(4)
        self.spn_quick_dc_level.setRange(-10000.0, 10000.0)
        self.spn_quick_dc_level.setValue(1.0)
        self.spn_quick_dc_level.setSuffix(" V/A")
        self.spn_quick_dc_level.editingFinished.connect(
            lambda: self._validate_and_clamp_spinbox(self.spn_quick_dc_level, "source_level", "단일 DC 소스 레벨")
        )
        qdc_row.addWidget(self.spn_quick_dc_level, 1)

        self.btn_quick_dc = QPushButton("⚡ 즉시 1회 측정")
        self.btn_quick_dc.setStyleSheet("background-color: #16a34a; color: white; font-weight: bold; border: none; padding: 5px 10px;")
        self.btn_quick_dc.clicked.connect(self.execute_quick_single_dc_measurement)
        qdc_row.addWidget(self.btn_quick_dc)
        c1_layout.addLayout(qdc_row)

        self.lbl_live_dmm = QLabel("V: --- | I: --- | R: --- | P: ---")
        self.lbl_live_dmm.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.lbl_live_dmm.setStyleSheet(
            "background-color: #0f172a; color: #38bdf8; font-family: Consolas; font-size: 8.5pt; font-weight: bold; padding: 5px; border-radius: 4px;"
        )
        c1_layout.addWidget(self.lbl_live_dmm)

        # Section 1: Sweep & Waveform Settings
        lbl_sec1 = QLabel("1. 스위프 / 파형 & 반복 제어")
        lbl_sec1.setStyleSheet("color: #1d4ed8; font-weight: bold; padding-top: 4px;")
        c1_layout.addWidget(lbl_sec1)

        form_swp = QFormLayout()
        form_swp.setSpacing(5)

        self.cmb_sweep_type = QComboBox()
        for st in SweepType:
            self.cmb_sweep_type.addItem(st.value)
        self.cmb_sweep_type.currentTextChanged.connect(self._on_sweep_param_changed)
        form_swp.addRow("파형 모드:", self.cmb_sweep_type)

        self.cmb_source_func = QComboBox()
        self.cmb_source_func.addItems([SourceFunction.VOLTAGE.value, SourceFunction.CURRENT.value])
        self.cmb_source_func.currentTextChanged.connect(self._on_sweep_param_changed)
        form_swp.addRow("소스 기능:", self.cmb_source_func)

        self.spn_start = QDoubleSpinBox()
        self.spn_start.setDecimals(4)
        self.spn_start.setRange(-10000.0, 10000.0)
        self.spn_start.setValue(0.0)
        self.spn_start.editingFinished.connect(lambda: self._validate_and_clamp_spinbox(self.spn_start, "source_level", "시작 레벨"))
        self.spn_start.valueChanged.connect(self._on_sweep_param_changed)
        form_swp.addRow("시작 레벨 (V/A):", self.spn_start)

        self.spn_stop = QDoubleSpinBox()
        self.spn_stop.setDecimals(4)
        self.spn_stop.setRange(-10000.0, 10000.0)
        self.spn_stop.setValue(1.2)
        self.spn_stop.editingFinished.connect(lambda: self._validate_and_clamp_spinbox(self.spn_stop, "source_level", "종료 레벨"))
        self.spn_stop.valueChanged.connect(self._on_sweep_param_changed)
        form_swp.addRow("종료 레벨 (V/A):", self.spn_stop)

        self.spn_points = QSpinBox()
        self.spn_points.setRange(2, 1000000)
        self.spn_points.setValue(21)
        self.spn_points.valueChanged.connect(self._on_sweep_param_changed)
        form_swp.addRow("포인트 수 (pts):", self.spn_points)

        self.spn_delay = QDoubleSpinBox()
        self.spn_delay.setDecimals(5)
        self.spn_delay.setRange(0.00001, 60.0)
        self.spn_delay.setValue(0.005)
        self.spn_delay.valueChanged.connect(self._on_sweep_param_changed)
        form_swp.addRow("스텝 지연 (s):", self.spn_delay)

        self.cmb_repeat_mode = QComboBox()
        for rm in RepeatMode:
            self.cmb_repeat_mode.addItem(rm.value)
        self.cmb_repeat_mode.currentTextChanged.connect(self._on_sweep_param_changed)
        form_swp.addRow("반복 모드:", self.cmb_repeat_mode)

        self.spn_repeat_count = QSpinBox()
        self.spn_repeat_count.setRange(-1, 100000)
        self.spn_repeat_count.setValue(1)
        self.spn_repeat_count.valueChanged.connect(self._on_repeat_count_spin_changed)
        form_swp.addRow("반복 횟수(-1=무한):", self.spn_repeat_count)

        self.spn_compliance = QDoubleSpinBox()
        self.spn_compliance.setDecimals(5)
        self.spn_compliance.setRange(1e-7, 10000.0)
        self.spn_compliance.setValue(0.1)
        self.spn_compliance.editingFinished.connect(lambda: self._validate_and_clamp_spinbox(self.spn_compliance, "compliance", "Compliance"))
        self.spn_compliance.valueChanged.connect(self._on_sweep_param_changed)
        form_swp.addRow("Compliance (A/V):", self.spn_compliance)

        self.spn_nplc = QDoubleSpinBox()
        self.spn_nplc.setDecimals(3)
        self.spn_nplc.setRange(0.0001, 100.0)
        self.spn_nplc.setValue(0.1)
        self.spn_nplc.editingFinished.connect(lambda: self._validate_and_clamp_spinbox(self.spn_nplc, "nplc", "NPLC"))
        form_swp.addRow("적분 시간 (NPLC):", self.spn_nplc)

        c1_layout.addLayout(form_swp)

        self.chk_return_bias = QCheckBox("종료 시 Bias(0V) 복귀 (해제 시 Hold)")
        self.chk_return_bias.setChecked(True)
        self.chk_return_bias.toggled.connect(self._on_sweep_param_changed)
        c1_layout.addWidget(self.chk_return_bias)

        # Section 2: Precision & Safety
        lbl_sec2 = QLabel("2. 정밀도 & 안전 보호 (Safety)")
        lbl_sec2.setStyleSheet("color: #1d4ed8; font-weight: bold; padding-top: 4px;")
        c1_layout.addWidget(lbl_sec2)

        form_saf = QFormLayout()
        form_saf.setSpacing(5)
        self.cmb_sense = QComboBox()
        self.cmb_sense.addItems([SenseMode.WIRE_2.value, SenseMode.WIRE_4.value])
        form_saf.addRow("배선 모드:", self.cmb_sense)

        self.cmb_terminals = QComboBox()
        self.cmb_terminals.addItems([TerminalLocation.FRONT.value, TerminalLocation.REAR.value])
        self.cmb_terminals.setToolTip("장비 전면(FRONT) 바나나잭 또는 후면(REAR) 트라이액셜/스크류 출력 단자 선택")
        form_saf.addRow("출력 단자:", self.cmb_terminals)

        self.cmb_meas_range = QComboBox()
        self.cmb_meas_range.addItem("AUTO (자동 레인지)", None)
        for rng_v, rng_lbl in [
            (1e-5, "10 µA / 10µV (고정)"),
            (1e-4, "100 µA / 100µV (고정)"),
            (1e-3, "1 mA / 1mV (고정)"),
            (1e-2, "10 mA / 10mV (고정)"),
            (0.1, "100 mA / 100mV (고정)"),
            (1.0, "1 A / 1V (고정)"),
            (10.0, "10 A / 10V (고정)"),
        ]:
            self.cmb_meas_range.addItem(rng_lbl, rng_v)
        self.cmb_meas_range.setToolTip("AUTO(자동) 또는 고정 측정 레인지 선택 — 오토레인지 스파이크 방지 및 고속 측정 시 고정 레인지 권장")
        form_saf.addRow("측정 레인지:", self.cmb_meas_range)

        self.cmb_offmode = QComboBox()
        for om in OutputOffMode:
            self.cmb_offmode.addItem(om.value)
        form_saf.addRow("Output-Off:", self.cmb_offmode)

        self.cmb_auto_zero = QComboBox()
        self.cmb_auto_zero.addItems(["ON", "ONCE", "OFF"])
        self.cmb_auto_zero.setCurrentText("ON")
        self.cmb_auto_zero.setToolTip(
            "AutoZero (자동 영점 보정)\n"
            "• ON: 매 포인트마다 영점 보정 (최고 정밀도, 스텝당 ~15~30ms 추가)\n"
            "• ONCE: 측정 시작 전 1회만 보정 (속도와 정밀도 균형 추천)\n"
            "• OFF: 영점 보정 비활성화 (최고 속도, 장시간 측정 시 미세 드리프트 가능)"
        )
        form_saf.addRow("AutoZero:", self.cmb_auto_zero)
        c1_layout.addLayout(form_saf)

        self.chk_source_readback = QCheckBox("Source Readback (실제 인가값 재측정)")
        self.chk_source_readback.setChecked(False)
        self.chk_source_readback.setToolTip(
            "Source Readback (24xx 시리즈 전용 · 기본값 OFF)\n"
            "• 해제(OFF, 기본): 설정한 목표 레벨을 그대로 기록하여 고속 스위프 안정성 확보\n"
            "• 체크(ON, 정밀): 매 스텝마다 실제 출력값을 재측정 (단일 ADC 모델에서 약 2배 느려질 수 있음, >1% 오차 자동 진단 연계)"
        )
        c1_layout.addWidget(self.chk_source_readback)

        self.chk_high_cap = QCheckBox("High-Capacitance 모드 (고용량 부하 안정화)")
        self.chk_high_cap.setChecked(False)
        self.chk_high_cap.setToolTip("커패시터/배터리 등 용량성 부하 연결 시 출력 발진을 방지합니다.")
        c1_layout.addWidget(self.chk_high_cap)

        self.chk_contact = QCheckBox("4-Wire Contact Check 사전 검사")
        c1_layout.addWidget(self.chk_contact)

        self.chk_neg_zero = QCheckBox("26xx 음수 전환 지연(-1e-12) 자동 보정")
        self.chk_neg_zero.setChecked(True)
        c1_layout.addWidget(self.chk_neg_zero)

        # Section 3: Persistent Export Directory
        lbl_sec3 = QLabel("3. 파일 저장 위치 (자동 기억)")
        lbl_sec3.setStyleSheet("color: #1d4ed8; font-weight: bold; padding-top: 4px;")
        c1_layout.addWidget(lbl_sec3)

        dir_row = QHBoxLayout()
        self.txt_export_dir = QLineEdit(str(self.exporter.output_dir))
        self.txt_export_dir.setReadOnly(True)
        self.txt_export_dir.setStyleSheet("background-color: #f8fafc; font-size: 8pt;")
        dir_row.addWidget(self.txt_export_dir, 1)

        self.btn_choose_export_dir = QPushButton("📁 변경")
        self.btn_choose_export_dir.setStyleSheet("background-color: #e0f2fe; color: #0369a1; font-weight: bold;")
        self.btn_choose_export_dir.clicked.connect(self.choose_export_directory)
        dir_row.addWidget(self.btn_choose_export_dir)
        c1_layout.addLayout(dir_row)

        self.chk_auto_export = QCheckBox("측정 완료 시 자동 CSV 백업 (Auto-Export)")
        c1_layout.addWidget(self.chk_auto_export)

        c1_layout.addStretch(1)
        col1_scroll.setWidget(col1_inner)
        col1_outer.addWidget(col1_scroll)

        btn_reset_defaults = QPushButton("↺ 입력값 기본값(Default) 초기화")
        btn_reset_defaults.clicked.connect(self._reset_input_defaults)
        col1_outer.addWidget(btn_reset_defaults)

        col_splitter.addWidget(col1_group)

        # =====================================================================
        # COLUMN 2 (GREEN BORDER): 2. 측정이 가능한 부분 & 실시간 분석 지표 (KPI Cards)
        # =====================================================================
        col2_group = QGroupBox("2. 실시간 측정 & 자동 특성 분석 (Live KPI Readouts)")
        col2_group.setObjectName("colMetrics")
        col2_outer = QVBoxLayout(col2_group)
        col2_outer.setContentsMargins(6, 10, 6, 6)

        col2_scroll = QScrollArea()
        col2_scroll.setWidgetResizable(True)
        col2_inner = QWidget()
        col2_inner.setStyleSheet("background-color: #ffffff;")
        c2_layout = QVBoxLayout(col2_inner)
        c2_layout.setContentsMargins(2, 2, 4, 2)
        c2_layout.setSpacing(6)

        self.kpi_voltage = KPIReadoutCard("인가/측정 전압 범위 (Voltage)", "Min ~ Max Voltage (V)", "--- V", "#0369a1")
        self.kpi_current = KPIReadoutCard("측정 전류 최대/평균 (Current)", "Peak & Mean Current (A)", "--- A", "#047857")
        self.kpi_power = KPIReadoutCard("최대 소비 전력 (Peak Power)", "Max |V × I| Dissipation (W)", "--- W", "#7c3aed")
        self.kpi_dvdi = KPIReadoutCard("미분 저항 (dV/dI Median)", "Differential Resistance (Ω)", "--- Ω", "#0f766e")
        self.kpi_vth = KPIReadoutCard("문턱 전압 (Threshold Vth)", "Forward Conduction Knee (V)", "--- V", "#b45309")
        self.kpi_ileak = KPIReadoutCard("누설 전류 (Leakage Ileak)", "Low-Bias Leakage Current (A)", "--- A", "#0369a1")
        self.kpi_pulse = KPIReadoutCard("펄스 과도응답 (Rise / Overshoot)", "10-90% Rise Time & Overshoot (%)", "---", "#be185d")
        self.kpi_compliance = KPIReadoutCard("Compliance / Limit 상태", "Compliance Hits & Pass/Fail", "NORMAL (0)", "#15803d")

        for card in (
            self.kpi_voltage,
            self.kpi_current,
            self.kpi_power,
            self.kpi_dvdi,
            self.kpi_vth,
            self.kpi_ileak,
            self.kpi_pulse,
            self.kpi_compliance,
        ):
            c2_layout.addWidget(card)
        c2_layout.addStretch(1)

        col2_scroll.setWidget(col2_inner)
        col2_outer.addWidget(col2_scroll)

        # Quick Export Action Row at Bottom of Column 2
        exp_mini = QHBoxLayout()
        exp_mini.setSpacing(3)
        btn_csv_q = QPushButton("📄 CSV")
        btn_csv_q.clicked.connect(self.export_csv)
        btn_pq_q = QPushButton("⚡ Parquet/DB")
        btn_pq_q.clicked.connect(self.export_parquet)
        btn_img_q = QPushButton("🖼 PNG/SVG")
        btn_img_q.clicked.connect(self.export_plot_image)
        btn_hist_q = QPushButton("📚 이력비교")
        btn_hist_q.clicked.connect(self.export_history_comparison)
        for b in (btn_csv_q, btn_pq_q, btn_img_q, btn_hist_q):
            b.setStyleSheet("padding: 4px 4px; font-size: 8.3pt;")
            exp_mini.addWidget(b)
        col2_outer.addLayout(exp_mini)

        col_splitter.addWidget(col2_group)

        # =====================================================================
        # COLUMN 3 (PURPLE BORDER): 3. 측정 제어 / 실시간 그래프 표현 / 데이터 테이블 (HERO PANEL)
        # =====================================================================
        col3_group = QGroupBox("3. 측정 제어 / 실시간 고대비 그래프 표현 / 측정 데이터 테이블 (Excel)")
        col3_group.setObjectName("colGraph")
        c3_layout = QVBoxLayout(col3_group)
        c3_layout.setContentsMargins(8, 10, 8, 6)
        c3_layout.setSpacing(6)

        # Row 1: Big Colored Action Buttons (Matches Tek QuickStart Image!)
        action_row = QHBoxLayout()
        action_row.setSpacing(6)

        self.btn_run = QPushButton("▶ 측정 시작 (Run)")
        self.btn_run.setStyleSheet(
            "background-color: #16a34a; color: white; font-weight: bold; font-size: 10pt; border: none; padding: 7px 16px; border-radius: 5px;"
        )
        self.btn_run.clicked.connect(self.execute_measurement)
        action_row.addWidget(self.btn_run, 2)

        btn_stop_smu = QPushButton("■ 완전 정지 (OFF)")
        btn_stop_smu.setStyleSheet(
            "background-color: #2563eb; color: white; font-weight: bold; font-size: 9.5pt; border: none; padding: 7px 12px; border-radius: 5px;"
        )
        btn_stop_smu.clicked.connect(self.emergency_stop)
        action_row.addWidget(btn_stop_smu, 1)

        self.btn_clear_data = QPushButton("🧹 초기화")
        self.btn_clear_data.setStyleSheet(
            "background-color: #ffffff; color: #0f172a; font-weight: bold; border: 1px solid #94a3b8; padding: 7px 12px; border-radius: 5px;"
        )
        self.btn_clear_data.clicked.connect(self.clear_graph_and_table_data)
        action_row.addWidget(self.btn_clear_data, 1)

        btn_xlsx = QPushButton("📥 Excel 저장 (.xlsx)")
        btn_xlsx.setStyleSheet(
            "background-color: #0f766e; color: white; font-weight: bold; font-size: 9.5pt; border: none; padding: 7px 14px; border-radius: 5px;"
        )
        btn_xlsx.clicked.connect(self.export_excel)
        action_row.addWidget(btn_xlsx, 1)

        c3_layout.addLayout(action_row)

        # Row 2: Status Line + Graph Mode + Detach Pop-out Button
        st_row = QHBoxLayout()
        self.lbl_state_pill = QLabel("● 상태: 대기 중 (READY)")
        self.lbl_state_pill.setStyleSheet("color: #0f766e; font-weight: bold; font-size: 9pt;")
        st_row.addWidget(self.lbl_state_pill)

        self.lbl_sample_count = QLabel("누적 샘플: 0 pts")
        self.lbl_sample_count.setStyleSheet("color: #334155; font-weight: bold; padding-left: 8px;")
        st_row.addWidget(self.lbl_sample_count)

        self.btn_time_guide = QPushButton("⏱ 예상 시간 공식")
        self.btn_time_guide.setToolTip("현재 입력된 설정값 기준으로 측정 완료 시간(Elapsed)이 어떻게 계산되는지 공식과 함께 쉽게 설명합니다.")
        self.btn_time_guide.setStyleSheet(
            "background-color: #eef2ff; color: #3730a3; border: 1px solid #a5b4fc; font-weight: bold; padding: 3px 8px; border-radius: 4px;"
        )
        self.btn_time_guide.clicked.connect(self.open_measurement_time_guide_dialog)
        st_row.addWidget(self.btn_time_guide)

        st_row.addStretch(1)

        st_row.addWidget(QLabel("그래프 모드:"))
        self.cmb_plot_view = QComboBox()
        self.cmb_plot_view.addItems([
            "I-V 특성 곡선 (Voltage vs Current)",
            "시간축 파형 (Time vs Current & Voltage)",
        ])
        self.cmb_plot_view.currentTextChanged.connect(self._refresh_plot)
        st_row.addWidget(self.cmb_plot_view)

        self.btn_detach_graph = QPushButton("🗗 새 창 분리 (Pop-out)")
        self.btn_detach_graph.setStyleSheet(
            "background-color: #0e7490; color: white; font-weight: bold; border: none; padding: 4px 10px; border-radius: 4px;"
        )
        self.btn_detach_graph.clicked.connect(self.open_detached_graph_window)
        st_row.addWidget(self.btn_detach_graph)
        c3_layout.addLayout(st_row)

        # Row 2b: Compact 1-Line Auto-Diagnosis Coach Banner (click to open full 15-rule popup + Auto-Fix)
        self.frm_diag_banner = QFrame()
        self.frm_diag_banner.setStyleSheet(
            "QFrame { background-color: #f8fafc; border: 1px solid #cbd5e1; border-radius: 5px; }"
        )
        diag_row = QHBoxLayout(self.frm_diag_banner)
        diag_row.setContentsMargins(8, 3, 8, 3)
        diag_row.setSpacing(8)
        self.lbl_diag_summary = QLabel("🩺 자동 측정 코치 대기 중 — 측정 완료 시 15대 셋업·품질 항목을 자동 진단합니다.")
        self.lbl_diag_summary.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.lbl_diag_summary.setStyleSheet("color: #475569; font-weight: bold; font-size: 8.6pt; border: none;")
        diag_row.addWidget(self.lbl_diag_summary, 1)

        self.btn_open_diag_popup = QPushButton("🩺 진단 상세 보기 · 자동 보정")
        self.btn_open_diag_popup.setStyleSheet(
            "background-color: #e0f2fe; color: #0369a1; border: 1px solid #7dd3fc; font-weight: bold; padding: 3px 10px; border-radius: 4px;"
        )
        self.btn_open_diag_popup.clicked.connect(self.open_diagnosis_popup)
        diag_row.addWidget(self.btn_open_diag_popup)
        c3_layout.addWidget(self.frm_diag_banner)

        # Row 3: 5 Feature Tabs inside Column 3 (Main Graph+Table Hero Tab is Tab 0)
        self.tabs = QTabWidget()

        # --- TAB 1 (HERO): Dark Oscilloscope-Style High-Contrast Graph + Live Measurement Data Table ---
        graph_tab = QWidget()
        g_layout = QVBoxLayout(graph_tab)
        g_layout.setContentsMargins(6, 6, 6, 6)
        g_layout.setSpacing(5)

        # Graph Tools Strip (Cursor Toggle, Log Scale, Cursor Readout)
        g_tools = QHBoxLayout()
        self.chk_dual_cursors = QCheckBox("➕ 듀얼 커서 (ΔX, ΔY, 1/R)")
        self.chk_dual_cursors.setChecked(True)
        self.chk_dual_cursors.toggled.connect(self._refresh_plot)
        g_tools.addWidget(self.chk_dual_cursors)

        self.chk_log_y = QCheckBox("📈 Y축 Log(|I|) 스케일")
        self.chk_log_y.toggled.connect(self._refresh_plot)
        g_tools.addWidget(self.chk_log_y)

        btn_autoscale = QPushButton("🔍 자동 스케일")
        btn_autoscale.clicked.connect(lambda: self.plot_iv.autoRange())
        g_tools.addWidget(btn_autoscale)

        self.lbl_cursor_readout = QLabel("커서 대기 중 (측정 후 그래프 위 노란/초록 점선을 드래그하세요)")
        self.lbl_cursor_readout.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.lbl_cursor_readout.setStyleSheet(
            "background-color: #eff6ff; color: #1e3a8a; border: 1px solid #bfdbfe; border-radius: 4px; "
            "padding: 3px 8px; font-family: Consolas, '맑은 고딕'; font-size: 8.3pt; font-weight: bold;"
        )
        g_tools.addWidget(self.lbl_cursor_readout, 1)
        g_layout.addLayout(g_tools)

        # Vertical Split inside Tab 1: Top = Dark Neon Plot (62%) | Bottom = Clean Live Data Table (38%)
        # (Matches the exact top-graph + bottom-table aesthetic of the Tek QuickStart image!)
        tab1_vsplit = QSplitter(Qt.Orientation.Vertical)

        pg.setConfigOptions(antialias=True)
        self.plot_iv = pg.PlotWidget()
        self.plot_iv.setBackground("#0b132b")
        self.plot_iv.setTitle(
            "<span style='color:#e2e8f0; font-size:10pt; font-weight:bold;'>SMU 실시간 I-V / 파형 특성 그래프 (High-Contrast Dark View)</span>"
        )
        self.plot_iv.getAxis("bottom").setPen(pg.mkPen("#94a3b8"))
        self.plot_iv.getAxis("bottom").setTextPen(pg.mkPen("#cbd5e1"))
        self.plot_iv.getAxis("left").setPen(pg.mkPen("#94a3b8"))
        self.plot_iv.getAxis("left").setTextPen(pg.mkPen("#cbd5e1"))
        self.plot_iv.setLabel("bottom", "<span style='color:#cbd5e1;'>Voltage</span>", units="V")
        self.plot_iv.setLabel("left", "<span style='color:#cbd5e1;'>Current</span>", units="A")
        self.plot_iv.showGrid(x=True, y=True, alpha=0.25)
        self.plot_iv.addLegend(labelTextColor="#f8fafc", brush=pg.mkBrush(15, 23, 42, 180))

        self.cursor_line1 = pg.InfiniteLine(pos=0.2, angle=90, movable=True, pen=pg.mkPen("#facc15", width=1.8, style=Qt.PenStyle.DashLine))
        self.cursor_line2 = pg.InfiniteLine(pos=0.9, angle=90, movable=True, pen=pg.mkPen("#4ade80", width=1.8, style=Qt.PenStyle.DashLine))
        self.cursor_line1.sigPositionChanged.connect(self._update_cursor_readout)
        self.cursor_line2.sigPositionChanged.connect(self._update_cursor_readout)
        tab1_vsplit.addWidget(self.plot_iv)

        # Bottom Live Data Table (matching Tek QuickStart clean spreadsheet style)
        self.tbl_live_data = QTableWidget(0, 7)
        self.tbl_live_data.setAlternatingRowColors(True)
        self.tbl_live_data.setHorizontalHeaderLabels([
            "Step", "Node.CH", "Elapsed (s)", "측정 전압 V (V)", "측정 전류 I (A)", "측정 저항 R (Ω)", "측정 전력 P (W)"
        ])
        self.tbl_live_data.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        tab1_vsplit.addWidget(self.tbl_live_data)
        tab1_vsplit.setSizes([380, 210])

        g_layout.addWidget(tab1_vsplit, 1)

        self.lbl_metrics = QLabel("측정 대기 중 — [▶ 측정 시작] 또는 좌측 [⚡ 즉시 1회 측정]을 누르면 다크 고대비 그래프와 하단 데이터 표에 즉시 표시됩니다.")
        self.lbl_metrics.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.lbl_metrics.setFixedHeight(32)
        self.lbl_metrics.setStyleSheet(
            "background-color: #f8fafc; border: 1px solid #cbd5e1; border-radius: 4px; padding: 4px 8px; "
            "font-family: Consolas, '맑은 고딕'; font-size: 8.3pt; color: #1e293b;"
        )
        g_layout.addWidget(self.lbl_metrics)
        self.tabs.addTab(graph_tab, "📈 실시간 측정 그래프 · 데이터 테이블")

        # --- TAB 2: Phase 3 High-Speed Pulse, 1 MS/s Digitizer & SOA Envelope ---
        p3_tab = QWidget()
        p3_layout = QHBoxLayout(p3_tab)
        p3_left = QWidget()
        p3_left_layout = QVBoxLayout(p3_left)
        grp_p3 = QGroupBox("고속 펄스 · 1 MS/s 디지타이저 & 열/SOA 설정")
        p3_form = QFormLayout(grp_p3)

        self.spn_pulse_width_us = QDoubleSpinBox()
        self.spn_pulse_width_us.setDecimals(1)
        self.spn_pulse_width_us.setRange(1.0, 1000000.0)
        self.spn_pulse_width_us.setValue(200.0)
        self.spn_pulse_width_us.editingFinished.connect(
            lambda: self._validate_and_clamp_spinbox(self.spn_pulse_width_us, "pulse_width_us", "펄스 폭(µs)")
        )
        self.spn_pulse_width_us.valueChanged.connect(self.refresh_soa_chart)
        p3_form.addRow("펄스 폭 (µs):", self.spn_pulse_width_us)

        self.spn_pulse_period_ms = QDoubleSpinBox()
        self.spn_pulse_period_ms.setDecimals(2)
        self.spn_pulse_period_ms.setRange(0.05, 5000.0)
        self.spn_pulse_period_ms.setValue(10.0)
        self.spn_pulse_period_ms.valueChanged.connect(self.refresh_soa_chart)
        p3_form.addRow("펄스 주기 (ms):", self.spn_pulse_period_ms)

        self.spn_pulse_count = QSpinBox()
        self.spn_pulse_count.setRange(1, 50000)
        self.spn_pulse_count.setValue(1)
        p3_form.addRow("펄스 개수:", self.spn_pulse_count)

        self.chk_digitizer = QCheckBox("1 MS/s 디지타이저 캡처")
        self.chk_digitizer.setChecked(True)
        p3_form.addRow(self.chk_digitizer)

        self.spn_sample_rate = QSpinBox()
        self.spn_sample_rate.setRange(1000, 1000000)
        self.spn_sample_rate.setSingleStep(100000)
        self.spn_sample_rate.setValue(1000000)
        p3_form.addRow("샘플레이트 (S/s):", self.spn_sample_rate)

        self.spn_inductance_uh = QDoubleSpinBox()
        self.spn_inductance_uh.setDecimals(2)
        self.spn_inductance_uh.setRange(0.0, 3.0)
        self.spn_inductance_uh.setValue(0.5)
        p3_form.addRow("2601B 인덕턴스 (µH):", self.spn_inductance_uh)

        self.chk_fast_adc = QCheckBox("2651A Fast ADC (1µs)")
        self.chk_fast_adc.setChecked(True)
        p3_form.addRow(self.chk_fast_adc)

        self.chk_sink_mode = QCheckBox("Sink 모드 (2·4사분면)")
        self.chk_sink_mode.toggled.connect(self.refresh_soa_chart)
        p3_form.addRow(self.chk_sink_mode)

        self.spn_ambient_temp = QDoubleSpinBox()
        self.spn_ambient_temp.setDecimals(1)
        self.spn_ambient_temp.setRange(15.0, 55.0)
        self.spn_ambient_temp.setValue(25.0)
        self.spn_ambient_temp.valueChanged.connect(self.refresh_soa_chart)
        p3_form.addRow("주변 온도 (°C):", self.spn_ambient_temp)
        p3_left_layout.addWidget(grp_p3)

        btn_run_pulse = QPushButton("⚡ 고속 펄스 & 디지타이저 측정 실행")
        btn_run_pulse.setStyleSheet("background-color: #2563eb; color: white; font-weight: bold; padding: 7px;")
        btn_run_pulse.clicked.connect(self.execute_high_speed_pulse_measurement)
        p3_left_layout.addWidget(btn_run_pulse)

        self.btn_acal = QPushButton("🔧 2461 원격 자가교정 (ACAL)")
        self.btn_acal.setStyleSheet("background-color: #0e7490; color: white; font-weight: bold; padding: 7px;")
        self.btn_acal.clicked.connect(self.run_remote_2461_acal)
        p3_left_layout.addWidget(self.btn_acal)

        self.lbl_soa_status = QLabel()
        self.lbl_soa_status.setWordWrap(True)
        self.lbl_soa_status.setStyleSheet("background-color: #eff6ff; color: #0b1f3a; padding: 6px; border-radius: 4px; font-weight: bold;")
        p3_left_layout.addWidget(self.lbl_soa_status)
        p3_left_layout.addStretch(1)
        p3_layout.addWidget(p3_left, 2)

        self.plot_soa = pg.PlotWidget()
        self.plot_soa.setBackground("#0b132b")
        self.plot_soa.setTitle("<span style='color:#e2e8f0;'>Safe Operating Area (SOA) Boundary</span>")
        self.plot_soa.setLabel("bottom", "<span style='color:#cbd5e1;'>Voltage |V|</span>", units="V")
        self.plot_soa.setLabel("left", "<span style='color:#cbd5e1;'>Current |I|</span>", units="A")
        self.plot_soa.showGrid(x=True, y=True, alpha=0.25)
        self.plot_soa.addLegend(labelTextColor="#f8fafc")
        p3_layout.addWidget(self.plot_soa, 3)
        self.tabs.addTab(p3_tab, "⚡ 고속 펄스 · SOA")

        # --- Core 3 Tabs Only (Optional Waveform Editor & Multi-Channel Setup are now dedicated popup dialogs!) ---
        # Hidden compatibility widgets kept for backward compatibility with headless test scripts
        self.cmb_easy_shape = QComboBox()
        for sh in EasyWaveformShape:
            self.cmb_easy_shape.addItem(sh.value)
        self.spn_easy_high = QDoubleSpinBox()
        self.spn_easy_high.setRange(-1100.0, 1100.0)
        self.spn_easy_high.setValue(3.3)
        self.spn_easy_low = QDoubleSpinBox()
        self.spn_easy_low.setRange(-1100.0, 1100.0)
        self.spn_easy_low.setValue(0.0)
        self.spn_easy_dur = QDoubleSpinBox()
        self.spn_easy_dur.setRange(0.00001, 60.0)
        self.spn_easy_dur.setValue(0.010)
        self.spn_easy_repeat = QSpinBox()
        self.spn_easy_repeat.setRange(-1, 100000)
        self.spn_easy_repeat.setValue(10)
        self.tbl_waveform = QTableWidget(0, 5)
        self.tbl_channels = QTableWidget(0, 9)

        # --- TAB 3 (Core): Full Reading Buffer Table & Run History ---
        tbl_tab = QWidget()
        t_layout = QVBoxLayout(tbl_tab)
        self.table = QTableWidget(0, 8)
        self.table.setAlternatingRowColors(True)
        self.table.setHorizontalHeaderLabels([
            "Idx", "Node.CH", "Time (s)", "Voltage (V)", "Current (A)", "Resistance (Ω)", "Power (W)", "Status"
        ])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        t_layout.addWidget(self.table)
        self.tabs.addTab(tbl_tab, "📋 전체 버퍼 데이터 · 이력")

        c3_layout.addWidget(self.tabs, 1)
        col_splitter.addWidget(col3_group)

        # Compact-fit 3-Column Width Allocation:
        # Ensure Column 1 (Input: 315px) & Column 2 (Live KPI: 290px) display all labels/buttons cleanly without clipping,
        # while giving Column 3 (Graph & Data Table: 1005px) maximum workspace.
        col1_group.setMinimumWidth(295)
        col2_group.setMinimumWidth(275)
        col_splitter.setStretchFactor(0, 0)
        col_splitter.setStretchFactor(1, 0)
        col_splitter.setStretchFactor(2, 1)
        col_splitter.setSizes([315, 290, 1005])
        main_vsplit.addWidget(col_splitter)

        # ---------------------------------------------------------------------
        # 5. BOTTOM TALKER/LISTENER INTERACTIVE COMMAND & LOG TABLE DOCK (Matches Image Bottom!)
        # ---------------------------------------------------------------------
        dock_frame = QFrame()
        dock_frame.setStyleSheet(
            "QFrame { background-color: #ffffff; border: 1px solid #cbd5e1; border-radius: 6px; }"
        )
        dock_v = QVBoxLayout(dock_frame)
        dock_v.setContentsMargins(8, 5, 8, 5)
        dock_v.setSpacing(4)

        cmd_bar = QHBoxLayout()
        lbl_tl = QLabel("💬 TekVISA Talker/Listener 명령 전송 & 통신 로그:")
        lbl_tl.setStyleSheet("font-weight: bold; color: #0f172a; border: none;")
        cmd_bar.addWidget(lbl_tl)

        self.txt_manual_cmd = QLineEdit("*IDN?")
        self.txt_manual_cmd.setPlaceholderText("직접 전송할 TSP 또는 SCPI 명령 입력 (예: *IDN?, *LANG?, print(smu.measure.read()))")
        cmd_bar.addWidget(self.txt_manual_cmd, 1)

        btn_write = QPushButton("📤 Write")
        btn_write.clicked.connect(self._send_manual_write)
        cmd_bar.addWidget(btn_write)

        btn_query = QPushButton("🔍 Query (송수신)")
        btn_query.setStyleSheet("background-color: #0e7490; color: white; font-weight: bold; border: none; padding: 4px 10px;")
        btn_query.clicked.connect(self._send_manual_query)
        cmd_bar.addWidget(btn_query)

        btn_clear_log = QPushButton("🗑 로그 지우기")
        btn_clear_log.clicked.connect(self._clear_spy_table)
        cmd_bar.addWidget(btn_clear_log)

        self.btn_toggle_log = QPushButton("▼ 로그 표 접기")
        self.btn_toggle_log.clicked.connect(self._toggle_spy_table)
        cmd_bar.addWidget(self.btn_toggle_log)
        dock_v.addLayout(cmd_bar)

        self.tbl_spy = QTableWidget(0, 5)
        self.tbl_spy.setAlternatingRowColors(True)
        self.tbl_spy.setHorizontalHeaderLabels([
            "시각 (Time)", "구분 (Dir)", "리소스 (Resource)", "전송 명령 (Command)", "수신 응답 & 지연시간 (Response & Latency)"
        ])
        self.tbl_spy.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.tbl_spy.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.tbl_spy.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.tbl_spy.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.tbl_spy.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        dock_v.addWidget(self.tbl_spy, 1)

        # Hidden alias for backward compatibility if any script inspects txt_spy
        self.txt_spy = QPlainTextEdit()
        self.txt_spy.setVisible(False)

        main_vsplit.addWidget(dock_frame)
        main_vsplit.setStretchFactor(0, 1)
        main_vsplit.setStretchFactor(1, 0)
        main_vsplit.setSizes([745, 115])
        root.addWidget(main_vsplit, 1)

        # Bottom Status Bar
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("준비 완료 — SMU 모델 및 연결 상태를 확인한 뒤 [▶ 측정 시작]을 눌러주세요.")

    # =========================================================================
    # Talker/Listener & Top Bar Helpers
    # =========================================================================

    def _on_spy_entry(self, entry: SpyEntry) -> None:
        self.txt_spy.appendPlainText(entry.format_line())
        row = self.tbl_spy.rowCount()
        if row > 300:
            self.tbl_spy.removeRow(0)
            row = self.tbl_spy.rowCount()
        self.tbl_spy.insertRow(row)

        ts_str = entry.timestamp.strftime("%H:%M:%S.%f")[:-3]
        dir_map = {"TX": "WRITE", "RX": "READ", "ERR": "ERROR", "SYS": "SYSTEM"}
        dir_lbl = dir_map.get(entry.direction, entry.direction)

        c_ts = QTableWidgetItem(ts_str)
        c_dir = QTableWidgetItem(dir_lbl)
        c_dir.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        if entry.direction == "ERR":
            c_dir.setForeground(QColor("#dc2626"))
        elif entry.direction == "RX":
            c_dir.setForeground(QColor("#047857"))
        else:
            c_dir.setForeground(QColor("#1d4ed8"))

        c_res = QTableWidgetItem(entry.target)
        c_cmd = QTableWidgetItem(entry.payload if entry.direction in ("TX", "SYS") else "")
        resp_text = (
            f"{entry.payload} ({entry.elapsed_ms:.2f} ms)"
            if entry.direction in ("RX", "ERR")
            else (f"VI_SUCCESS ({entry.elapsed_ms:.2f} ms)" if entry.elapsed_ms > 0 else "OK")
        )
        if entry.guide_ko:
            resp_text += f" | {entry.guide_ko}"
        c_resp = QTableWidgetItem(resp_text)

        for col_i, item in enumerate((c_ts, c_dir, c_res, c_cmd, c_resp)):
            self.tbl_spy.setItem(row, col_i, item)
        self.tbl_spy.scrollToBottom()

    def _send_manual_write(self) -> None:
        cmd = self.txt_manual_cmd.text().strip()
        if not cmd:
            return
        try:
            self.driver.transport.write(cmd)
            self.status_bar.showMessage(f"명령 전송 완료: {cmd}")
        except Exception as exc:
            self.spy.record("ERR", self.driver.profile.model, str(exc))

    def _send_manual_query(self) -> None:
        cmd = self.txt_manual_cmd.text().strip()
        if not cmd:
            return
        try:
            resp = self.driver.transport.query(cmd)
            self.status_bar.showMessage(f"응답 수신: {resp}")
        except Exception as exc:
            self.spy.record("ERR", self.driver.profile.model, str(exc))

    def _clear_spy_table(self) -> None:
        self.tbl_spy.setRowCount(0)
        self.txt_spy.clear()
        self.spy.clear()

    def _toggle_spy_table(self) -> None:
        self._log_visible = not self._log_visible
        self.tbl_spy.setVisible(self._log_visible)
        self.btn_toggle_log.setText("▼ 로그 표 접기" if self._log_visible else "▲ 로그 표 펼치기")

    def _quick_idn_check(self) -> None:
        en_chs = [c for c in self.logical_channels if c.enabled]
        if self.multi_sync_mode == MultiUnitSyncMode.MULTI_VISA_SW and len(en_chs) >= 2:
            lines: list[str] = ["[방식 B: USB/LAN 개별 다중 연결 — 전체 장비 *IDN? 응답]\n"]
            for lch in en_chs:
                m_name = lch.model_name or self.driver.profile.model
                res_addr = lch.visa_resource or f"SIM::{m_name}::INSTR"
                try:
                    if lch.node_id == 1 and m_name == self.driver.profile.model and res_addr == self.active_resource_name:
                        idn = self.driver.identify().strip()
                    else:
                        tr = self.visa_mgr.open_transport_session(res_addr, model=m_name)
                        sub_drv = create_smu_driver(m_name, transport=tr, spy=self.spy, registry=self.registry)
                        try:
                            idn = sub_drv.identify().strip()
                        finally:
                            tr.close()
                    lines.append(f"• Node {lch.node_id} ({m_name}) [{res_addr}]\n   ✅ {idn}")
                except Exception as exc:
                    lines.append(f"• Node {lch.node_id} ({m_name}) [{res_addr}]\n   ❌ 연결 실패: {exc}")
            QMessageBox.information(self, "다중 장비 연결 상태 확인 (*IDN?)", "\n\n".join(lines))
            return

        try:
            idn = self.driver.identify()
            extra = (
                f"\n\n(방식 A TSP-Link 연결 대수: {self.spn_nodes.value()}대)"
                if self.spn_nodes.value() > 1
                else ""
            )
            QMessageBox.information(self, "장비 연결 상태 확인 (*IDN?)", f"현재 연결된 장비 응답:\n\n{idn}{extra}")
        except Exception as exc:
            QMessageBox.critical(self, "통신 오류", str(exc))

    def _on_sim_mode_toggled(self, checked: bool) -> None:
        self.prefer_simulator = checked
        self._populate_top_visa_combo(self.cmb_model.currentText())
        self._connect_driver_to_resource(self.active_resource_name, self.cmb_model.currentText())

    def _reset_waveform_badge_to_default(self) -> None:
        """Reset top Waveform Editor button badge back to inactive default style."""
        if hasattr(self, "btn_waveform_popup"):
            self.btn_waveform_popup.setText("🌊 파형 편집기")
            self.btn_waveform_popup.setStyleSheet(
                "background-color: #eef2ff; color: #3730a3; border: 1px solid #a5b4fc; font-weight: bold; padding: 5px 10px;"
            )

    def _reset_input_defaults(self) -> None:
        self.cmb_sweep_type.setCurrentText(SweepType.LINEAR.value)
        self.cmb_source_func.setCurrentText(SourceFunction.VOLTAGE.value)
        self.spn_start.setValue(0.0)
        self.spn_stop.setValue(1.2)
        self.spn_points.setValue(21)
        self.spn_delay.setValue(0.005)
        self.spn_repeat_count.setValue(1)
        self.spn_compliance.setValue(0.1)
        self.spn_nplc.setValue(0.1)
        self.cmb_terminals.setCurrentText(TerminalLocation.FRONT.value)
        self.cmb_meas_range.setCurrentIndex(0)
        self.cmb_auto_zero.setCurrentText("ON")
        self.chk_source_readback.setChecked(False)
        self.chk_high_cap.setChecked(False)
        self.custom_steps = WaveformBuilder.build_staircase_wave(0.0, 1.2, 13, 0.005, dual_return=True)
        self._populate_waveform_table()
        self._reset_waveform_badge_to_default()
        self.status_bar.showMessage("입력 파라미터 및 커스텀 파형 설정이 기본값(Default LINEAR)으로 초기화되었습니다.")

    # =========================================================================
    # Connection, Manual, Detached Window, Range Clamp, Setup Save/Load
    # =========================================================================

    def open_connection_wizard(self) -> None:
        dlg = SMUConnectionWizardDialog(
            visa_mgr=self.visa_mgr,
            registry=self.registry,
            initial_model=self.cmb_model.currentText(),
            initial_nodes=self.spn_nodes.value(),
            initial_sync_mode=self.multi_sync_mode,
            initial_node_resources=[c.visa_resource for c in self.logical_channels],
            parent=self,
        )
        if dlg.exec() == dlg.DialogCode.Accepted:
            self.active_resource_name = dlg.selected_resource
            self.multi_sync_mode = dlg.selected_sync_mode
            self.last_checklist_status = [
                {"title": c.item_spec.title, "checked": c.checkbox.isChecked()}
                for c in dlg.step_cards
            ]
            self.cmb_model.setCurrentText(dlg.selected_model)
            self.spn_nodes.setValue(dlg.selected_nodes)
            for lch in self.logical_channels:
                if lch.node_id in dlg.selected_node_resources:
                    lch.visa_resource = dlg.selected_node_resources[lch.node_id]
            self._connect_driver_to_resource(self.active_resource_name, dlg.selected_model)
            self._refresh_multi_conn_badge()

    def _populate_top_visa_combo(self, model_name: str) -> None:
        if not hasattr(self, "cmb_visa_bar"):
            return
        self.cmb_visa_bar.blockSignals(True)
        self.cmb_visa_bar.clear()
        discovered = self.visa_mgr.discover_instruments(include_simulators=True)
        hw_idx = -1
        sim_idx = 0
        for i, inst in enumerate(discovered):
            tag = "[SIM]" if inst.is_simulator else f"[HW-{inst.interface_type}]"
            self.cmb_visa_bar.addItem(f"{inst.resource_name} {tag}", inst.resource_name)
            if not inst.is_simulator and hw_idx < 0:
                hw_idx = i
            if inst.is_simulator and model_name in inst.resource_name:
                sim_idx = i
        target_idx = sim_idx if (self.prefer_simulator or hw_idx < 0) else hw_idx
        self.cmb_visa_bar.setCurrentIndex(target_idx)
        self.active_resource_name = self.cmb_visa_bar.currentData() or f"SIM::{model_name}::INSTR"
        self.cmb_visa_bar.blockSignals(False)

    def _on_top_visa_resource_changed(self, *_) -> None:
        res = self.cmb_visa_bar.currentData() or self.cmb_visa_bar.currentText().split()[0].strip()
        if res:
            self._connect_driver_to_resource(res, self.cmb_model.currentText())

    def _connect_driver_to_resource(self, resource_name: str, model_name: str) -> None:
        self.active_resource_name = resource_name
        try:
            transport = self.visa_mgr.open_transport_session(resource_name, model=model_name)
            self.driver = create_smu_driver(model_name, transport=transport, spy=self.spy, registry=self.registry)
            if self.driver.is_simulated:
                self.lbl_conn_badge.setText(f"[시뮬레이션 연결] {resource_name}")
                self.lbl_conn_badge.setStyleSheet("color: #facc15; font-weight: bold; border: none;")
            else:
                self.lbl_conn_badge.setText(f"[실제 장비 연결 성공] {resource_name}")
                self.lbl_conn_badge.setStyleSheet("color: #4ade80; font-weight: bold; border: none;")
        except Exception as exc:
            self.spy.record("ERR", model_name, f"실물 VISA 연결 실패 ({resource_name}) -> 시뮬레이터 전환: {exc}")
            self.driver = create_smu_driver(model_name, spy=self.spy, registry=self.registry)
            self.lbl_conn_badge.setText(f"[시뮬레이터 폴백] SIM::{model_name}::INSTR")
            self.lbl_conn_badge.setStyleSheet("color: #fb923c; font-weight: bold; border: none;")
        self.engine = SweepExecutionEngine(self.driver)

    def open_current_model_manual(self) -> bool:
        return open_model_manual_pdf(self.cmb_model.currentText(), self)

    def open_detached_graph_window(self) -> DetachedGraphWindow:
        win = DetachedGraphWindow(
            result=self.last_result,
            run_history=self.run_history,
            exporter=self.exporter,
            parent=self,
        )
        self.detached_windows.append(win)
        win.show()
        return win

    def build_measurement_time_guide_html(self) -> tuple[str, dict[str, float]]:
        """Calculate expected Elapsed measurement time from current UI settings and return a beginner-friendly HTML guide."""
        p = self.driver.profile
        recipe = self._build_current_recipe()
        auto_zero = self.cmb_auto_zero.currentText()
        source_readback = self.chk_source_readback.isChecked()

        levels = recipe.generate_levels()
        cycle_pts = max(1, len(levels))
        is_continuous = recipe.repeat_mode == RepeatMode.CONTINUOUS
        repeat_n = max(1, recipe.repeat_count) if recipe.repeat_mode == RepeatMode.FINITE else 1
        total_pts = cycle_pts * repeat_n
        intervals = max(0, total_pts - 1)  # Step 1 = 0.0000 s 기준점이므로 총 N-1 구간

        if recipe.sweep_type == SweepType.LIST and recipe.custom_steps:
            step_durs = [max(0.0, s.duration_s) for s in recipe.custom_steps]
            avg_delay_s = sum(step_durs) / max(1, len(step_durs))
            total_delay_s = intervals * avg_delay_s
        else:
            avg_delay_s = max(0.0, recipe.delay_s)
            total_delay_s = intervals * avg_delay_s

        # 60Hz 전원 기준 ADC 적분 시간 (1 NPLC = 1/60 s ≈ 16.67 ms)
        base_adc_s = max(0.0001, recipe.nplc) / 60.0
        az_mult = 3.0 if auto_zero == "ON" else 1.0
        t_adc_per_pt = base_adc_s * az_mult

        # 장비 내부 처리 시간 (AutoZero 보정 / Source Readback / AutoRange·소스 전환)
        t_az_extra = 0.015 if auto_zero == "ON" else 0.0
        t_rb_extra = 0.010 if (source_readback and p.is_24xx) else 0.0
        t_sys_base = 0.003
        t_overhead_per_pt = t_az_extra + t_rb_extra + t_sys_base

        t_step_total = avg_delay_s + t_adc_per_pt + t_overhead_per_pt
        ideal_elapsed_s = total_delay_s
        est_low_s = intervals * (avg_delay_s + t_adc_per_pt + t_overhead_per_pt * 0.75)
        est_mid_s = intervals * t_step_total
        est_high_s = intervals * (avg_delay_s + t_adc_per_pt + t_overhead_per_pt * 1.25)

        actual_block = ""
        if self.last_result and self.last_result.points:
            actual_elapsed = self.last_result.points[-1].timestamp_s
            actual_block = (
                f"<div style='margin-top:6px; padding:6px 10px; background:#ecfdf5; border:1px solid #6ee7b7; border-radius:5px; color:#065f46;'>"
                f"📌 <b>방금 측정한 실제 완료 시간 (Elapsed):</b> <b>{actual_elapsed:.4f} s</b> ({len(self.last_result.points):,} pts)"
                f"</div>"
            )

        cont_note = (
            "<div style='margin-top:6px; color:#b45309; font-weight:bold;'>"
            "※ 현재 '무한 연속(CONTINUOUS)' 모드이므로 아래 계산은 1주기(1 Cycle) 기준 시간입니다.</div>"
            if is_continuous
            else ""
        )

        rb_label = "ON (실제 인가값 재측정)" if (source_readback and p.is_24xx) else ("OFF (설정값 그대로 기록)" if p.is_24xx else "N/A (26xx 미적용)")
        az_desc = {
            "ON": "ON (매 포인트마다 영점 보정 → 적분시간 ×3 + ~15ms)",
            "ONCE": "ONCE (측정 시작 전 1회만 보정 → 빠르고 안정적)",
            "OFF": "OFF (보정 꺼짐 → 최고 속도)",
        }.get(auto_zero, auto_zero)

        html = f"""
        <div style="font-family:'맑은 고딕', sans-serif; font-size:9.5pt; color:#0f172a; line-height:1.55;">
          <h3 style="margin:0 0 8px 0; color:#0b2545;">⏱ 현재 설정값 기준 예상 측정 시간 (Elapsed) 안내</h3>

          <div style="background:#eff6ff; border:1.5px solid #3b82f6; border-radius:6px; padding:10px 12px; margin-bottom:10px;">
            <div style="font-size:10.5pt; font-weight:bold; color:#1d4ed8;">
              예상 측정 완료 시간 (Elapsed): 약 {est_low_s:.2f} s ~ {est_high_s:.2f} s &nbsp;(평균 약 {est_mid_s:.2f} s)
            </div>
            <div style="color:#334155; margin-top:3px;">
              • 단순 이론 지연 합계 (스텝 지연만 계산): <b>{ideal_elapsed_s:.3f} s</b> &nbsp;|&nbsp;
              • 포인트 1개당 예상 소요 시간: <b>{t_step_total * 1000:.1f} ms/pt</b>
            </div>
            {actual_block}
            {cont_note}
          </div>

          <h4 style="margin:10px 0 4px 0; color:#1e3a8a;">1. 초보자도 바로 이해하는 측정 시간 공식</h4>
          <div style="background:#f8fafc; border:1px solid #cbd5e1; border-radius:5px; padding:8px 10px; font-family:Consolas, '맑은 고딕';">
            <b>총 완료 시간 (Elapsed) ≈ (총 포인트 수 − 1) × (스텝 지연 + ADC 적분 시간 + 장비 내부 처리 시간)</b>
          </div>
          <ul style="margin:6px 0 10px 18px; padding:0;">
            <li><b>왜 (총 포인트 수 − 1) 인가요?</b><br>
                첫 번째 측정점(Step 1)이 <b>0.0000 s 기준점</b>이기 때문입니다. 예를 들어 50개 포인트라면 점과 점 사이의 간격은 <b>49구간</b>이 됩니다.</li>
            <li><b>스텝 지연 (Step Delay):</b> 전압/전류를 바꾼 뒤 회로가 안정될 때까지 기다리는 시간입니다.</li>
            <li><b>ADC 적분 시간 (NPLC):</b> SMU가 노이즈를 줄이기 위해 신호를 읽어들이는 시간입니다. 한국 전원(60Hz) 기준 <b>1 NPLC = 16.67 ms</b>이며, <code>AutoZero = ON</code>이면 매 포인트마다 내부 영점을 다시 재므로 약 <b>3배</b>가 걸립니다.</li>
            <li><b>장비 내부 처리 시간:</b> AutoZero 스위칭, Source Readback(실제 인가값 재측정), AutoRange(전류 범위 자동 탐색) 등에 쓰이는 하드웨어 고정 시간입니다.</li>
          </ul>

          <h4 style="margin:10px 0 4px 0; color:#1e3a8a;">2. 지금 입력하신 설정값 대입 계산표 ({p.model} / {recipe.sweep_type.value})</h4>
          <table width="100%" cellspacing="0" cellpadding="5" style="border-collapse:collapse; border:1px solid #cbd5e1; font-size:9pt;">
            <tr style="background:#0b2545; color:white;">
              <th align="left">항목</th>
              <th align="left">현재 설정값</th>
              <th align="right">1포인트당 시간</th>
              <th align="right">{intervals}구간 합계</th>
            </tr>
            <tr style="background:#ffffff;">
              <td><b>스텝 지연 (Delay)</b></td>
              <td>{avg_delay_s:.5f} s</td>
              <td align="right">{avg_delay_s * 1000:.2f} ms</td>
              <td align="right">{total_delay_s:.3f} s</td>
            </tr>
            <tr style="background:#f8fafc;">
              <td><b>ADC 적분 (NPLC)</b></td>
              <td>{recipe.nplc:g} PLC (×{az_mult:g})</td>
              <td align="right">{t_adc_per_pt * 1000:.2f} ms</td>
              <td align="right">{intervals * t_adc_per_pt:.3f} s</td>
            </tr>
            <tr style="background:#ffffff;">
              <td><b>AutoZero</b></td>
              <td>{az_desc}</td>
              <td align="right">+{t_az_extra * 1000:.1f} ms</td>
              <td align="right">{intervals * t_az_extra:.3f} s</td>
            </tr>
            <tr style="background:#f8fafc;">
              <td><b>Source Readback</b></td>
              <td>{rb_label}</td>
              <td align="right">+{t_rb_extra * 1000:.1f} ms</td>
              <td align="right">{intervals * t_rb_extra:.3f} s</td>
            </tr>
            <tr style="background:#ffffff;">
              <td><b>기본 범위/스위칭</b></td>
              <td>AutoRange · 소스 전환</td>
              <td align="right">+{t_sys_base * 1000:.1f} ms</td>
              <td align="right">{intervals * t_sys_base:.3f} s</td>
            </tr>
            <tr style="background:#e0f2fe; font-weight:bold;">
              <td colspan="2">합계 (총 {total_pts:,} pts / {intervals:,}구간 기준)</td>
              <td align="right">{t_step_total * 1000:.2f} ms/pt</td>
              <td align="right">약 {est_mid_s:.3f} s</td>
            </tr>
          </table>

          <div style="margin-top:10px; padding:8px 10px; background:#fffbeb; border:1px solid #fcd34d; border-radius:5px; font-size:8.8pt;">
            💡 <b>실전 팁:</b>
            측정 속도를 이론치(<code>{ideal_elapsed_s:.2f} s</code>)에 가깝게 빠르게 만들고 싶다면 좌측 <b>2. 정밀도 &amp; 안전 보호</b>에서
            <b>AutoZero = ONCE</b>, <b>Source Readback = 해제(OFF)</b>로 설정해 보세요.
            반대로 미세 전류(nA 이하) 초정밀 측정이 필요할 때는 <b>AutoZero = ON</b>, <b>NPLC = 1.0</b> 이상을 권장합니다.
          </div>
        </div>
        """
        stats = {
            "total_pts": float(total_pts),
            "intervals": float(intervals),
            "ideal_elapsed_s": ideal_elapsed_s,
            "est_low_s": est_low_s,
            "est_mid_s": est_mid_s,
            "est_high_s": est_high_s,
            "per_point_ms": t_step_total * 1000.0,
        }
        return html, stats

    def open_measurement_time_guide_dialog(self) -> None:
        """Open a clean modal popup explaining how Elapsed measurement time is calculated for current inputs."""
        html, _ = self.build_measurement_time_guide_html()
        dlg = QDialog(self)
        dlg.setWindowTitle("⏱ 예상 측정 시간 (Elapsed) 계산기 & 공식 안내")
        dlg.resize(650, 560)
        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(12, 12, 12, 10)

        browser = QTextBrowser(dlg)
        browser.setHtml(html)
        browser.setStyleSheet("background-color: #ffffff; border: 1px solid #cbd5e1; border-radius: 6px; padding: 6px;")
        layout.addWidget(browser, 1)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        btn_close = QPushButton("확인 (닫기)")
        btn_close.setStyleSheet("background-color: #2563eb; color: white; font-weight: bold; padding: 6px 18px; border-radius: 4px;")
        btn_close.clicked.connect(dlg.accept)
        btn_row.addWidget(btn_close)
        layout.addLayout(btn_row)
        dlg.exec()

    def clear_graph_and_table_data(self) -> None:
        self.last_result = None
        self.run_history.clear()
        self.single_dc_points.clear()
        self.plot_iv.clear()
        self.lbl_cursor_readout.setText("커서 대기 중")
        self.lbl_metrics.setText("초기화 완료 — 그래프 및 측정 데이터 표가 깨끗하게 비워졌습니다.")
        self.lbl_live_dmm.setText("V: --- | I: --- | R: --- | P: ---")
        self.lbl_state_pill.setText("● 상태: 초기화 완료 (CLEARED)")
        self.lbl_sample_count.setText("누적 샘플: 0 pts")
        self.lbl_diag_summary.setText("🩺 자동 측정 코치 대기 중 — 측정 완료 시 15대 셋업·품질 항목을 자동 진단합니다.")
        self.lbl_diag_summary.setStyleSheet("color: #475569; font-weight: bold; font-size: 8.6pt; border: none;")
        self.frm_diag_banner.setStyleSheet("QFrame { background-color: #f8fafc; border: 1px solid #cbd5e1; border-radius: 5px; }")
        for card, ph in [
            (self.kpi_voltage, "--- V"),
            (self.kpi_current, "--- A"),
            (self.kpi_power, "--- W"),
            (self.kpi_dvdi, "--- Ω"),
            (self.kpi_vth, "--- V"),
            (self.kpi_ileak, "--- A"),
            (self.kpi_pulse, "---"),
            (self.kpi_compliance, "NORMAL (0)"),
        ]:
            card.set_value(ph)

        self.tbl_live_data.setRowCount(0)
        self.table.setRowCount(0)
        for dwin in list(self.detached_windows):
            if dwin.isVisible():
                dwin.update_data(None, [])
        try:
            if self.driver.profile.is_24xx:
                if not self.driver.is_simulated and "SCPI" in self.driver.transport.query("*LANG?").upper():
                    self.driver.transport.write(":TRAC:CLE 'defbuffer1'")
                else:
                    self.driver.transport.write("defbuffer1.clear()")
            else:
                pfx = self.driver.profile.channel_names[0]
                self.driver.transport.write(f"{pfx}.nvbuffer1.clear()")
        except Exception:
            pass
        self.spy.record("SYS", self.driver.profile.model, "그래프·데이터 표·KPI·버퍼 전체 초기화 완료")

    def _validate_and_clamp_spinbox(self, spinbox: QDoubleSpinBox, kind: str, label_ko: str) -> tuple[float, str | None]:
        p = self.driver.profile
        val = spinbox.value()
        s_func = SourceFunction(self.cmb_source_func.currentText())
        is_pulse = self.cmb_sweep_type.currentText() in (SweepType.PULSE_TRAIN.value, SweepType.PULSE_SWEEP.value)

        if kind == "source_level":
            max_lim = (p.max_pulse_voltage_v if is_pulse else p.max_dc_voltage_v) if s_func == SourceFunction.VOLTAGE else (p.max_pulse_current_a if is_pulse else p.max_dc_current_a)
            unit = "V" if s_func == SourceFunction.VOLTAGE else "A"
            min_lim = -max_lim
        elif kind == "compliance":
            max_lim = (p.max_pulse_current_a if is_pulse else p.max_dc_current_a) if s_func == SourceFunction.VOLTAGE else (p.max_pulse_voltage_v if is_pulse else p.max_dc_voltage_v)
            unit = "A" if s_func == SourceFunction.VOLTAGE else "V"
            min_lim = 1e-7
        elif kind == "nplc":
            min_lim, max_lim, unit = p.min_nplc, p.max_nplc, " PLC"
        elif kind == "pulse_width_us":
            min_lim, max_lim, unit = p.min_pulse_width_s * 1e6, 500000.0, " µs"
        else:
            return val, None

        clamped, notice = clamp_value_with_notice(val, min_lim, max_lim, label_ko, p.model, unit)
        if notice is not None:
            self.last_range_notice = notice
            spinbox.blockSignals(True)
            spinbox.setValue(clamped)
            spinbox.blockSignals(False)
            self.spy.record("SYS", p.model, notice.replace("\n", " "))
            self._on_sweep_param_changed()
            if not self.suppress_range_popup:
                QMessageBox.warning(self, f"{p.model} 입력 범위 초과 자동 조정", notice)
        return clamped, notice

    def _on_repeat_count_spin_changed(self, val: int) -> None:
        self.cmb_repeat_mode.blockSignals(True)
        if val <= 0:
            self.cmb_repeat_mode.setCurrentText(RepeatMode.CONTINUOUS.value)
        elif val == 1:
            self.cmb_repeat_mode.setCurrentText(RepeatMode.SINGLE.value)
        else:
            self.cmb_repeat_mode.setCurrentText(RepeatMode.FINITE.value)
        self.cmb_repeat_mode.blockSignals(False)
        self._on_sweep_param_changed()

    def generate_easy_parametric_waveform(self) -> None:
        p = self.driver.profile
        s_func = SourceFunction(self.cmb_source_func.currentText())
        max_lim = p.max_dc_voltage_v if s_func == SourceFunction.VOLTAGE else p.max_dc_current_a
        unit = "V" if s_func == SourceFunction.VOLTAGE else "A"

        high_v, notice_h = clamp_value_with_notice(self.spn_easy_high.value(), -max_lim, max_lim, "쉬운 파형 High 레벨", p.model, unit)
        if notice_h:
            self.last_range_notice = notice_h
            self.spn_easy_high.setValue(high_v)
            if not self.suppress_range_popup:
                QMessageBox.warning(self, f"{p.model} 입력 범위 초과 자동 조정", notice_h)

        low_v, notice_l = clamp_value_with_notice(self.spn_easy_low.value(), -max_lim, max_lim, "쉬운 파형 Low 레벨", p.model, unit)
        if notice_l:
            self.last_range_notice = notice_l
            self.spn_easy_low.setValue(low_v)
            if not self.suppress_range_popup:
                QMessageBox.warning(self, f"{p.model} 입력 범위 초과 자동 조정", notice_l)

        res = WaveformBuilder.build_easy_waveform(
            shape=self.cmb_easy_shape.currentText(),
            high_level=high_v,
            low_level=low_v,
            duration_s=self.spn_easy_dur.value(),
            repeat_count=self.spn_easy_repeat.value(),
            compliance=self.spn_compliance.value(),
        )
        self.custom_steps = res.steps
        self._populate_waveform_table()
        self.cmb_sweep_type.setCurrentText(SweepType.LIST.value)
        self.cmb_repeat_mode.setCurrentText(res.repeat_mode.value)
        self.spn_repeat_count.blockSignals(True)
        self.spn_repeat_count.setValue(self.spn_easy_repeat.value())
        self.spn_repeat_count.blockSignals(False)
        self.refresh_waveform_preview()
        self.spy.record("SYS", p.model, f"쉬운 파형 생성 완료: {res.summary_ko}")

    def _delete_selected_waveform_steps(self) -> None:
        self.custom_steps = self._read_waveform_table_steps()
        selected_rows = sorted({idx.row() for idx in self.tbl_waveform.selectedIndexes()})
        if not selected_rows and self.tbl_waveform.rowCount() > 0:
            selected_rows = [self.tbl_waveform.rowCount() - 1]
        self.custom_steps = WaveformBuilder.delete_steps(self.custom_steps, selected_rows)
        self._populate_waveform_table()
        self.refresh_waveform_preview()

    def _clear_all_waveform_steps(self) -> None:
        self.custom_steps = [
            WaveformStep(1, duration_s=self.spn_delay.value(), level=0.0, compliance_limit=self.spn_compliance.value())
        ]
        self._populate_waveform_table()
        self.refresh_waveform_preview()

    def save_setup_dialog(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "SMU 전체 설정 저장", "smu_full_setup.json", "SMU Setup JSON (*.json)")
        if not path:
            return
        saved = self.save_setup_to_path(path, silent=False)
        QMessageBox.information(self, "설정 저장 완료", f"장비 및 파형 전체 설정이 저장되었습니다:\n{saved}")

    def save_setup_to_path(self, file_path: Path | str, silent: bool = False) -> Path:
        p = self.driver.profile
        recipe = self._build_current_recipe()
        p_cfg = self._build_pulse_digitizer_config()
        ch_cfg = ChannelConfig(
            logical_channel=LogicalChannel(1, p.channel_names[0]),
            source_func=recipe.source_func,
            measure_func=recipe.measure_func,
            compliance_limit=self.spn_compliance.value(),
            nplc=self.spn_nplc.value(),
            sense_mode=SenseMode(self.cmb_sense.currentText()),
            terminals=TerminalLocation(self.cmb_terminals.currentText()),
            measure_range=self.cmb_meas_range.currentData(),
            high_capacitance=self.chk_high_cap.isChecked(),
            output_off_mode=OutputOffMode(self.cmb_offmode.currentText()),
            auto_zero=self.cmb_auto_zero.currentText(),
            source_readback=self.chk_source_readback.isChecked(),
            contact_check_enabled=self.chk_contact.isChecked(),
        )
        self._sync_logical_channels_from_table()
        out = SetupPersistenceManager.save_full_setup(
            file_path=file_path,
            model=p.model,
            variant=self._selected_variant_code(),
            node_count=self.spn_nodes.value(),
            dut_type=self.cmb_dut.currentText(),
            interlock_engaged=self.chk_interlock.isChecked(),
            channel_config=ch_cfg,
            recipe=recipe,
            pulse_config=p_cfg,
            logical_channels=self.logical_channels,
            export_dir=self.exporter.output_dir,
            sync_mode=self.multi_sync_mode.value,
        )
        if not silent:
            self.spy.record("SYS", p.model, f"Full Setup Saved -> {out}")
        return out

    def load_setup_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "SMU 전체 설정 불러오기", "", "SMU Setup JSON (*.json)")
        if not path:
            return
        self.load_setup_from_path(path, silent=False)
        QMessageBox.information(self, "설정 복원 완료", f"저장된 설정 파일을 성공적으로 불러왔습니다:\n{path}")

    def load_setup_from_path(self, file_path: Path | str, silent: bool = False) -> None:
        data = SetupPersistenceManager.load_full_setup(file_path)
        prev_suppress = self.suppress_range_popup
        self.suppress_range_popup = True
        try:
            self.cmb_model.setCurrentText(data["model"])
            for i in range(self.cmb_variant.count()):
                if self.cmb_variant.itemText(i).startswith(data["variant"]):
                    self.cmb_variant.setCurrentIndex(i)
                    break
            self.spn_nodes.setValue(data["node_count"])
            self.cmb_dut.setCurrentText(data["dut_type"])
            self.chk_interlock.setChecked(data["interlock_engaged"])

            rc: SweepRecipe = data["recipe"]
            self.cmb_sweep_type.setCurrentText(rc.sweep_type.value)
            self.cmb_source_func.setCurrentText(rc.source_func.value)
            self.spn_start.setValue(rc.start_level)
            self.spn_stop.setValue(rc.stop_level)
            self.spn_points.setValue(rc.points)
            self.spn_delay.setValue(rc.delay_s)
            self.spn_compliance.setValue(rc.compliance_limit)
            self.spn_nplc.setValue(rc.nplc)
            self.cmb_repeat_mode.setCurrentText(rc.repeat_mode.value)
            self.spn_repeat_count.blockSignals(True)
            self.spn_repeat_count.setValue(-1 if rc.repeat_mode == RepeatMode.CONTINUOUS else rc.repeat_count)
            self.spn_repeat_count.blockSignals(False)
            self.chk_return_bias.setChecked(rc.end_Return_to_bias)
            self.chk_neg_zero.setChecked(rc.apply_negative_zero_fix)

            if rc.custom_steps:
                self.custom_steps = rc.custom_steps
                self._populate_waveform_table()

            cc: ChannelConfig = data["channel_config"]
            self.cmb_sense.setCurrentText(cc.sense_mode.value)
            self.cmb_terminals.setCurrentText(cc.terminals.value)
            self._set_meas_range_combo(cc.measure_range)
            self.chk_high_cap.setChecked(cc.high_capacitance)
            self.cmb_offmode.setCurrentText(cc.output_off_mode.value)
            self.cmb_auto_zero.setCurrentText(cc.auto_zero)
            self.chk_source_readback.setChecked(cc.source_readback)
            self.chk_contact.setChecked(cc.contact_check_enabled)

            pc: PulseDigitizerConfig = data["pulse_config"]
            self.spn_pulse_width_us.setValue(pc.pulse_width_s * 1e6)
            self.spn_pulse_period_ms.setValue(pc.pulse_period_s * 1e3)
            self.spn_pulse_count.setValue(pc.pulse_count)
            self.chk_digitizer.setChecked(pc.digitize_enable)
            self.spn_sample_rate.setValue(pc.sample_rate_sps)
            self.spn_inductance_uh.setValue(pc.load_inductance_uh)
            self.chk_fast_adc.setChecked(pc.fast_adc_mode)
            self.chk_sink_mode.setChecked(pc.sink_mode_enable)
            self.spn_ambient_temp.setValue(pc.ambient_temp_c)

            if data["logical_channels"]:
                self.logical_channels = data["logical_channels"]
                self._populate_logical_channel_table_widgets()
            try:
                self.multi_sync_mode = MultiUnitSyncMode(data.get("sync_mode", "TSPLINK_HW"))
            except Exception:
                self.multi_sync_mode = MultiUnitSyncMode.TSPLINK_HW

            if data.get("export_dir"):
                self.set_export_directory(data["export_dir"], persist=True)

            self.refresh_waveform_preview()
            self.refresh_soa_chart()
            if not silent:
                self.spy.record("SYS", data["model"], f"Full Setup Loaded <- {file_path}")
        finally:
            self.suppress_range_popup = prev_suppress

    # =========================================================================
    # Model Switch, Channel Table, Pulse/SOA & Execution
    # =========================================================================

    def _on_sweep_param_changed(self, *_) -> None:
        if hasattr(self, "cmb_sweep_type") and self.cmb_sweep_type.currentText() != SweepType.LIST.value:
            self._reset_waveform_badge_to_default()
        self.refresh_waveform_preview()
        self.refresh_soa_chart()

    def _on_model_changed(self, model_name: str) -> None:
        # Highlight selected model pill button
        for mk, btn in self.model_buttons.items():
            is_sel = mk == model_name
            btn.setChecked(is_sel)
            if is_sel:
                btn.setStyleSheet(
                    "background-color: #2563eb; color: white; font-weight: bold; border: 1px solid #1d4ed8; padding: 5px 9px; border-radius: 5px;"
                )
            else:
                btn.setStyleSheet(
                    "background-color: #ffffff; color: #334155; font-weight: bold; border: 1px solid #cbd5e1; padding: 5px 9px; border-radius: 5px;"
                )

        self._populate_top_visa_combo(model_name)
        self._connect_driver_to_resource(self.active_resource_name, model_name)
        self.combined_config = None
        self.single_dc_points.clear()
        p = self.driver.profile

        self.cmb_variant.blockSignals(True)
        self.cmb_variant.clear()
        if p.model == "2600B":
            self.cmb_variant.addItems(["2602B", "2612B", "2636B", "2604B (No TSP-Link)", "2614B (No TSP-Link)", "2634B (No TSP-Link)"])
        else:
            self.cmb_variant.addItem(p.model)
        self.cmb_variant.blockSignals(False)

        pdf = find_manual_pdf_for_model(p.model)
        pdf_name = pdf.name if pdf else "N/A"
        self.lbl_model_title.setText(f"⚡ {p.display_name}")
        self.lbl_spec_badge.setText(
            f"• 지원 인터페이스: USB (USBTMC), LAN (Raw Socket 5025), GPIB  |  최대 채널: {p.channels}CH  |  "
            f"DC 출력: ±{p.max_dc_voltage_v}V / ±{p.max_dc_current_a}A ({p.max_dc_power_w}W)  |  "
            f"Pulse: {p.max_pulse_current_a}A (최소 {p.min_pulse_width_s*1e6:.0f}µs)  |  매뉴얼: {pdf_name}"
        )

        self.chk_contact.setEnabled(p.has_contact_check)
        self.chk_neg_zero.setEnabled(p.negative_zero_bias_fix)
        self.btn_combining.setEnabled(p.combining_supported)

        if hasattr(self, "btn_acal"):
            self.btn_acal.setEnabled(p.has_acal)
            if self.spn_pulse_width_us.value() < p.min_pulse_width_s * 1e6:
                self.spn_pulse_width_us.setValue(max(p.min_pulse_width_s * 1e6, 200.0))

        self._validate_and_clamp_spinbox(self.spn_start, "source_level", "시작 레벨")
        self._validate_and_clamp_spinbox(self.spn_stop, "source_level", "종료 레벨")
        self._validate_and_clamp_spinbox(self.spn_compliance, "compliance", "Compliance Limit")

        self._on_dut_changed(self.cmb_dut.currentText())
        self._on_interlock_toggled(self.chk_interlock.isChecked())
        self._rebuild_logical_channel_table()
        self._update_sync_warning_banner()
        self.refresh_soa_chart()

    def _selected_variant_code(self) -> str:
        return self.cmb_variant.currentText().split()[0].strip()

    def _on_nodes_changed(self, _: int) -> None:
        self._rebuild_logical_channel_table()
        self._update_sync_warning_banner()

    def _update_sync_warning_banner(self) -> None:
        p = self.driver.profile
        variant = self._selected_variant_code()
        nodes = self.spn_nodes.value()
        msgs: list[str] = []
        if nodes > 1 and not p.supports_tsplink_for_variant(variant):
            msgs.append(
                f"⚠ [SW 동기화 모드 활성] {variant} 모델은 TSP-Link 미지원입니다 ({nodes}대 연동 시 1~20ms Skew 발생 가능)."
            )
        if self.combined_config is not None:
            msgs.append(
                f"⚡ [출력 결합 활성: {self.combined_config.mode.value}] 합산={self.combined_config.total_target_level} | Slave 10% 마진={self.combined_config.slave_compliance}"
            )
        if msgs:
            self.lbl_warning_banner.setText("   |   ".join(msgs))
            self.lbl_warning_banner.setVisible(True)
        else:
            self.lbl_warning_banner.setVisible(False)

    def _rebuild_logical_channel_table(self) -> None:
        self.logical_channels = self.engine.cluster.build_logical_channels(self.spn_nodes.value())
        for lch in self.logical_channels:
            lch.override_sweep_type = self.cmb_sweep_type.currentText()
            lch.override_source_func = self.cmb_source_func.currentText()
            lch.override_start_level = self.spn_start.value()
            lch.override_stop_level = self.spn_stop.value()
            lch.override_compliance = self.spn_compliance.value()
        self._populate_logical_channel_table_widgets()

    def _populate_logical_channel_table_widgets(self) -> None:
        self.tbl_channels.setRowCount(len(self.logical_channels))
        for r, lch in enumerate(self.logical_channels):
            self.tbl_channels.setItem(r, 0, QTableWidgetItem(lch.label))
            self.tbl_channels.setItem(r, 1, QTableWidgetItem(lch.tsp_prefix))

            chk_en = QCheckBox("ON")
            chk_en.setChecked(lch.enabled)
            chk_en.toggled.connect(lambda checked, idx=r: self._on_channel_enable_toggled(idx, checked))
            self.tbl_channels.setCellWidget(r, 2, chk_en)

            cmb_role = QComboBox()
            for role in ChannelRole:
                cmb_role.addItem(role.value)
            cmb_role.setCurrentText(lch.role)
            cmb_role.currentTextChanged.connect(lambda val, idx=r: self._on_channel_role_changed(idx, val))
            self.tbl_channels.setCellWidget(r, 3, cmb_role)

            chk_ind = QCheckBox("개별 설정")
            chk_ind.setChecked(lch.independent_override)
            chk_ind.toggled.connect(lambda checked, idx=r: self._on_channel_ind_toggled(idx, checked))
            self.tbl_channels.setCellWidget(r, 4, chk_ind)

            cmb_swp = QComboBox()
            for st in SweepType:
                cmb_swp.addItem(st.value)
            cmb_swp.setCurrentText(lch.override_sweep_type)
            cmb_swp.currentTextChanged.connect(lambda val, idx=r: self._on_channel_swp_changed(idx, val))
            self.tbl_channels.setCellWidget(r, 5, cmb_swp)

            cmb_src = QComboBox()
            cmb_src.addItems([SourceFunction.VOLTAGE.value, SourceFunction.CURRENT.value])
            cmb_src.setCurrentText(lch.override_source_func)
            cmb_src.currentTextChanged.connect(lambda val, idx=r: self._on_channel_src_changed(idx, val))
            self.tbl_channels.setCellWidget(r, 6, cmb_src)

            self.tbl_channels.setItem(r, 7, QTableWidgetItem(f"{lch.override_start_level:g}, {lch.override_stop_level:g}"))
            self.tbl_channels.setItem(r, 8, QTableWidgetItem(f"{lch.override_compliance:g}"))

    def _sync_logical_channels_from_table(self) -> None:
        for r, lch in enumerate(self.logical_channels):
            if r >= self.tbl_channels.rowCount():
                break
            item_range = self.tbl_channels.item(r, 7)
            if item_range:
                parts = [x.strip() for x in item_range.text().split(",")]
                try:
                    lch.override_start_level = float(parts[0])
                    if len(parts) > 1:
                        lch.override_stop_level = float(parts[1])
                except Exception:
                    pass
            item_lim = self.tbl_channels.item(r, 8)
            if item_lim:
                try:
                    lch.override_compliance = float(item_lim.text().strip())
                except Exception:
                    pass

    def _on_channel_enable_toggled(self, idx: int, checked: bool) -> None:
        if 0 <= idx < len(self.logical_channels):
            self.logical_channels[idx].enabled = checked

    def _on_channel_role_changed(self, idx: int, role_val: str) -> None:
        if 0 <= idx < len(self.logical_channels):
            self.logical_channels[idx].role = role_val

    def _on_channel_ind_toggled(self, idx: int, checked: bool) -> None:
        if 0 <= idx < len(self.logical_channels):
            self.logical_channels[idx].independent_override = checked

    def _on_channel_swp_changed(self, idx: int, swp_val: str) -> None:
        if 0 <= idx < len(self.logical_channels):
            self.logical_channels[idx].override_sweep_type = swp_val

    def _on_channel_src_changed(self, idx: int, src_val: str) -> None:
        if 0 <= idx < len(self.logical_channels):
            self.logical_channels[idx].override_source_func = src_val

    def open_waveform_editor_dialog(self) -> None:
        """Open dedicated spacious popup dialog for Easy Waveform Generator & Arbitrary Step Editor."""
        dlg = WaveformEditorDialog(
            profile=self.driver.profile,
            source_func=SourceFunction(self.cmb_source_func.currentText()),
            initial_steps=self.custom_steps,
            default_delay_s=self.spn_delay.value(),
            default_compliance=self.spn_compliance.value(),
            initial_repeat_count=self.spn_repeat_count.value(),
            initial_repeat_mode=RepeatMode(self.cmb_repeat_mode.currentText()),
            end_return_to_bias=self.chk_return_bias.isChecked(),
            parent=self,
        )
        if dlg.exec() == dlg.DialogCode.Accepted:
            if dlg.reset_to_default_requested:
                self._reset_input_defaults()
                self.spy.record("SYS", self.driver.profile.model, "커스텀 파형 설정 해제 -> 기본 LINEAR 모드 복귀")
                return
            self.custom_steps = dlg.result_steps
            self._populate_waveform_table()
            self.cmb_sweep_type.setCurrentText(SweepType.LIST.value)
            self.cmb_repeat_mode.setCurrentText(dlg.result_repeat_mode.value)
            self.spn_repeat_count.blockSignals(True)
            self.spn_repeat_count.setValue(
                -1 if dlg.result_repeat_mode == RepeatMode.CONTINUOUS else dlg.result_repeat_count
            )
            self.spn_repeat_count.blockSignals(False)
            self.btn_waveform_popup.setText(f"🌊 파형 편집기 ({dlg.summary_badge_text})")
            self.btn_waveform_popup.setStyleSheet(
                "background-color: #4f46e5; color: white; border: 1px solid #3730a3; font-weight: bold; padding: 5px 10px;"
            )
            self.status_bar.showMessage(f"커스텀 파형 적용 완료: {dlg.summary_badge_text}")
            self.spy.record("SYS", self.driver.profile.model, f"커스텀 파형 편집기 설정 적용: {dlg.summary_badge_text}")
            if dlg.run_immediately_requested:
                self.cmb_plot_view.setCurrentIndex(1)  # Switch to Time-Domain waveform view
                self.tabs.setCurrentIndex(0)
                self.execute_measurement()

    def _refresh_multi_conn_badge(self) -> None:
        """Update top-bar connection status badge when Mode B multi-USB/LAN units are active."""
        en_chs = [c for c in self.logical_channels if c.enabled]
        if self.multi_sync_mode == MultiUnitSyncMode.MULTI_VISA_SW and len(en_chs) >= 2:
            summary = " + ".join(f"N{c.node_id}({c.model_name or self.driver.profile.model})" for c in en_chs[:4])
            self.lbl_conn_badge.setText(f"[다중 USB/LAN {len(en_chs)}대] {summary}")
            self.lbl_conn_badge.setStyleSheet("color: #38bdf8; font-weight: bold; border: none;")

    def open_multi_channel_dialog(self) -> None:
        """Open dedicated wide popup dialog for 1~10 SMU Node-Channel mapping, Mode A/B selection, and mixed models."""
        discovered = [d.resource_name for d in self.visa_mgr.discover_instruments(include_simulators=True)]
        dlg = MultiChannelConfigDialog(
            profile=self.driver.profile,
            logical_channels=self.logical_channels,
            variant_code=self._selected_variant_code(),
            sync_mode=self.multi_sync_mode,
            registry=self.registry,
            available_visa_resources=discovered,
            visa_mgr=self.visa_mgr,
            parent=self,
        )
        if dlg.exec() == dlg.DialogCode.Accepted:
            self.logical_channels = dlg.result_channels
            self.multi_sync_mode = dlg.result_sync_mode
            unique_nodes = len({c.node_id for c in self.logical_channels})
            if 1 <= unique_nodes <= 10 and self.spn_nodes.value() != unique_nodes:
                self.spn_nodes.blockSignals(True)
                self.spn_nodes.setValue(unique_nodes)
                self.spn_nodes.blockSignals(False)
            self._populate_logical_channel_table_widgets()
            self._update_sync_warning_banner()
            self._refresh_multi_conn_badge()

            en_chs = [c for c in self.logical_channels if c.enabled]
            models = list(dict.fromkeys((c.model_name or self.driver.profile.model) for c in en_chs))
            mode_tag = "방식A·TSP-Link" if self.multi_sync_mode == MultiUnitSyncMode.TSPLINK_HW else "방식B·USB/LAN"
            ind_cnt = sum(1 for c in en_chs if c.independent_override)
            en_cnt = len(en_chs)
            if len(models) > 1:
                self.btn_multich_popup.setText(f"🔗 {'+'.join(models[:3])} ({mode_tag})")
                self.btn_multich_popup.setStyleSheet(
                    "background-color: #0f766e; color: white; border: 1px solid #115e59; font-weight: bold; padding: 5px 10px;"
                )
            elif ind_cnt > 0 or en_cnt > 1:
                self.btn_multich_popup.setText(f"🔗 다중 채널 ({en_cnt}CH / {mode_tag})")
                self.btn_multich_popup.setStyleSheet(
                    "background-color: #15803d; color: white; border: 1px solid #166534; font-weight: bold; padding: 5px 10px;"
                )
            else:
                self.btn_multich_popup.setText(f"🔗 다중 장비·채널 설정 ({en_cnt}CH)")
                self.btn_multich_popup.setStyleSheet(
                    "background-color: #f0fdf4; color: #166534; border: 1px solid #86efac; font-weight: bold; padding: 5px 10px;"
                )
            self.status_bar.showMessage(
                f"다중 장비 설정 적용 완료: {' + '.join(models)} | {mode_tag} (활성 {en_cnt}CH, 개별 {ind_cnt}CH)"
            )
            self.spy.record(
                "SYS",
                self.driver.profile.model,
                f"다중 장비·혼합 연결 설정 적용: {'+'.join(models)} ({mode_tag}, {en_cnt}CH)",
            )

    def open_combining_wizard(self) -> None:
        dlg = OutputCombiningWizardDialog(self.driver.profile, self)
        if dlg.exec() == dlg.DialogCode.Accepted and dlg.combined_config is not None:
            self.combined_config = dlg.combined_config
            if self.spn_nodes.value() < 2 and self.driver.profile.channels < 2:
                self.spn_nodes.setValue(2)
            self._update_sync_warning_banner()

    def _build_pulse_digitizer_config(self) -> PulseDigitizerConfig:
        pw_s = self.spn_pulse_width_us.value() * 1e-6
        per_s = self.spn_pulse_period_ms.value() * 1e-3
        sr = self.spn_sample_rate.value()
        return PulseDigitizerConfig(
            bias_level=self.spn_start.value(),
            pulse_level=self.spn_stop.value(),
            pulse_width_s=pw_s,
            pulse_period_s=per_s,
            pulse_count=self.spn_pulse_count.value(),
            digitize_enable=self.chk_digitizer.isChecked(),
            sample_rate_sps=sr,
            aperture_s=1.0 / max(1000, sr),
            load_inductance_uh=self.spn_inductance_uh.value(),
            fast_adc_mode=self.chk_fast_adc.isChecked(),
            sink_mode_enable=self.chk_sink_mode.isChecked(),
            ambient_temp_c=self.spn_ambient_temp.value(),
            last_acal_temp_c=self.last_acal_temp_c,
        )

    def refresh_soa_chart(self) -> None:
        if not hasattr(self, "plot_soa"):
            return
        p = self.driver.profile
        self.plot_soa.clear()
        curves = SafetyAndSOAValidator.get_soa_boundary_curves(p)

        dc_v, dc_i = curves.get("DC", ([0.0], [0.0]))
        self.plot_soa.plot(dc_v, dc_i, pen=pg.mkPen("#38bdf8", width=2.5), name="DC SOA Boundary")
        if "PULSE" in curves:
            pv, pi = curves["PULSE"]
            self.plot_soa.plot(pv, pi, pen=pg.mkPen("#4ade80", width=2.5, style=Qt.PenStyle.DashLine), name="Pulse Extended SOA")

        recipe = self._build_current_recipe()
        cfg = ChannelConfig(logical_channel=LogicalChannel(1, p.channel_names[0]))
        p_cfg = self._build_pulse_digitizer_config()
        recipe.pulse_width_s = p_cfg.pulse_width_s
        recipe.pulse_period_s = p_cfg.pulse_period_s

        rep = SafetyAndSOAValidator.validate_soa_and_recipe(
            p, cfg, recipe, ambient_temp_c=p_cfg.ambient_temp_c, is_sink_quadrant=p_cfg.sink_mode_enable
        )
        max_lvl = max(abs(self.spn_start.value()), abs(self.spn_stop.value()))
        comp = abs(self.spn_compliance.value())
        op_v = max_lvl if recipe.source_func == SourceFunction.VOLTAGE else comp
        op_i = comp if recipe.source_func == SourceFunction.VOLTAGE else max_lvl

        pt_color = "#4ade80" if rep.passed else "#f87171"
        self.plot_soa.plot([op_v], [op_i], pen=None, symbol="o", symbolSize=12, symbolBrush=pt_color, name=f"Op ({op_v:.3g}V, {op_i:.3g}A)")

        duty_pct = (p_cfg.pulse_width_s / max(p_cfg.pulse_period_s, p_cfg.pulse_width_s)) * 100.0
        max_duty_pct = SafetyAndSOAValidator.compute_thermal_and_sink_max_duty(p, op_v, op_i, p_cfg.ambient_temp_c, p_cfg.sink_mode_enable) * 100.0
        acal_st = HighSpeedPulseDigitizerEngine.check_and_run_2461_acal(p, p_cfg, execute_now=False)
        status_header = "✔ [SOA & Duty Cycle 안전 검증 통과]" if rep.passed else "✖ [SOA / Duty Cycle 위반]"
        err_detail = ("\n• " + "\n• ".join(rep.errors)) if rep.errors else ""
        self.lbl_soa_status.setText(
            f"{status_header}\n• 동작점: |V|={op_v:.3g}V, |I|={op_i:.3g}A (P={op_v*op_i:.2f}W)\n"
            f"• 펄스 Duty: {duty_pct:.2f}% / 허용: {max_duty_pct:.2f}%\n• {acal_st.message}{err_detail}"
        )

    def run_remote_2461_acal(self) -> None:
        p = self.driver.profile
        p_cfg = self._build_pulse_digitizer_config()
        st = HighSpeedPulseDigitizerEngine.check_and_run_2461_acal(p, p_cfg, execute_now=True)
        for cmd in st.executed_commands:
            self.driver.transport.write(cmd)
        self.last_acal_temp_c = p_cfg.ambient_temp_c
        self.refresh_soa_chart()
        QMessageBox.information(self, "2461 Autocalibration (ACAL)", st.message)

    def execute_high_speed_pulse_measurement(self) -> None:
        self.cmb_sweep_type.setCurrentText(SweepType.PULSE_TRAIN.value)
        self.cmb_plot_view.setCurrentIndex(1)
        self.tabs.setCurrentIndex(0)
        self.execute_measurement()

    # --- Waveform Table Methods ---
    def _populate_waveform_table(self) -> None:
        self.tbl_waveform.setRowCount(len(self.custom_steps))
        for r, s in enumerate(self.custom_steps):
            self.tbl_waveform.setItem(r, 0, QTableWidgetItem(str(s.step_index)))
            self.tbl_waveform.setItem(r, 1, QTableWidgetItem(f"{s.duration_s:.6g}"))
            self.tbl_waveform.setItem(r, 2, QTableWidgetItem(f"{s.level:.6g}"))
            self.tbl_waveform.setItem(r, 3, QTableWidgetItem(f"{(s.compliance_limit or self.spn_compliance.value()):.6g}"))
            self.tbl_waveform.setItem(r, 4, QTableWidgetItem("1" if s.measure_enable else "0"))

    def _read_waveform_table_steps(self) -> list[WaveformStep]:
        steps: list[WaveformStep] = []
        for r in range(self.tbl_waveform.rowCount()):
            try:
                dur = float(self.tbl_waveform.item(r, 1).text())
                lvl = float(self.tbl_waveform.item(r, 2).text())
                lim = float(self.tbl_waveform.item(r, 3).text())
                meas_txt = self.tbl_waveform.item(r, 4).text().strip().lower()
                meas = meas_txt not in ("0", "false", "no", "off")
                steps.append(WaveformStep(len(steps) + 1, max(1e-6, dur), lvl, lim, meas))
            except Exception:
                continue
        return steps

    def _load_square_preset(self) -> None:
        self.custom_steps = WaveformBuilder.build_square_wave(self.spn_stop.value(), self.spn_start.value(), self.spn_delay.value(), self.spn_delay.value(), 5, self.spn_compliance.value())
        self._populate_waveform_table()
        self._apply_waveform_table_to_recipe()

    def _load_triangle_preset(self) -> None:
        self.custom_steps = WaveformBuilder.build_triangle_wave(self.spn_start.value(), self.spn_stop.value(), 10, self.spn_delay.value(), 2, self.spn_compliance.value())
        self._populate_waveform_table()
        self._apply_waveform_table_to_recipe()

    def _load_staircase_preset(self) -> None:
        self.custom_steps = WaveformBuilder.build_staircase_wave(self.spn_start.value(), self.spn_stop.value(), 11, self.spn_delay.value(), True, self.spn_compliance.value())
        self._populate_waveform_table()
        self._apply_waveform_table_to_recipe()

    def _add_waveform_step(self) -> None:
        self.custom_steps = self._read_waveform_table_steps()
        self.custom_steps.append(WaveformStep(len(self.custom_steps) + 1, self.spn_delay.value(), self.spn_stop.value(), self.spn_compliance.value(), True))
        self._populate_waveform_table()
        self.refresh_waveform_preview()

    def _import_waveform_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "파형 CSV/Excel 불러오기", "", "Waveform Files (*.csv *.xlsx *.xls)")
        if not path:
            return
        try:
            self.custom_steps = WaveformBuilder.import_steps_from_file(path, self.spn_compliance.value())
            self._populate_waveform_table()
            self._apply_waveform_table_to_recipe()
        except Exception as exc:
            QMessageBox.critical(self, "파형 임포트 오류", str(exc))

    def _export_waveform_file(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "파형 CSV 저장", "custom_waveform.csv", "CSV Files (*.csv)")
        if not path:
            return
        self.custom_steps = self._read_waveform_table_steps()
        WaveformBuilder.export_steps_to_csv(self.custom_steps, path)
        QMessageBox.information(self, "저장 완료", f"파형 테이블이 저장되었습니다:\n{path}")

    def _apply_waveform_table_to_recipe(self) -> None:
        self.custom_steps = self._read_waveform_table_steps()
        self.cmb_sweep_type.setCurrentText(SweepType.LIST.value)
        self.refresh_waveform_preview()

    def execute_custom_waveform_on_smu(self) -> None:
        """Apply the edited custom waveform table, switch graph to Time-Domain view, and output/measure on the SMU immediately."""
        self._apply_waveform_table_to_recipe()
        self.cmb_plot_view.setCurrentIndex(1)  # Switch to Time-Domain view so the output waveform is clearly visible
        self.tabs.setCurrentIndex(0)
        self.execute_measurement()

    def _build_current_recipe(self) -> SweepRecipe:
        p = self.driver.profile
        if hasattr(self, "tbl_waveform") and self.tbl_waveform.rowCount() > 0:
            self.custom_steps = self._read_waveform_table_steps()
        pw_s = self.spn_pulse_width_us.value() * 1e-6 if hasattr(self, "spn_pulse_width_us") else 0.0002
        per_s = self.spn_pulse_period_ms.value() * 1e-3 if hasattr(self, "spn_pulse_period_ms") else 0.010
        use_dig = self.chk_digitizer.isChecked() if hasattr(self, "chk_digitizer") else False
        sr = self.spn_sample_rate.value() if hasattr(self, "spn_sample_rate") else 1000000
        return SweepRecipe(
            name=f"{p.model}_{self.cmb_sweep_type.currentText().lower()}",
            sweep_type=SweepType(self.cmb_sweep_type.currentText()),
            source_func=SourceFunction(self.cmb_source_func.currentText()),
            measure_func=(MeasureFunction.CURRENT if self.cmb_source_func.currentText() == SourceFunction.VOLTAGE.value else MeasureFunction.VOLTAGE),
            start_level=self.spn_start.value(),
            stop_level=self.spn_stop.value(),
            points=self.spn_points.value(),
            delay_s=self.spn_delay.value(),
            compliance_limit=self.spn_compliance.value(),
            nplc=self.spn_nplc.value(),
            pulse_width_s=pw_s,
            pulse_period_s=per_s,
            repeat_mode=RepeatMode(self.cmb_repeat_mode.currentText()),
            repeat_count=max(1, self.spn_repeat_count.value()),
            end_Return_to_bias=self.chk_return_bias.isChecked(),
            apply_negative_zero_fix=self.chk_neg_zero.isChecked(),
            use_digitizer=use_dig,
            digitizer_rate_sps=sr,
            custom_steps=list(self.custom_steps),
        )

    def refresh_waveform_preview(self) -> None:
        if not hasattr(self, "plot_preview"):
            return
        recipe = self._build_current_recipe()
        pv = WaveformBuilder.generate_preview(recipe)
        self.plot_preview.clear()
        self.plot_preview.plot(pv.time_s, pv.level_va, pen=pg.mkPen(color="#38bdf8", width=2.2), name="Source")
        if len(pv.measure_times_s) > 0:
            self.plot_preview.plot(pv.measure_times_s, pv.measure_levels_va, pen=None, symbol="o", symbolSize=6, symbolBrush="#f472b6", name="Measure")
        tot_str = "∞ (무한 연속 -1)" if pv.is_infinite_continuous else f"{pv.total_duration_s:.4f}s ({pv.total_measure_points:,} pts)"
        self.lbl_preview_summary.setText(f"[프리뷰] 1주기: {pv.single_cycle_duration_s:.4f}s ({pv.single_cycle_points} pts) | 총: {tot_str}")

    def _on_dut_changed(self, dut_str: str) -> None:
        if isinstance(self.driver.transport, VirtualSMUTransport):
            self.driver.transport.state.dut_type = VirtualDUTType(dut_str)

    def _on_interlock_toggled(self, checked: bool) -> None:
        if isinstance(self.driver.transport, VirtualSMUTransport):
            self.driver.transport.state.interlock_engaged = checked

    # --- Live Measurement Progress & Execution ---
    def _set_measuring_ui_state(self, running: bool, status_text: str = "") -> None:
        self._is_measuring = running
        if running:
            self._meas_elapsed_s = 0.0
            self.lbl_running_status.setText(status_text or "⏳ SMU 소스 출력 및 측정 진행 중입니다...")
            self.lbl_elapsed_time.setText("경과 시간: 0.0 s")
            self.progress_bar.setRange(0, 0)
            self.frm_progress_banner.setVisible(True)
            self.lbl_state_pill.setText("● 상태: 측정 진행 중 (RUNNING)")
            self.lbl_state_pill.setStyleSheet("color: #d97706; font-weight: bold; font-size: 9pt;")
            self.btn_run.setEnabled(False)
            self.btn_run.setText("⏳ 측정 진행 중...")
            self.btn_run.setStyleSheet("background-color: #d97706; color: white; font-weight: bold; font-size: 10pt; border: none; padding: 7px 16px; border-radius: 5px;")
            self.btn_quick_dc.setEnabled(False)
            self._meas_timer.start()
            QApplication.processEvents()
        else:
            self._meas_timer.stop()
            self.frm_progress_banner.setVisible(False)
            self.btn_run.setEnabled(True)
            self.btn_run.setText("▶ 측정 시작 (Run)")
            self.btn_run.setStyleSheet("background-color: #16a34a; color: white; font-weight: bold; font-size: 10pt; border: none; padding: 7px 16px; border-radius: 5px;")
            self.btn_quick_dc.setEnabled(True)

    def _on_measurement_timer_tick(self) -> None:
        self._meas_elapsed_s += 0.2
        dots = "." * (int(self._meas_elapsed_s * 2) % 4)
        self.lbl_elapsed_time.setText(f"경과 시간: {self._meas_elapsed_s:.1f} s")
        base_msg = self.lbl_running_status.text().rstrip(".")
        if "측정 진행 중" in base_msg:
            self.lbl_running_status.setText(f"{base_msg}{dots}")

    def emergency_stop(self) -> None:
        nodes = list(range(1, self.spn_nodes.value() + 1))
        self.driver.emergency_abort_all(active_nodes=nodes)
        self._set_measuring_ui_state(False)
        self.lbl_state_pill.setText("● 상태: 완전 정지 / 안전 출력 차단 (STOPPED)")
        self.lbl_state_pill.setStyleSheet("color: #dc2626; font-weight: bold; font-size: 9pt;")
        self.status_bar.showMessage(f"긴급 정지 완료: 전체 노드({nodes}) 출력이 안전하게 OFF 되었습니다.")
        QMessageBox.warning(self, "ALL STOP 실행 완료", f"전체 노드({nodes}) 출력 차단 및 트리거 강제 중단이 완료되었습니다.")

    def execute_quick_single_dc_measurement(self) -> None:
        self._validate_and_clamp_spinbox(self.spn_quick_dc_level, "source_level", "단일 DC 소스 레벨")
        self._validate_and_clamp_spinbox(self.spn_compliance, "compliance", "Compliance Limit")

        p = self.driver.profile
        s_func = SourceFunction(self.cmb_source_func.currentText())
        m_func = MeasureFunction.CURRENT if s_func == SourceFunction.VOLTAGE else MeasureFunction.VOLTAGE
        cfg = ChannelConfig(
            logical_channel=LogicalChannel(node_id=1, channel_name=p.channel_names[0]),
            source_func=s_func,
            measure_func=m_func,
            source_level=self.spn_quick_dc_level.value(),
            compliance_limit=self.spn_compliance.value(),
            nplc=self.spn_nplc.value(),
            sense_mode=SenseMode(self.cmb_sense.currentText()),
            terminals=TerminalLocation(self.cmb_terminals.currentText()),
            measure_range=self.cmb_meas_range.currentData(),
            high_capacitance=self.chk_high_cap.isChecked(),
            output_off_mode=OutputOffMode(self.cmb_offmode.currentText()),
            auto_zero=self.cmb_auto_zero.currentText(),
            source_readback=self.chk_source_readback.isChecked(),
        )
        self._set_measuring_ui_state(True, f"⏳ [{p.model}] 단일 DC ({cfg.source_level:g} {s_func.value}) 인가 및 1회 측정 진행 중")
        try:
            pt = self.driver.measure_single_dc(cfg)
        except Exception as exc:
            self._set_measuring_ui_state(False)
            QMessageBox.critical(self, "단일 DC 소스/측정 오류", str(exc))
            return
        finally:
            self._set_measuring_ui_state(False)

        pt.index = len(self.single_dc_points) + 1
        self.single_dc_points.append(pt)
        r_str = f"{pt.resistance_ohm:.3e} Ω" if pt.resistance_ohm != float("inf") else "INF Ω"
        self.lbl_live_dmm.setText(f"전압(V): {pt.voltage_v:+.5f} V | 전류(I): {pt.current_a:+.4e} A | 저항(R): {r_str}")

        recipe = self._build_current_recipe()
        recipe.name = f"{p.model}_single_dc_log"
        pts_list = list(self.single_dc_points)
        res = MeasurementResult(
            session_id=datetime.now().strftime("%Y%m%d_%H%M%S_DC"),
            model=p.model,
            recipe_name=recipe.name,
            started_at=datetime.now(),
            completed_at=datetime.now(),
            channel_config=cfg,
            recipe=recipe,
            points=pts_list,
            metrics=analyze_measurement_points(pts_list, recipe),
        )
        self._on_measurement_succeeded(res)

    def execute_measurement(self, checked: bool = False) -> None:
        if self._is_measuring:
            return
        self._validate_and_clamp_spinbox(self.spn_start, "source_level", "시작 레벨")
        self._validate_and_clamp_spinbox(self.spn_stop, "source_level", "종료 레벨")
        self._validate_and_clamp_spinbox(self.spn_compliance, "compliance", "Compliance Limit")
        self._sync_logical_channels_from_table()

        p = self.driver.profile
        recipe = self._build_current_recipe()
        p_cfg = self._build_pulse_digitizer_config()
        cfg = ChannelConfig(
            logical_channel=LogicalChannel(node_id=1, channel_name=p.channel_names[0]),
            source_func=recipe.source_func,
            measure_func=recipe.measure_func,
            compliance_limit=self.spn_compliance.value(),
            nplc=self.spn_nplc.value(),
            sense_mode=SenseMode(self.cmb_sense.currentText()),
            terminals=TerminalLocation(self.cmb_terminals.currentText()),
            measure_range=self.cmb_meas_range.currentData(),
            high_capacitance=self.chk_high_cap.isChecked(),
            output_off_mode=OutputOffMode(self.cmb_offmode.currentText()),
            auto_zero=self.cmb_auto_zero.currentText(),
            source_readback=self.chk_source_readback.isChecked(),
            contact_check_enabled=self.chk_contact.isChecked(),
        )
        tot_pts = len(recipe.generate_levels()) * max(1, recipe.repeat_count if recipe.repeat_mode == RepeatMode.FINITE else 1)
        self._set_measuring_ui_state(
            True, f"⏳ [{p.model} | {recipe.sweep_type.value} | 총 {tot_pts:,} pts | NPLC={recipe.nplc:g}] SMU 소스 출력 및 측정 진행 중"
        )

        if not self.driver.is_simulated:
            self._meas_worker = MeasurementWorkerThread(
                engine=self.engine,
                cfg=cfg,
                recipe=recipe,
                node_count=self.spn_nodes.value(),
                specific_variant=self._selected_variant_code(),
                logical_channels=self.logical_channels,
                combined_config=self.combined_config,
                pulse_config=p_cfg,
                sync_mode=self.multi_sync_mode,
                visa_mgr=self.visa_mgr,
            )
            self._meas_worker.finished_ok.connect(self._on_measurement_succeeded)
            self._meas_worker.failed_err.connect(self._on_measurement_failed)
            self._meas_worker.start()
            return

        try:
            res = self.engine.run_measurement(
                cfg=cfg,
                recipe=recipe,
                node_count=self.spn_nodes.value(),
                specific_variant=self._selected_variant_code(),
                logical_channels=self.logical_channels,
                combined_config=self.combined_config,
                pulse_config=p_cfg,
                sync_mode=self.multi_sync_mode,
                visa_mgr=self.visa_mgr,
            )
        except Exception as exc:
            self._on_measurement_failed(str(exc))
            return
        self._on_measurement_succeeded(res)

    def _on_measurement_failed(self, err_msg: str) -> None:
        self._set_measuring_ui_state(False)
        self.last_measurement_error = err_msg
        self.lbl_state_pill.setText("● 상태: 측정 차단 / 오류 발생")
        self.lbl_state_pill.setStyleSheet("color: #dc2626; font-weight: bold;")
        self.spy.record("ERR", self.driver.profile.model, err_msg)
        if not self.suppress_range_popup:
            QMessageBox.critical(self, "Pre-Flight 안전 검증 / 실행 차단", err_msg)

    def _on_measurement_succeeded(self, res: MeasurementResult) -> None:
        self._set_measuring_ui_state(False)
        self.last_result = res
        self.run_history.append(res)
        self._refresh_plot()
        self._populate_table(res)
        self.refresh_soa_chart()
        self._update_kpi_cards(res)
        self._update_diagnosis_banner(res)

        self.lbl_state_pill.setText("● 상태: 측정 완료 / 출력 안전 OFF (DONE)")
        self.lbl_state_pill.setStyleSheet("color: #16a34a; font-weight: bold; font-size: 9pt;")
        self.lbl_sample_count.setText(f"누적 샘플: {len(res.points):,} pts")
        self.status_bar.showMessage(f"{res.model} 측정 완료 ({len(res.points):,} pts) — 출력이 안전하게 OFF 되었습니다.")

        for dwin in list(self.detached_windows):
            if dwin.isVisible():
                dwin.update_data(res, self.run_history)

        if self.chk_auto_export.isChecked():
            auto_csv = self.exporter.export_streaming_csv(res)
            res.warnings.append(f"Auto-Export 완료: {auto_csv.name}")

        m = res.metrics
        vth_s = f"{m['v_threshold_v']:.3f} V" if m.get("v_threshold_v") is not None else "N/A"
        dvdi_s = f"{m['dv_di_median_ohm']:.2f} Ω" if m.get("dv_di_median_ohm") is not None else "N/A"
        self.lbl_metrics.setText(
            f"[측정 완료: {len(res.points):,} pts] V: [{m['v_min']:.3f}~{m['v_max']:.3f}V] | "
            f"I_max: {m['i_max']:.4e}A | Vth: {vth_s} | dV/dI: {dvdi_s} | Compliance: {m['compliance_hit_count']}건"
        )

    def _update_kpi_cards(self, res: MeasurementResult) -> None:
        m = res.metrics
        if not m or m.get("count", 0) == 0:
            return
        self.kpi_voltage.set_value(f"{m['v_min']:.3f} ~ {m['v_max']:.3f} V")
        self.kpi_current.set_value(f"{m['i_max']:.3e} A")
        self.kpi_power.set_value(f"{m.get('p_max_w', 0.0):.3e} W")
        dvdi = m.get("dv_di_median_ohm")
        self.kpi_dvdi.set_value(f"{dvdi:.2e} Ω" if dvdi is not None else "N/A")
        vth = m.get("v_threshold_v")
        self.kpi_vth.set_value(f"{vth:.3f} V" if vth is not None else "N/A")
        ileak = m.get("i_leakage_a")
        self.kpi_ileak.set_value(f"{ileak:.3e} A" if ileak is not None else "N/A")
        rt = m.get("rise_time_10_90_s")
        ov = m.get("overshoot_pct", 0.0)
        self.kpi_pulse.set_value(f"{rt*1e6:.1f}µs / {ov:.1f}%" if rt is not None else f"Ov {ov:.1f}%")
        hits = m.get("compliance_hit_count", 0)
        self.kpi_compliance.set_value(f"TRIPPED ({hits})" if hits > 0 else "NORMAL (0)")

    def _set_meas_range_combo(self, range_val: float | None) -> None:
        if range_val is None:
            self.cmb_meas_range.setCurrentIndex(0)
            return
        for i in range(1, self.cmb_meas_range.count()):
            data_v = self.cmb_meas_range.itemData(i)
            if data_v is not None and abs(float(data_v) - float(range_val)) <= max(1e-9, abs(float(range_val)) * 0.05):
                self.cmb_meas_range.setCurrentIndex(i)
                return
        self.cmb_meas_range.addItem(f"{range_val:g} (고정)", float(range_val))
        self.cmb_meas_range.setCurrentIndex(self.cmb_meas_range.count() - 1)

    def _update_diagnosis_banner(self, res: MeasurementResult) -> None:
        """Run or display the 15-rule Auto-Diagnosis Coach summary on the compact 1-line banner."""
        if res.diagnosis_report is None:
            recent_errs = [e.payload for e in self.spy.entries()[-20:] if e.direction == "ERR"]
            res.diagnosis_report = AutoDiagnosisEngine.diagnose(
                res,
                self.driver.profile,
                is_sink_mode=self.chk_sink_mode.isChecked(),
                interlock_engaged=self.chk_interlock.isChecked(),
                recent_error_messages=recent_errs,
            )
        diag: DiagnosisReport = res.diagnosis_report
        if diag.critical_count > 0:
            top_titles = ", ".join(it.title for it in diag.issues[:2])
            self.lbl_diag_summary.setText(
                f"🛑 자동 진단: 심각 {diag.critical_count}건 · 주의 {diag.warning_count}건 감지 — {top_titles}"
            )
            self.lbl_diag_summary.setStyleSheet("color: #991b1b; font-weight: bold; font-size: 8.6pt; border: none;")
            self.frm_diag_banner.setStyleSheet("QFrame { background-color: #fef2f2; border: 1.5px solid #ef4444; border-radius: 5px; }")
            self.btn_open_diag_popup.setStyleSheet(
                "background-color: #dc2626; color: white; border: none; font-weight: bold; padding: 3px 10px; border-radius: 4px;"
            )
        elif diag.warning_count > 0:
            top_titles = ", ".join(it.title for it in diag.issues[:2])
            self.lbl_diag_summary.setText(
                f"⚠ 자동 진단: 주의 {diag.warning_count}건 감지 — {top_titles}"
            )
            self.lbl_diag_summary.setStyleSheet("color: #92400e; font-weight: bold; font-size: 8.6pt; border: none;")
            self.frm_diag_banner.setStyleSheet("QFrame { background-color: #fffbeb; border: 1.5px solid #f59e0b; border-radius: 5px; }")
            self.btn_open_diag_popup.setStyleSheet(
                "background-color: #d97706; color: white; border: none; font-weight: bold; padding: 3px 10px; border-radius: 4px;"
            )
        else:
            self.lbl_diag_summary.setText(
                f"✅ 자동 진단 정상 — [{diag.model}] 15대 셋업·품질 항목 모두 통과 (클릭하여 상세 보기)"
            )
            self.lbl_diag_summary.setStyleSheet("color: #065f46; font-weight: bold; font-size: 8.6pt; border: none;")
            self.frm_diag_banner.setStyleSheet("QFrame { background-color: #ecfdf5; border: 1px solid #34d399; border-radius: 5px; }")
            self.btn_open_diag_popup.setStyleSheet(
                "background-color: #16a34a; color: white; border: none; font-weight: bold; padding: 3px 10px; border-radius: 4px;"
            )

    def apply_diagnosis_autofix(self, item: DiagnosisItem) -> str:
        """Apply 1-click Auto-Fix recommended by the Auto-Diagnosis Coach directly to GUI inputs."""
        act = item.fix_action_id
        params = item.fix_params
        msg = "권장 보정이 적용되었습니다."

        if act == "RAISE_COMPLIANCE" and "compliance_limit" in params:
            new_lim = float(params["compliance_limit"])
            self.spn_compliance.setValue(new_lim)
            msg = f"Compliance Limit을 {new_lim:g}(으)로 상향 적용했습니다."
        elif act == "SET_TERMINALS" and "terminals" in params:
            self.cmb_terminals.setCurrentText(str(params["terminals"]))
            msg = f"출력 단자를 {params['terminals']}로 전환했습니다."
        elif act in ("ENABLE_4WIRE", "ENABLE_2WIRE") and "sense_mode" in params:
            self.cmb_sense.setCurrentText(str(params["sense_mode"]))
            msg = f"배선 모드를 {params['sense_mode']}로 전환했습니다."
        elif act == "SET_NPLC" and "nplc" in params:
            self.spn_nplc.setValue(float(params["nplc"]))
            msg = f"적분 시간(NPLC)을 {params['nplc']:g}로 상향 적용했습니다."
        elif act in ("SET_FIXED_RANGE", "SET_AUTO_RANGE"):
            self._set_meas_range_combo(params.get("measure_range"))
            rng_lbl = "AUTO" if params.get("measure_range") is None else f"{params['measure_range']:g}"
            msg = f"측정 레인지를 {rng_lbl}(으)로 변경했습니다."
        elif act == "SET_AUTOZERO" and "auto_zero" in params:
            self.cmb_auto_zero.setCurrentText(str(params["auto_zero"]))
            msg = f"AutoZero를 {params['auto_zero']}(으)로 변경했습니다."
        elif act == "INCREASE_DELAY" and "delay_s" in params:
            self.spn_delay.setValue(float(params["delay_s"]))
            msg = f"스텝 지연(Delay)을 {params['delay_s']:g}s로 증가 적용했습니다."
        elif act == "ENABLE_HIGH_CAP":
            self.chk_high_cap.setChecked(True)
            msg = "High-Capacitance 모드를 활성화했습니다."
        elif act == "OPTIMIZE_SPEED":
            if "auto_zero" in params:
                self.cmb_auto_zero.setCurrentText(str(params["auto_zero"]))
            if "source_readback" in params:
                self.chk_source_readback.setChecked(bool(params["source_readback"]))
            msg = "고속 측정 최적화(AutoZero=ONCE, Source Readback=OFF)를 적용했습니다."

        self.status_bar.showMessage(f"[자동 보정 완료] {msg}")
        self.spy.record("SYS", self.driver.profile.model, f"자동 측정 코치 보정 적용 ({item.rule_id}): {msg}")
        return msg

    def open_diagnosis_popup(self) -> None:
        """Open the interactive Auto-Measurement Coach popup with 15-rule results & 1-click Auto-Fix."""
        if self.last_result is None:
            QMessageBox.information(
                self,
                "SMU 자동 측정 코치",
                "아직 완료된 측정 데이터가 없습니다.\n[▶ 측정 시작 (Run)]을 실행하면 15대 셋업·품질 항목이 자동으로 진단됩니다.",
            )
            return
        if self.last_result.diagnosis_report is None:
            self._update_diagnosis_banner(self.last_result)
        assert self.last_result.diagnosis_report is not None
        dlg = AutoDiagnosisDialog(
            report=self.last_result.diagnosis_report,
            apply_fix_callback=self.apply_diagnosis_autofix,
            parent=self,
        )
        dlg.exec()

    def _current_plot_xy(self, pts) -> tuple[np.ndarray, np.ndarray, bool]:
        is_time = self.cmb_plot_view.currentIndex() == 1
        x = np.array([p.timestamp_s if is_time else p.voltage_v for p in pts], dtype=np.float64)
        y = np.array([p.current_a for p in pts], dtype=np.float64)
        if self.chk_log_y.isChecked():
            y = np.log10(np.maximum(np.abs(y), 1e-15))
        return x, y, is_time

    def _refresh_plot(self) -> None:
        self.plot_iv.clear()
        if not self.last_result or not self.last_result.points:
            return

        if len(self.run_history) >= 2:
            prev = self.run_history[-2]
            px, py, _ = self._current_plot_xy(prev.points)
            dx, dy = lttb_downsample(px, py, threshold=1500)
            self.plot_iv.plot(dx, dy, pen=pg.mkPen(color="#64748b", width=1.4, style=Qt.PenStyle.DashLine), name="Prev Run")

        pts = self.last_result.points
        x_all, y_all, is_time = self._current_plot_xy(pts)
        self.plot_iv.setLabel("bottom", "<span style='color:#cbd5e1;'>Time</span>" if is_time else "<span style='color:#cbd5e1;'>Voltage</span>", units="s" if is_time else "V")
        self.plot_iv.setLabel("left", "<span style='color:#cbd5e1;'>log10(|I|)</span>" if self.chk_log_y.isChecked() else "<span style='color:#cbd5e1;'>Current</span>", units="log A" if self.chk_log_y.isChecked() else "A")

        # Group by Node.Channel for crisp multi-channel neon plotting
        grouped: dict[str, list] = {}
        for p in pts:
            grouped.setdefault(f"Node{p.node_id}.{p.channel}", []).append(p)

        for idx_c, (ch_key, ch_pts) in enumerate(grouped.items()):
            cx, cy, _ = self._current_plot_xy(ch_pts)
            dx, dy = lttb_downsample(cx, cy, threshold=2000)
            col = self._SERIES_COLORS[idx_c % len(self._SERIES_COLORS)]
            self.plot_iv.plot(
                dx,
                dy,
                pen=pg.mkPen(color=col, width=2.4),
                symbol="o",
                symbolSize=5,
                symbolBrush=col,
                symbolPen=pg.mkPen("#0b132b", width=0.5),
                name=f"{ch_key} ({len(ch_pts)} pts)",
            )

        if self.chk_dual_cursors.isChecked() and len(x_all) >= 2:
            x_min, x_max = float(np.min(x_all)), float(np.max(x_all))
            span = max(1e-9, x_max - x_min)
            self.cursor_line1.setPos(x_min + 0.25 * span)
            self.cursor_line2.setPos(x_min + 0.75 * span)
            self.plot_iv.addItem(self.cursor_line1)
            self.plot_iv.addItem(self.cursor_line2)
            self._update_cursor_readout()

    def _update_cursor_readout(self) -> None:
        if not self.last_result or not self.last_result.points:
            return
        x, y, is_time = self._current_plot_xy(self.last_result.points)
        c = compute_dual_cursor_metrics(x, y, float(self.cursor_line1.value()), float(self.cursor_line2.value()))
        xu = "s" if is_time else "V"
        self.lbl_cursor_readout.setText(
            f"[커서] C1=({c.x1:.4g}{xu}, {c.y1:.3e}A) | C2=({c.x2:.4g}{xu}, {c.y2:.3e}A) | "
            f"ΔX={c.delta_x:.4g}{xu}, ΔY={c.delta_y:.3e}A | 1/Slope={c.inverse_slope_ohm:.3e} Ω"
        )

    def _populate_table(self, res: MeasurementResult) -> None:
        display_pts = res.points[:2000]
        comp_bg = QColor("#fef3c7")

        if display_pts:
            last_p = display_pts[-1]
            r_str = f"{last_p.resistance_ohm:.3e} Ω" if last_p.resistance_ohm != float("inf") else "INF Ω"
            self.lbl_live_dmm.setText(
                f"전압(V): {last_p.voltage_v:+.5f} V | 전류(I): {last_p.current_a:+.4e} A | 저항(R): {r_str}"
            )

        # Tab 1 bottom spreadsheet table
        self.tbl_live_data.setRowCount(len(display_pts))
        for r, p in enumerate(display_pts):
            live_vals = [
                str(p.index),
                f"N{p.node_id}.{p.channel}",
                f"{p.timestamp_s:.4f}",
                f"{p.voltage_v:+.5f}",
                f"{p.current_a:+.5e}",
                f"{p.resistance_ohm:.3e}" if p.resistance_ohm != float("inf") else "INF",
                f"{p.power_w:+.4e}",
            ]
            for c, text in enumerate(live_vals):
                it = QTableWidgetItem(text)
                if p.in_compliance:
                    it.setBackground(comp_bg)
                self.tbl_live_data.setItem(r, c, it)

        # Tab 5 full buffer table
        self.table.setRowCount(len(display_pts))
        for r, p in enumerate(display_pts):
            status_str = "COMPLIANCE" if p.in_compliance else ("PASS" if p.limit_passed else "FAIL")
            vals = [
                str(p.index),
                f"N{p.node_id}.{p.channel}",
                f"{p.timestamp_s:.6f}",
                f"{p.voltage_v:.5f}",
                f"{p.current_a:.6e}",
                f"{p.resistance_ohm:.3e}" if p.resistance_ohm != float("inf") else "INF",
                f"{p.power_w:.5e}",
                status_str,
            ]
            for c, text in enumerate(vals):
                item = QTableWidgetItem(text)
                if p.in_compliance:
                    item.setBackground(comp_bg)
                self.table.setItem(r, c, item)

    # --- Persistent Export Folder & Multi-Format Export ---
    def choose_export_directory(self) -> Path | None:
        chosen = QFileDialog.getExistingDirectory(
            self,
            "리포트 및 측정 데이터 저장 폴더 선택 (선택한 위치는 계속 유지됩니다)",
            str(self.exporter.output_dir),
        )
        if not chosen:
            return None
        return self.set_export_directory(chosen, persist=True)

    def set_export_directory(self, folder_path: Path | str, persist: bool = True) -> Path:
        new_dir = self.exporter.set_output_dir(folder_path, persist=persist)
        if hasattr(self, "txt_export_dir"):
            self.txt_export_dir.setText(str(new_dir))
            self.txt_export_dir.setToolTip(f"현재 파일 저장 폴더: {new_dir}")
        self.spy.record("SYS", self.driver.profile.model, f"파일 저장 폴더 위치 설정 및 기억 완료: {new_dir}")
        return new_dir

    def export_excel(self) -> None:
        if not self.last_result:
            QMessageBox.information(self, "데이터 없음", "먼저 측정을 실행해 주세요.")
            return
        try:
            idn_str = self.driver.identify()
        except Exception:
            idn_str = f"KEITHLEY INSTRUMENTS,MODEL {self.last_result.model}"
        path = self.exporter.export_excel_report(
            self.last_result,
            resource_addr=self.active_resource_name,
            is_simulation=self.driver.is_simulated,
            idn_string=idn_str,
            checklist_status=self.last_checklist_status or None,
        )
        QMessageBox.information(
            self,
            "Tek QuickStart 양식 Excel 리포트 저장 완료",
            f"체크리스트 // 입력 설정값 // 측정 데이터(+통계) // 그래프 시트가 포함된 Excel 리포트가 생성되었습니다:\n{path}",
        )

    def export_history_comparison(self) -> None:
        if not self.run_history:
            QMessageBox.information(self, "데이터 없음", "비교할 Run History 측정 이력이 없습니다.")
            return
        path = self.exporter.export_run_history_comparison_excel(self.run_history)
        QMessageBox.information(self, "Run History 비교 리포트 완료", f"총 {len(self.run_history)}회 실행 비교 Excel 리포트가 저장되었습니다:\n{path}")

    def export_csv(self) -> None:
        if not self.last_result:
            QMessageBox.information(self, "데이터 없음", "먼저 측정을 실행해 주세요.")
            return
        path = self.exporter.export_streaming_csv(self.last_result)
        QMessageBox.information(self, "CSV 저장 완료", f"스트리밍 CSV 파일이 저장되었습니다:\n{path}")

    def export_parquet(self) -> None:
        if not self.last_result:
            QMessageBox.information(self, "데이터 없음", "먼저 측정을 실행해 주세요.")
            return
        pq_path = self.exporter.export_parquet(self.last_result)
        db_path = self.exporter.export_sqlite_archive(self.last_result)
        QMessageBox.information(self, "Parquet & SQLite 저장 완료", f"고속 아카이브 파일이 생성되었습니다:\n• Parquet: {pq_path}\n• SQLite: {db_path}")

    def export_plot_image(self) -> None:
        if not self.last_result:
            QMessageBox.information(self, "데이터 없음", "먼저 측정을 실행해 주세요.")
            return
        mode = "TIME" if self.cmb_plot_view.currentIndex() == 1 else "IV"
        png_path = self.exporter.export_plot_image(self.last_result, fmt="png", mode=mode, history_overlays=self.run_history[:-1])
        svg_path = self.exporter.export_plot_image(self.last_result, fmt="svg", mode=mode, history_overlays=self.run_history[:-1])
        QMessageBox.information(self, "그래프 이미지 저장 완료", f"PNG 및 SVG 고해상도 그래프가 저장되었습니다:\n• PNG: {png_path}\n• SVG: {svg_path}")


def launch_app(show_startup_wizard: bool = True) -> int:
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication.instance() or QApplication(sys.argv)
    win = SMUMainWindow(show_startup_wizard=show_startup_wizard)
    win.show_adaptive()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(launch_app())
