"""PyQt6 + PyQtGraph Integrated SMU Desktop Control & Analysis Dashboard (v3.0 + All 9 UX Upgrades).

Includes all Phase 1~3 capabilities plus the 9 User Experience upgrades:
1. TEK_quickstart-style Beginner Connection Wizard & Multi-SMU (1~10) per-unit communication test + checklist (`SMUConnectionWizardDialog`).
2. Arbitrary Waveform Editor step deletion (`- 선택 스텝 삭제`, `🗑 전체 초기화`) as well as addition.
3. Easy Parametric Waveform Generator (`EasyWaveformShape`: Square/Pulse/Triangle/Staircase/Sine + Level + Duration + Repeat Count `x100` or `-1` Infinite Continuous).
4. Detachable / Pop-out Graph Window (`🗗 그래프 새 창으로 분리`, multiple independent windows supported).
5. Automatic Screen Resolution Adaptation (`fit_and_center_window`, `show_adaptive`).
6. Direct Reference Manual PDF icon button (`📖 매뉴얼 보기`) for the selected SMU model.
7. Per-Instrument / Per-Channel Independent Waveform Mode & Source Function (`VOLTAGE` vs `CURRENT`) separation when 2+ instruments/channels are connected.
8. Out-of-Range Auto-Clamping with User Notification (`clamp_value_with_notice`): alerts user and clamps input to model maximum/minimum.
9. One-Click Full Setup Save & Load (`💾 설정 저장` / `📂 설정 불러오기`) + automatic last-session persistence.
"""

from __future__ import annotations

import sys
from pathlib import Path
import numpy as np

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QCloseEvent, QColor, QFont
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
import pyqtgraph as pg

from smu_app.analysis.iv_pulse_analyzer import (
    compute_dual_cursor_metrics,
    lttb_downsample,
)
from smu_app.core.command_spy import CommandSpy, SpyEntry
from smu_app.core.models import (
    ChannelConfig,
    LogicalChannel,
    MeasureFunction,
    MeasurementResult,
    OutputOffMode,
    RepeatMode,
    SenseMode,
    SourceFunction,
    SweepRecipe,
    SweepType,
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


class SMUMainWindow(QMainWindow):
    """Main Desktop GUI for Tektronix/Keithley 8-Model SMU Control (Phase 1~3 + 9 UX Upgrades)."""

    def __init__(self, show_startup_wizard: bool = False, prefer_simulator: bool = False) -> None:
        super().__init__()
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
        self.combined_config: CombinedOutputConfig | None = None
        self.last_acal_temp_c: float = 23.0
        self.detached_windows: list[DetachedGraphWindow] = []
        self.suppress_range_popup: bool = False
        self.last_range_notice: str = ""
        self._geometry_locked: bool = False

        # Requirement 5: Automatic screen resolution adaptation (locked once at startup so measurement never resizes window)
        avail = get_available_screen_geometry(self)
        self.is_compact_screen = avail.width() <= 1600 or avail.height() <= 920
        self.setWindowTitle(
            f"SAITEK Tektronix/Keithley SMU Advanced Control Suite v3.0 ({avail.width()}×{avail.height()})"
        )

        self._build_ui()
        fit_and_center_window(
            self,
            target_w_ratio=0.95 if self.is_compact_screen else 0.90,
            target_h_ratio=0.94 if self.is_compact_screen else 0.90,
            min_w=1024,
            min_h=640,
        )
        self._geometry_locked = True

        self.spy.subscribe(self._on_spy_entry)
        self._on_model_changed("2450")
        self._populate_waveform_table()
        self.refresh_waveform_preview()
        self.refresh_soa_chart()

        # Requirement 9: Auto-restore last saved session setup if present
        if SetupPersistenceManager.DEFAULT_LAST_SESSION_PATH.exists():
            try:
                self.load_setup_from_path(SetupPersistenceManager.DEFAULT_LAST_SESSION_PATH, silent=True)
            except Exception:
                pass

        if show_startup_wizard:
            QTimer.singleShot(120, self.open_connection_wizard)

    def show_adaptive(self) -> None:
        """Requirement 5: Maximize on compact screens or display fitted & centered on high-res monitors."""
        avail = get_available_screen_geometry(self)
        if avail.width() <= 1600 or avail.height() <= 920:
            self.showMaximized()
        else:
            self.show()

    def closeEvent(self, event: QCloseEvent) -> None:
        """Requirement 9: Automatically save current instrument & parameter setup on exit."""
        try:
            self.save_setup_to_path(SetupPersistenceManager.DEFAULT_LAST_SESSION_PATH, silent=True)
        except Exception:
            pass
        super().closeEvent(event)

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)
        m = 6 if self.is_compact_screen else 10
        root_layout.setContentsMargins(m, m, m, m)
        root_layout.setSpacing(6)

        # 1. Global Top Bar (Connection Wizard, Model, Variant, Manual Icon, Nodes 1~10, Save/Load Setup, ALL STOP)
        top_bar = QHBoxLayout()
        top_bar.setSpacing(6)

        self.btn_conn_wizard = QPushButton("🔌 장비 연결 & 통신 점검")
        self.btn_conn_wizard.setToolTip("TEK_quickstart 스타일 초보자 연결 마법사 및 다중 장비 개별 통신 테스트를 엽니다.")
        self.btn_conn_wizard.setStyleSheet(
            "background-color: #0b2545; color: #38bdf8; font-weight: bold; padding: 6px 10px; border-radius: 4px;"
        )
        self.btn_conn_wizard.clicked.connect(self.open_connection_wizard)
        top_bar.addWidget(self.btn_conn_wizard)

        top_bar.addWidget(QLabel("<b>모델:</b>"))
        self.cmb_model = QComboBox()
        self.cmb_model.addItems(self.registry.list_models())
        self.cmb_model.currentTextChanged.connect(self._on_model_changed)
        top_bar.addWidget(self.cmb_model)

        self.btn_manual = QPushButton("📖 매뉴얼(PDF)")
        self.btn_manual.setToolTip("선택된 SMU 모델의 프로그래밍 레퍼런스 매뉴얼(PDF)을 즉시 엽니다.")
        self.btn_manual.setStyleSheet(
            "background-color: #e0f2fe; color: #0369a1; border: 1px solid #7dd3fc; font-weight: bold; padding: 5px 9px; border-radius: 4px;"
        )
        self.btn_manual.clicked.connect(self.open_current_model_manual)
        top_bar.addWidget(self.btn_manual)

        top_bar.addWidget(QLabel("<b>세부모델:</b>"))
        self.cmb_variant = QComboBox()
        self.cmb_variant.currentTextChanged.connect(self._update_sync_warning_banner)
        top_bar.addWidget(self.cmb_variant)

        top_bar.addWidget(QLabel("<b>대수(1~10):</b>"))
        self.spn_nodes = QSpinBox()
        self.spn_nodes.setRange(1, 10)
        self.spn_nodes.setValue(1)
        self.spn_nodes.valueChanged.connect(self._on_nodes_changed)
        top_bar.addWidget(self.spn_nodes)

        self.btn_combining = QPushButton("⚡ 출력 결합")
        self.btn_combining.setStyleSheet("background-color: #0E7490; color: white; font-weight: bold; padding: 6px 9px;")
        self.btn_combining.clicked.connect(self.open_combining_wizard)
        top_bar.addWidget(self.btn_combining)

        self.btn_save_setup = QPushButton("💾 설정 저장")
        self.btn_save_setup.setToolTip("현재 장비, 다중 채널, 파형 및 펄스 설정 전체를 파일로 저장합니다.")
        self.btn_save_setup.clicked.connect(self.save_setup_dialog)
        top_bar.addWidget(self.btn_save_setup)

        self.btn_load_setup = QPushButton("📂 설정 불러오기")
        self.btn_load_setup.setToolTip("저장해 둔 장비 및 파형 설정 파일을 불러와 즉시 복원합니다.")
        self.btn_load_setup.clicked.connect(self.load_setup_dialog)
        top_bar.addWidget(self.btn_load_setup)

        top_bar.addWidget(QLabel("<b>DUT:</b>"))
        self.cmb_dut = QComboBox()
        for d in VirtualDUTType:
            self.cmb_dut.addItem(d.value)
        self.cmb_dut.currentTextChanged.connect(self._on_dut_changed)
        top_bar.addWidget(self.cmb_dut)

        self.chk_interlock = QCheckBox("Interlock(>42V)")
        self.chk_interlock.setChecked(True)
        self.chk_interlock.toggled.connect(self._on_interlock_toggled)
        top_bar.addWidget(self.chk_interlock)

        top_bar.addWidget(QLabel("<b>VISA 연결:</b>"))
        self.cmb_visa_bar = QComboBox()
        self.cmb_visa_bar.setEditable(True)
        self.cmb_visa_bar.setMinimumWidth(180)
        self.cmb_visa_bar.setMaximumWidth(280)
        self.cmb_visa_bar.currentIndexChanged.connect(self._on_top_visa_resource_changed)
        top_bar.addWidget(self.cmb_visa_bar)

        self.lbl_spec_badge = QLabel()
        self.lbl_spec_badge.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.lbl_spec_badge.setStyleSheet(
            "background-color: #EFF6FF; color: #0B1F3A; padding: 4px 8px; border-radius: 4px; font-weight: bold;"
        )
        top_bar.addWidget(self.lbl_spec_badge, stretch=1)

        self.btn_run = QPushButton("▶ 스위프/파형 측정 시작 (Run)")
        self.btn_run.setStyleSheet(
            "background-color: #0070C0; color: white; font-weight: bold; font-size: 12.5px; padding: 7px 14px;"
        )
        self.btn_run.clicked.connect(self.execute_measurement)
        top_bar.addWidget(self.btn_run)

        self.btn_all_stop = QPushButton("■ 긴급 정지 (ALL STOP)")
        self.btn_all_stop.setStyleSheet(
            "background-color: #DC2626; color: white; font-weight: bold; font-size: 12.5px; padding: 7px 14px;"
        )
        self.btn_all_stop.clicked.connect(self.emergency_stop)
        top_bar.addWidget(self.btn_all_stop)

        root_layout.addLayout(top_bar)

        self.lbl_warning_banner = QLabel()
        self.lbl_warning_banner.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.lbl_warning_banner.setStyleSheet(
            "background-color: #FFF7ED; color: #C2410C; border: 1px solid #FB923C; padding: 6px; font-weight: bold;"
        )
        self.lbl_warning_banner.setVisible(False)
        root_layout.addWidget(self.lbl_warning_banner)

        # 2. Main Splitter
        main_splitter = QSplitter(Qt.Orientation.Horizontal)

        left_panel = QWidget()
        left_panel.setMinimumWidth(340)
        left_panel.setMaximumWidth(430)
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 6, 0)

        # Quick Single DC Source & Measure Box (Direct 2450 Source + Simultaneous V/I/R/P Measurement)
        grp_quick_dc = QGroupBox("⚡ 0. 단일 DC 즉시 소스 인가 & 측정 (Quick Source & Measure)")
        grp_quick_dc.setStyleSheet("QGroupBox { font-weight: bold; color: #0070C0; }")
        qdc_layout = QVBoxLayout(grp_quick_dc)
        qdc_row = QHBoxLayout()
        qdc_row.addWidget(QLabel("인가 값(V/A):"))
        self.spn_quick_dc_level = QDoubleSpinBox()
        self.spn_quick_dc_level.setDecimals(4)
        self.spn_quick_dc_level.setRange(-10000.0, 10000.0)
        self.spn_quick_dc_level.setValue(1.0)
        self.spn_quick_dc_level.editingFinished.connect(
            lambda: self._validate_and_clamp_spinbox(self.spn_quick_dc_level, "source_level", "단일 DC 소스 레벨")
        )
        qdc_row.addWidget(self.spn_quick_dc_level, 1)

        self.btn_quick_dc = QPushButton("⚡ 즉시 소스 인가 & 1회 측정")
        self.btn_quick_dc.setStyleSheet(
            "background-color: #16A34A; color: white; font-weight: bold; padding: 6px 10px; border-radius: 4px;"
        )
        self.btn_quick_dc.clicked.connect(self.execute_quick_single_dc_measurement)
        qdc_row.addWidget(self.btn_quick_dc)
        qdc_layout.addLayout(qdc_row)

        self.lbl_live_dmm = QLabel("전압(V): ---   |   전류(I): ---   |   저항(R): ---   |   전력(P): ---")
        self.lbl_live_dmm.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.lbl_live_dmm.setStyleSheet(
            "background-color: #0B1F3A; color: #38BDF8; font-family: Consolas, '맑은 고딕'; font-size: 9.5pt; "
            "font-weight: bold; padding: 6px; border-radius: 4px;"
        )
        qdc_layout.addWidget(self.lbl_live_dmm)
        left_layout.addWidget(grp_quick_dc)

        grp_sweep = QGroupBox("1. 파형 / 스위프 & 반복 제어 (초과 입력 시 최댓값 자동 보정)")
        form_swp = QFormLayout(grp_sweep)

        self.cmb_sweep_type = QComboBox()
        for st in SweepType:
            self.cmb_sweep_type.addItem(st.value)
        self.cmb_sweep_type.currentTextChanged.connect(self._on_sweep_param_changed)
        form_swp.addRow("파형 모드:", self.cmb_sweep_type)

        self.cmb_source_func = QComboBox()
        self.cmb_source_func.addItems([SourceFunction.VOLTAGE.value, SourceFunction.CURRENT.value])
        self.cmb_source_func.currentTextChanged.connect(self._on_sweep_param_changed)
        form_swp.addRow("소스(인가) 기능:", self.cmb_source_func)

        # Wide physical spinbox range so user can type out-of-range numbers and get Requirement 8 notification + auto-clamp
        self.spn_start = QDoubleSpinBox()
        self.spn_start.setDecimals(4)
        self.spn_start.setRange(-10000.0, 10000.0)
        self.spn_start.setValue(0.0)
        self.spn_start.editingFinished.connect(lambda: self._validate_and_clamp_spinbox(self.spn_start, "source_level", "시작/바이어스 레벨"))
        self.spn_start.valueChanged.connect(self._on_sweep_param_changed)
        form_swp.addRow("시작/바이어스 (Start V/A):", self.spn_start)

        self.spn_stop = QDoubleSpinBox()
        self.spn_stop.setDecimals(4)
        self.spn_stop.setRange(-10000.0, 10000.0)
        self.spn_stop.setValue(1.2)
        self.spn_stop.editingFinished.connect(lambda: self._validate_and_clamp_spinbox(self.spn_stop, "source_level", "종료/펄스 레벨"))
        self.spn_stop.valueChanged.connect(self._on_sweep_param_changed)
        form_swp.addRow("종료/펄스 레벨 (Stop V/A):", self.spn_stop)

        self.spn_points = QSpinBox()
        self.spn_points.setRange(2, 1000000)
        self.spn_points.setValue(21)
        self.spn_points.valueChanged.connect(self._on_sweep_param_changed)
        form_swp.addRow("측정 포인트 수:", self.spn_points)

        self.spn_delay = QDoubleSpinBox()
        self.spn_delay.setDecimals(5)
        self.spn_delay.setRange(0.00001, 60.0)
        self.spn_delay.setValue(0.005)
        self.spn_delay.valueChanged.connect(self._on_sweep_param_changed)
        form_swp.addRow("스텝 지연 (Delay s):", self.spn_delay)

        self.cmb_repeat_mode = QComboBox()
        for rm in RepeatMode:
            self.cmb_repeat_mode.addItem(rm.value)
        self.cmb_repeat_mode.currentTextChanged.connect(self._on_sweep_param_changed)
        form_swp.addRow("반복 출력 모드:", self.cmb_repeat_mode)

        self.spn_repeat_count = QSpinBox()
        self.spn_repeat_count.setRange(-1, 100000)
        self.spn_repeat_count.setValue(1)
        self.spn_repeat_count.setToolTip("1: 단발 출력 | 2 이상(예: 100): N회 반복 출력 | -1: 무한 연속 출력")
        self.spn_repeat_count.valueChanged.connect(self._on_repeat_count_spin_changed)
        form_swp.addRow("반복 개수 (-1=무한연속):", self.spn_repeat_count)

        self.chk_return_bias = QCheckBox("종료 시 Idle(Bias 0V) 복귀 (해제 시 Hold)")
        self.chk_return_bias.setChecked(True)
        self.chk_return_bias.toggled.connect(self._on_sweep_param_changed)
        form_swp.addRow(self.chk_return_bias)

        self.spn_compliance = QDoubleSpinBox()
        self.spn_compliance.setDecimals(5)
        self.spn_compliance.setRange(1e-7, 10000.0)
        self.spn_compliance.setValue(0.1)
        self.spn_compliance.editingFinished.connect(lambda: self._validate_and_clamp_spinbox(self.spn_compliance, "compliance", "Compliance Limit"))
        self.spn_compliance.valueChanged.connect(self._on_sweep_param_changed)
        form_swp.addRow("Compliance Limit (A/V):", self.spn_compliance)

        self.spn_nplc = QDoubleSpinBox()
        self.spn_nplc.setDecimals(3)
        self.spn_nplc.setRange(0.0001, 100.0)
        self.spn_nplc.setValue(0.1)
        self.spn_nplc.editingFinished.connect(lambda: self._validate_and_clamp_spinbox(self.spn_nplc, "nplc", "적분 시간(NPLC)"))
        form_swp.addRow("적분 시간 (NPLC):", self.spn_nplc)

        left_layout.addWidget(grp_sweep)

        grp_safety = QGroupBox("2. 정밀도 & 안전 보호 (Safety & Precision)")
        form_saf = QFormLayout(grp_safety)

        self.cmb_sense = QComboBox()
        self.cmb_sense.addItems([SenseMode.WIRE_2.value, SenseMode.WIRE_4.value])
        form_saf.addRow("배선 모드 (2/4-Wire):", self.cmb_sense)

        self.cmb_offmode = QComboBox()
        for om in OutputOffMode:
            self.cmb_offmode.addItem(om.value)
        form_saf.addRow("Output-Off 상태:", self.cmb_offmode)

        self.chk_contact = QCheckBox("4-Wire Contact Check 수행")
        form_saf.addRow(self.chk_contact)

        self.chk_neg_zero = QCheckBox("26xx 음수 전환 지연(-1e-12) 자동 보정")
        self.chk_neg_zero.setChecked(True)
        form_saf.addRow(self.chk_neg_zero)

        left_layout.addWidget(grp_safety)

        grp_exp = QGroupBox("3. 대용량 데이터 저장 & 리포트 (Export)")
        exp_layout = QVBoxLayout(grp_exp)
        self.chk_auto_export = QCheckBox("측정 완료 시 자동 백업 (Auto-Export CSV)")
        self.chk_auto_export.setChecked(False)
        exp_layout.addWidget(self.chk_auto_export)

        btn_xlsx = QPushButton("📊 구조화 Excel (.xlsx + 차트) 저장")
        btn_xlsx.clicked.connect(self.export_excel)
        btn_hist_xlsx = QPushButton("📚 Run History 다중 비교 Excel 리포트")
        btn_hist_xlsx.clicked.connect(self.export_history_comparison)
        btn_csv = QPushButton("📄 대용량 스트리밍 CSV 저장")
        btn_csv.clicked.connect(self.export_csv)
        btn_pq = QPushButton("⚡ 고속 Parquet / SQLite 아카이브 저장")
        btn_pq.clicked.connect(self.export_parquet)
        btn_img = QPushButton("🖼 그래프 이미지 (PNG / SVG) 저장")
        btn_img.clicked.connect(self.export_plot_image)

        for b in (btn_xlsx, btn_hist_xlsx, btn_csv, btn_pq, btn_img):
            exp_layout.addWidget(b)
        left_layout.addWidget(grp_exp)
        left_layout.addStretch(1)

        main_splitter.addWidget(left_panel)

        # Right Panel: 5 Tabs + Command Spy Console
        right_splitter = QSplitter(Qt.Orientation.Vertical)
        self.tabs = QTabWidget()

        # --- TAB 1: Real-time LTTB Graph, Dual Cursors & Detach Pop-out Button ---
        graph_tab = QWidget()
        g_layout = QVBoxLayout(graph_tab)

        g_toolbar = QHBoxLayout()
        g_toolbar.addWidget(QLabel("<b>그래프 뷰 모드:</b>"))
        self.cmb_plot_view = QComboBox()
        self.cmb_plot_view.addItems(["I-V Characteristic (V vs I)", "Time-Domain Pulse/Waveform (Time vs I & V)"])
        self.cmb_plot_view.currentTextChanged.connect(self._refresh_plot)
        g_toolbar.addWidget(self.cmb_plot_view)

        self.chk_log_y = QCheckBox("Y축 Log(|I|) 스케일")
        self.chk_log_y.toggled.connect(self._refresh_plot)
        g_toolbar.addWidget(self.chk_log_y)

        self.chk_dual_cursors = QCheckBox("듀얼 커서 (ΔX, ΔY, 1/R)")
        self.chk_dual_cursors.setChecked(True)
        self.chk_dual_cursors.toggled.connect(self._refresh_plot)
        g_toolbar.addWidget(self.chk_dual_cursors)

        self.lbl_cursor_readout = QLabel("커서 대기 중")
        self.lbl_cursor_readout.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.lbl_cursor_readout.setStyleSheet(
            "background-color: #EFF6FF; color: #0B1F3A; padding: 4px 8px; font-family: Consolas, '맑은 고딕'; font-weight: bold;"
        )
        g_toolbar.addWidget(self.lbl_cursor_readout, stretch=1)

        self.btn_clear_data = QPushButton("🧹 그래프·데이터 표 전체 초기화")
        self.btn_clear_data.setToolTip("현재 그래프, 측정 데이터 표, 단일 DC 누적 기록, 이전 실행 이력(Run History) 및 장비 내부 버퍼를 모두 깨끗하게 비웁니다.")
        self.btn_clear_data.setStyleSheet(
            "background-color: #F1F5F9; color: #0F172A; border: 1px solid #94A3B8; font-weight: bold; padding: 5px 10px; border-radius: 4px;"
        )
        self.btn_clear_data.clicked.connect(self.clear_graph_and_table_data)
        g_toolbar.addWidget(self.btn_clear_data)

        self.btn_detach_graph = QPushButton("🗗 그래프 새 창으로 분리 (Pop-out)")
        self.btn_detach_graph.setStyleSheet(
            "background-color: #0E7490; color: white; font-weight: bold; padding: 5px 12px; border-radius: 4px;"
        )
        self.btn_detach_graph.clicked.connect(self.open_detached_graph_window)
        g_toolbar.addWidget(self.btn_detach_graph)

        g_layout.addLayout(g_toolbar)

        # Side-by-side Splitter inside Tab 1: Left = Real-time Plot | Right = Live Measurement Data Table
        tab1_splitter = QSplitter(Qt.Orientation.Horizontal)

        pg.setConfigOptions(antialias=True, background="w", foreground="k")
        self.plot_iv = pg.PlotWidget(title="I-V / Time-Domain Characteristic Curve (LTTB Downsampled)")
        self.plot_iv.setLabel("bottom", "Voltage", units="V")
        self.plot_iv.setLabel("left", "Current", units="A")
        self.plot_iv.showGrid(x=True, y=True, alpha=0.3)
        self.plot_iv.addLegend()

        self.cursor_line1 = pg.InfiniteLine(pos=0.2, angle=90, movable=True, pen=pg.mkPen("#D97706", width=1.8, style=Qt.PenStyle.DashLine))
        self.cursor_line2 = pg.InfiniteLine(pos=0.9, angle=90, movable=True, pen=pg.mkPen("#059669", width=1.8, style=Qt.PenStyle.DashLine))
        self.cursor_line1.sigPositionChanged.connect(self._update_cursor_readout)
        self.cursor_line2.sigPositionChanged.connect(self._update_cursor_readout)
        tab1_splitter.addWidget(self.plot_iv)

        # Docked Live Measurement Data Table right next to the graph!
        live_tbl_box = QGroupBox("📋 실시간 측정 데이터 (인가/측정 전압 V · 전류 A · 저항 Ω · 전력 W)")
        lt_layout = QVBoxLayout(live_tbl_box)
        lt_layout.setContentsMargins(4, 6, 4, 4)
        self.tbl_live_data = QTableWidget(0, 6)
        self.tbl_live_data.setHorizontalHeaderLabels([
            "No.", "Time(s)", "전압 V (V)", "전류 I (A)", "저항 R (Ω)", "전력 P (W)"
        ])
        self.tbl_live_data.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        lt_layout.addWidget(self.tbl_live_data)
        tab1_splitter.addWidget(live_tbl_box)
        tab1_splitter.setSizes([680, 440])

        g_layout.addWidget(tab1_splitter, 1)

        self.lbl_metrics = QLabel("측정 대기 중 — 상단 [측정 시작] 또는 좌측 [단일 DC 즉시 측정]을 누르면 그래프와 우측 데이터 표에 측정값이 즉시 표시됩니다.")
        self.lbl_metrics.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.lbl_metrics.setFixedHeight(44)
        self.lbl_metrics.setStyleSheet(
            "background-color: #F8FAFC; border: 1px solid #CBD5E1; padding: 6px; font-family: Consolas, '맑은 고딕';"
        )
        self.lbl_metrics.setWordWrap(True)
        g_layout.addWidget(self.lbl_metrics)
        self.tabs.addTab(graph_tab, "📈 실시간 그래프 & 측정 데이터 표 (한눈에 보기)")

        # --- TAB 2: Phase 3 High-Speed Pulse, 1 MS/s Digitizer & SOA Envelope ---
        p3_tab = QWidget()
        p3_layout = QHBoxLayout(p3_tab)

        p3_left = QWidget()
        p3_left_layout = QVBoxLayout(p3_left)
        grp_p3 = QGroupBox("고속 펄스 · 1 MS/s 디지타이저 & 열/SOA 설정 (2461 / 2601B-PULSE / 2651A)")
        p3_form = QFormLayout(grp_p3)

        self.spn_pulse_width_us = QDoubleSpinBox()
        self.spn_pulse_width_us.setDecimals(1)
        self.spn_pulse_width_us.setRange(1.0, 1000000.0)
        self.spn_pulse_width_us.setValue(200.0)
        self.spn_pulse_width_us.editingFinished.connect(
            lambda: self._validate_and_clamp_spinbox(self.spn_pulse_width_us, "pulse_width_us", "펄스 폭(µs)")
        )
        self.spn_pulse_width_us.valueChanged.connect(self.refresh_soa_chart)
        p3_form.addRow("펄스 폭 (Pulse Width µs):", self.spn_pulse_width_us)

        self.spn_pulse_period_ms = QDoubleSpinBox()
        self.spn_pulse_period_ms.setDecimals(2)
        self.spn_pulse_period_ms.setRange(0.05, 5000.0)
        self.spn_pulse_period_ms.setValue(10.0)
        self.spn_pulse_period_ms.valueChanged.connect(self.refresh_soa_chart)
        p3_form.addRow("펄스 주기 (Pulse Period ms):", self.spn_pulse_period_ms)

        self.spn_pulse_count = QSpinBox()
        self.spn_pulse_count.setRange(1, 50000)
        self.spn_pulse_count.setValue(1)
        p3_form.addRow("펄스 개수 (Pulse Count):", self.spn_pulse_count)

        self.chk_digitizer = QCheckBox("1 MS/s 고속 디지타이저 파형 캡처 활성화")
        self.chk_digitizer.setChecked(True)
        p3_form.addRow(self.chk_digitizer)

        self.spn_sample_rate = QSpinBox()
        self.spn_sample_rate.setRange(1000, 1000000)
        self.spn_sample_rate.setSingleStep(100000)
        self.spn_sample_rate.setValue(1000000)
        p3_form.addRow("디지타이저 샘플레이트 (S/s):", self.spn_sample_rate)

        self.spn_inductance_uh = QDoubleSpinBox()
        self.spn_inductance_uh.setDecimals(2)
        self.spn_inductance_uh.setRange(0.0, 3.0)
        self.spn_inductance_uh.setValue(0.5)
        p3_form.addRow("2601B-PULSE 부하 인덕턴스 (µH):", self.spn_inductance_uh)

        self.chk_fast_adc = QCheckBox("2651A Fast ADC (1µs 과도응답 캡처) 사용")
        self.chk_fast_adc.setChecked(True)
        p3_form.addRow(self.chk_fast_adc)

        self.chk_sink_mode = QCheckBox("Sink 모드 (2·4사분면 전력 흡수 / 2651A Sink)")
        self.chk_sink_mode.toggled.connect(self.refresh_soa_chart)
        p3_form.addRow(self.chk_sink_mode)

        self.spn_ambient_temp = QDoubleSpinBox()
        self.spn_ambient_temp.setDecimals(1)
        self.spn_ambient_temp.setRange(15.0, 55.0)
        self.spn_ambient_temp.setValue(25.0)
        self.spn_ambient_temp.valueChanged.connect(self.refresh_soa_chart)
        p3_form.addRow("주변/내부 온도 (Ambient °C):", self.spn_ambient_temp)

        p3_left_layout.addWidget(grp_p3)

        btn_run_pulse = QPushButton("⚡ 고속 펄스 & 디지타이저 즉시 측정 실행")
        btn_run_pulse.setStyleSheet("background-color: #0070C0; color: white; font-weight: bold; padding: 8px;")
        btn_run_pulse.clicked.connect(self.execute_high_speed_pulse_measurement)
        p3_left_layout.addWidget(btn_run_pulse)

        self.btn_acal = QPushButton("🔧 2461 원격 자가교정 (Autocalibration ACAL) 실행")
        self.btn_acal.setStyleSheet("background-color: #0E7490; color: white; font-weight: bold; padding: 8px;")
        self.btn_acal.clicked.connect(self.run_remote_2461_acal)
        p3_left_layout.addWidget(self.btn_acal)

        self.lbl_soa_status = QLabel()
        self.lbl_soa_status.setWordWrap(True)
        self.lbl_soa_status.setStyleSheet(
            "background-color: #EFF6FF; color: #0B1F3A; padding: 8px; border-radius: 4px; font-weight: bold;"
        )
        p3_left_layout.addWidget(self.lbl_soa_status)
        p3_left_layout.addStretch(1)
        p3_layout.addWidget(p3_left, stretch=2)

        soa_box = QWidget()
        soa_vlayout = QVBoxLayout(soa_box)
        self.plot_soa = pg.PlotWidget(title="Safe Operating Area (SOA) Boundary & Operating Point")
        self.plot_soa.setLabel("bottom", "Voltage |V|", units="V")
        self.plot_soa.setLabel("left", "Current |I|", units="A")
        self.plot_soa.showGrid(x=True, y=True, alpha=0.3)
        self.plot_soa.addLegend()
        soa_vlayout.addWidget(self.plot_soa)
        p3_layout.addWidget(soa_box, stretch=3)

        self.tabs.addTab(p3_tab, "⚡ 고속 펄스·디지타이저 & SOA 검증 (Phase 3)")

        # --- TAB 3: Requirement 2 & 3 Easy Parametric Waveform Generator + Arbitrary Step Editor ---
        wf_tab = QWidget()
        wf_layout = QVBoxLayout(wf_tab)

        # Requirement 3: Easy Parametric Waveform Builder Box
        grp_easy = QGroupBox(
            "✨ 쉬운 파형 생성기 (모형·인가 레벨·Duration 시간·반복 개수 입력 → 원클릭 생성 | 반복 개수 -1 입력 시 무한 연속 출력)"
        )
        grp_easy.setStyleSheet("QGroupBox { font-weight: bold; color: #0B1F3A; }")
        easy_row = QHBoxLayout(grp_easy)

        easy_row.addWidget(QLabel("파형 모형:"))
        self.cmb_easy_shape = QComboBox()
        for sh in EasyWaveformShape:
            self.cmb_easy_shape.addItem(sh.value)
        easy_row.addWidget(self.cmb_easy_shape)

        easy_row.addWidget(QLabel("인가 레벨(High V/A):"))
        self.spn_easy_high = QDoubleSpinBox()
        self.spn_easy_high.setDecimals(4)
        self.spn_easy_high.setRange(-1100.0, 1100.0)
        self.spn_easy_high.setValue(3.3)
        easy_row.addWidget(self.spn_easy_high)

        easy_row.addWidget(QLabel("베이스 레벨(Low V/A):"))
        self.spn_easy_low = QDoubleSpinBox()
        self.spn_easy_low.setDecimals(4)
        self.spn_easy_low.setRange(-1100.0, 1100.0)
        self.spn_easy_low.setValue(0.0)
        easy_row.addWidget(self.spn_easy_low)

        easy_row.addWidget(QLabel("유지 시간(Duration s):"))
        self.spn_easy_dur = QDoubleSpinBox()
        self.spn_easy_dur.setDecimals(5)
        self.spn_easy_dur.setRange(0.00001, 60.0)
        self.spn_easy_dur.setValue(0.010)
        easy_row.addWidget(self.spn_easy_dur)

        easy_row.addWidget(QLabel("반복 개수 (×N 또는 -1=무한):"))
        self.spn_easy_repeat = QSpinBox()
        self.spn_easy_repeat.setRange(-1, 100000)
        self.spn_easy_repeat.setValue(100)
        self.spn_easy_repeat.setToolTip("예: 100 입력 시 100개 연속 출력 | 1 입력 시 단발 출력 | -1 입력 시 무한 연속 출력")
        easy_row.addWidget(self.spn_easy_repeat)

        self.btn_easy_generate = QPushButton("⚡ 이 조건으로 파형 즉시 생성 & 적용")
        self.btn_easy_generate.setStyleSheet(
            "background-color: #16A34A; color: white; font-weight: bold; padding: 6px 14px; border-radius: 4px;"
        )
        self.btn_easy_generate.clicked.connect(self.generate_easy_parametric_waveform)
        easy_row.addWidget(self.btn_easy_generate)

        wf_layout.addWidget(grp_easy)

        # Requirement 2: Step Add / Delete Selected / Clear All Toolbar
        wf_toolbar = QHBoxLayout()
        btn_add_step = QPushButton("+ 스텝 추가")
        btn_add_step.setStyleSheet("font-weight: bold;")
        btn_add_step.clicked.connect(self._add_waveform_step)

        self.btn_del_step = QPushButton("- 선택 스텝 삭제")
        self.btn_del_step.setStyleSheet("background-color: #FEE2E2; color: #991B1B; font-weight: bold;")
        self.btn_del_step.clicked.connect(self._delete_selected_waveform_steps)

        self.btn_clear_steps = QPushButton("🗑 전체 스텝 초기화")
        self.btn_clear_steps.clicked.connect(self._clear_all_waveform_steps)

        btn_sq = QPushButton("사각파 예시")
        btn_sq.clicked.connect(self._load_square_preset)
        btn_tri = QPushButton("삼각파 예시")
        btn_tri.clicked.connect(self._load_triangle_preset)
        btn_stair = QPushButton("계단파 예시")
        btn_stair.clicked.connect(self._load_staircase_preset)
        btn_imp_csv = QPushButton("📂 CSV/Excel 불러오기")
        btn_imp_csv.clicked.connect(self._import_waveform_file)
        btn_exp_csv = QPushButton("💾 파형 CSV 저장")
        btn_exp_csv.clicked.connect(self._export_waveform_file)
        btn_use_list = QPushButton("✔ 테이블 수정사항 프리뷰 반영")
        btn_use_list.setStyleSheet("background-color: #0070C0; color: white; font-weight: bold;")
        btn_use_list.clicked.connect(self._apply_waveform_table_to_recipe)

        for b in (
            btn_add_step,
            self.btn_del_step,
            self.btn_clear_steps,
            btn_sq,
            btn_tri,
            btn_stair,
            btn_imp_csv,
            btn_exp_csv,
            btn_use_list,
        ):
            wf_toolbar.addWidget(b)
        wf_layout.addLayout(wf_toolbar)

        wf_split = QSplitter(Qt.Orientation.Horizontal)
        self.tbl_waveform = QTableWidget(0, 5)
        self.tbl_waveform.setHorizontalHeaderLabels([
            "Step", "시간 Duration (s)", "인가 레벨 (V/A)", "Limit (A/V)", "측정 여부 (1/0)"
        ])
        self.tbl_waveform.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        wf_split.addWidget(self.tbl_waveform)

        preview_box = QWidget()
        pv_layout = QVBoxLayout(preview_box)
        self.plot_preview = pg.PlotWidget(title="Time-Domain Waveform Preview (출력 전 시간축 파형 & 측정 포인트)")
        self.plot_preview.setLabel("bottom", "Time", units="s")
        self.plot_preview.setLabel("left", "Source Level", units="V/A")
        self.plot_preview.showGrid(x=True, y=True, alpha=0.3)
        self.plot_preview.addLegend()
        pv_layout.addWidget(self.plot_preview)

        self.lbl_preview_summary = QLabel()
        self.lbl_preview_summary.setStyleSheet(
            "background-color: #EFF6FF; color: #0B1F3A; padding: 6px; font-weight: bold;"
        )
        pv_layout.addWidget(self.lbl_preview_summary)
        wf_split.addWidget(preview_box)
        wf_split.setSizes([460, 580])
        wf_layout.addWidget(wf_split)

        self.tabs.addTab(wf_tab, "🌊 쉬운 파형 생성기 & 임의 파형 편집기")

        # --- TAB 4: Requirement 7 Per-Node Independent Waveform Mode & Source Function Separation ---
        ch_tab = QWidget()
        ch_layout = QVBoxLayout(ch_tab)
        ch_info = QLabel(
            "<b>다중 장비(1~10대) & 다채널 논리 채널 매핑 + 장비별 독립 파형/소스 분리 설정:</b><br>"
            "• 장비가 2대 이상(또는 다채널)일 때 <b>[개별 분리 설정]</b>을 체크하면 각 장비별로 <b>파형 모드(LINEAR/LIST/PULSE 등)</b>와 "
            "<b>소스 인가 기능(VOLTAGE 전압원 vs CURRENT 전류원)</b> 및 시작·종료 레벨을 서로 다르게 독립 제어할 수 있습니다."
        )
        ch_info.setWordWrap(True)
        ch_layout.addWidget(ch_info)

        self.tbl_channels = QTableWidget(0, 9)
        self.tbl_channels.setHorizontalHeaderLabels([
            "논리 채널 ID",
            "TSP 경로",
            "활성화",
            "할당 역할(Role)",
            "개별 분리 설정",
            "장비별 파형 모드",
            "장비별 소스 기능",
            "시작~종료 (Start,Stop)",
            "개별 Limit",
        ])
        self.tbl_channels.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        ch_layout.addWidget(self.tbl_channels)
        self.tabs.addTab(ch_tab, "🔗 다중 장비·채널별 개별 파형/소스 설정 (1~10대)")

        # --- TAB 5: Reading Buffer Table ---
        tbl_tab = QWidget()
        t_layout = QVBoxLayout(tbl_tab)
        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels([
            "Idx", "Node.CH", "Time (s)", "Voltage (V)", "Current (A)", "Resistance (Ω)", "Power (W)", "Status"
        ])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        t_layout.addWidget(self.table)
        self.tabs.addTab(tbl_tab, "📋 내부 Reading Buffer 데이터 테이블")

        right_splitter.addWidget(self.tabs)

        # Bottom Console: Live TSP Command Spy
        spy_group = QGroupBox("🔍 실시간 TSP Command Spy & 에러 큐 모니터 (PC ↔ SMU 패킷 로거)")
        spy_layout = QVBoxLayout(spy_group)
        self.txt_spy = QPlainTextEdit()
        self.txt_spy.setReadOnly(True)
        self.txt_spy.setFont(QFont("Consolas", 9))
        self.txt_spy.setStyleSheet("background-color: #0B1F3A; color: #E2E8F0;")
        spy_layout.addWidget(self.txt_spy)
        right_splitter.addWidget(spy_group)
        right_splitter.setSizes([660, 200])

        main_splitter.addWidget(right_splitter)
        main_splitter.setSizes([380, 1140])
        root_layout.addWidget(main_splitter)

    # =========================================================================
    # Requirement 1, 4, 6, 8, 9 Handlers
    # =========================================================================

    def open_connection_wizard(self) -> None:
        """Requirement 1: Launch TEK_quickstart-style Beginner Connection & Multi-Node Test Wizard."""
        dlg = SMUConnectionWizardDialog(
            visa_mgr=self.visa_mgr,
            registry=self.registry,
            initial_model=self.cmb_model.currentText(),
            initial_nodes=self.spn_nodes.value(),
            parent=self,
        )
        if dlg.exec() == dlg.DialogCode.Accepted:
            self.active_resource_name = dlg.selected_resource
            self.cmb_model.setCurrentText(dlg.selected_model)
            self.spn_nodes.setValue(dlg.selected_nodes)
            self._connect_driver_to_resource(self.active_resource_name, dlg.selected_model)

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
        except Exception as exc:
            self.spy.record("ERR", model_name, f"실물 VISA 연결 실패 ({resource_name}) -> 시뮬레이터 전환: {exc}")
            self.driver = create_smu_driver(model_name, spy=self.spy, registry=self.registry)
        self.engine = SweepExecutionEngine(self.driver)

    def execute_quick_single_dc_measurement(self) -> None:
        """Apply single DC voltage/current on 2450 (or any SMU) and immediately measure V, I, R, P into Live DMM & Table."""
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
            output_off_mode=OutputOffMode(self.cmb_offmode.currentText()),
        )
        try:
            pt = self.driver.measure_single_dc(cfg)
        except Exception as exc:
            QMessageBox.critical(self, "단일 DC 소스/측정 오류", str(exc))
            return

        pt.index = len(self.single_dc_points) + 1
        self.single_dc_points.append(pt)
        r_str = f"{pt.resistance_ohm:.4e} Ω" if pt.resistance_ohm != float("inf") else "INF Ω"
        self.lbl_live_dmm.setText(
            f"전압(V): {pt.voltage_v:+.5f} V  |  전류(I): {pt.current_a:+.5e} A  |  "
            f"저항(R): {r_str}  |  전력(P): {pt.power_w:+.4e} W"
        )

        # Build a MeasurementResult from accumulated single DC readings so Graph, Live Table, and Excel Export all work seamlessly
        from datetime import datetime
        from smu_app.analysis.iv_pulse_analyzer import analyze_measurement_points

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
        self.last_result = res
        self._refresh_plot()
        self._populate_table(res)
        self.lbl_metrics.setText(
            f"[단일 DC 즉시 측정 완료 #{pt.index}] 인가={cfg.source_level:g} ({s_func.value})  ➜  "
            f"측정 전압 V = {pt.voltage_v:+.6f} V  |  측정 전류 I = {pt.current_a:+.6e} A  |  "
            f"저항 R = {r_str}  |  전력 P = {pt.power_w:+.5e} W"
        )

    def open_current_model_manual(self) -> bool:
        """Requirement 6: Open the reference manual PDF for the currently selected SMU model."""
        return open_model_manual_pdf(self.cmb_model.currentText(), self)

    def open_detached_graph_window(self) -> DetachedGraphWindow:
        """Requirement 4: Pop out the measurement graph into an independent resizable window."""
        win = DetachedGraphWindow(
            result=self.last_result,
            run_history=self.run_history,
            exporter=self.exporter,
            parent=self,
        )
        self.detached_windows.append(win)
        win.show()
        return win

    def clear_graph_and_table_data(self) -> None:
        """Reset all graphs, side-by-side live data table, buffer table, Live DMM, Run History, and SMU internal buffer to an empty state."""
        self.last_result = None
        self.run_history.clear()
        self.single_dc_points.clear()

        # Clear main plot and cursor readout
        self.plot_iv.clear()
        self.lbl_cursor_readout.setText("커서 대기 중")
        self.lbl_metrics.setText(
            "초기화 완료 — 그래프 및 측정 데이터 표가 비워졌습니다. [측정 시작] 또는 [단일 DC 즉시 측정]을 눌러 새 측정을 시작하세요."
        )

        # Clear Live DMM banner
        if hasattr(self, "lbl_live_dmm"):
            self.lbl_live_dmm.setText("전압(V): ---   |   전류(I): ---   |   저항(R): ---   |   전력(P): ---")

        # Clear side-by-side Live Measurement Table (Tab 1) and Full Reading Buffer Table (Tab 5)
        if hasattr(self, "tbl_live_data"):
            self.tbl_live_data.setRowCount(0)
        self.table.setRowCount(0)

        # Clear any open detached pop-out graph windows
        for dwin in list(self.detached_windows):
            if dwin.isVisible():
                dwin.update_data(None, [])

        # Clear instrument internal reading buffer (defbuffer1 / nvbuffer1)
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
        self.spy.record("SYS", self.driver.profile.model, "그래프·데이터 표·Run History 및 장비 내부 버퍼 전체 초기화 완료")

    def _validate_and_clamp_spinbox(self, spinbox: QDoubleSpinBox, kind: str, label_ko: str) -> tuple[float, str | None]:
        """Requirement 8: Check input value against selected SMU limits; if exceeded, notify user and clamp to max/min."""
        p = self.driver.profile
        val = spinbox.value()
        s_func = SourceFunction(self.cmb_source_func.currentText())
        is_pulse = self.cmb_sweep_type.currentText() in (SweepType.PULSE_TRAIN.value, SweepType.PULSE_SWEEP.value)

        if kind == "source_level":
            if s_func == SourceFunction.VOLTAGE:
                max_lim = p.max_pulse_voltage_v if is_pulse else p.max_dc_voltage_v
                unit = "V"
            else:
                max_lim = p.max_pulse_current_a if is_pulse else p.max_dc_current_a
                unit = "A"
            min_lim = -max_lim
        elif kind == "compliance":
            if s_func == SourceFunction.VOLTAGE:
                max_lim = p.max_pulse_current_a if is_pulse else p.max_dc_current_a
                unit = "A"
            else:
                max_lim = p.max_pulse_voltage_v if is_pulse else p.max_dc_voltage_v
                unit = "V"
            min_lim = 1e-7
        elif kind == "nplc":
            min_lim, max_lim, unit = p.min_nplc, p.max_nplc, " PLC"
        elif kind == "pulse_width_us":
            min_lim = p.min_pulse_width_s * 1e6
            max_lim = 500000.0
            unit = " µs"
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
        """Requirement 3: When repeat count is set to -1, automatically switch RepeatMode to CONTINUOUS."""
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
        """Requirement 3: Build waveform from simple parameters (Shape, High/Low level, Duration, x100 or -1 repeat)."""
        p = self.driver.profile
        s_func = SourceFunction(self.cmb_source_func.currentText())
        max_lim = p.max_dc_voltage_v if s_func == SourceFunction.VOLTAGE else p.max_dc_current_a
        unit = "V" if s_func == SourceFunction.VOLTAGE else "A"

        high_v, notice_h = clamp_value_with_notice(
            self.spn_easy_high.value(), -max_lim, max_lim, "쉬운 파형 High 레벨", p.model, unit
        )
        if notice_h:
            self.last_range_notice = notice_h
            self.spn_easy_high.setValue(high_v)
            if not self.suppress_range_popup:
                QMessageBox.warning(self, f"{p.model} 입력 범위 초과 자동 조정", notice_h)

        low_v, notice_l = clamp_value_with_notice(
            self.spn_easy_low.value(), -max_lim, max_lim, "쉬운 파형 Low 레벨", p.model, unit
        )
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
        """Requirement 2: Delete selected step rows from the arbitrary waveform editor table."""
        self.custom_steps = self._read_waveform_table_steps()
        selected_rows = sorted({idx.row() for idx in self.tbl_waveform.selectedIndexes()})
        if not selected_rows and self.tbl_waveform.rowCount() > 0:
            # If no cell is highlighted, delete the last row for convenience
            selected_rows = [self.tbl_waveform.rowCount() - 1]
        self.custom_steps = WaveformBuilder.delete_steps(self.custom_steps, selected_rows)
        self._populate_waveform_table()
        self.refresh_waveform_preview()

    def _clear_all_waveform_steps(self) -> None:
        """Requirement 2: Clear all steps and leave 1 default 0V baseline step."""
        self.custom_steps = [
            WaveformStep(1, duration_s=self.spn_delay.value(), level=0.0, compliance_limit=self.spn_compliance.value())
        ]
        self._populate_waveform_table()
        self.refresh_waveform_preview()

    def save_setup_dialog(self) -> None:
        """Requirement 9: Prompt user to save complete setup JSON file."""
        path, _ = QFileDialog.getSaveFileName(
            self, "SMU 전체 설정 저장", "smu_full_setup.json", "SMU Setup JSON (*.json)"
        )
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
            output_off_mode=OutputOffMode(self.cmb_offmode.currentText()),
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
        )
        if not silent:
            self.spy.record("SYS", p.model, f"Full Setup Saved -> {out}")
        return out

    def load_setup_dialog(self) -> None:
        """Requirement 9: Prompt user to load and restore a saved setup JSON file."""
        path, _ = QFileDialog.getOpenFileName(
            self, "SMU 전체 설정 불러오기", "", "SMU Setup JSON (*.json)"
        )
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
            self.cmb_offmode.setCurrentText(cc.output_off_mode.value)
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

            self.refresh_waveform_preview()
            self.refresh_soa_chart()
            if not silent:
                self.spy.record("SYS", data["model"], f"Full Setup Loaded <- {file_path}")
        finally:
            self.suppress_range_popup = prev_suppress

    # =========================================================================
    # Standard Internal Handlers
    # =========================================================================

    def _on_spy_entry(self, entry: SpyEntry) -> None:
        self.txt_spy.appendPlainText(entry.format_line())

    def _on_sweep_param_changed(self, *_) -> None:
        self.refresh_waveform_preview()
        self.refresh_soa_chart()

    def _on_model_changed(self, model_name: str) -> None:
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

        self.lbl_spec_badge.setText(
            f"[{p.display_name}] {p.family} | {p.channels}CH | "
            f"DC: ±{p.max_dc_voltage_v}V / ±{p.max_dc_current_a}A ({p.max_dc_power_w}W) | "
            f"Pulse: {p.max_pulse_current_a}A (최소 {p.min_pulse_width_s*1e6:.0f}µs)"
        )
        self.chk_contact.setEnabled(p.has_contact_check)
        self.chk_neg_zero.setEnabled(p.negative_zero_bias_fix)
        self.btn_combining.setEnabled(p.combining_supported)

        if hasattr(self, "btn_acal"):
            self.btn_acal.setEnabled(p.has_acal)
            if self.spn_pulse_width_us.value() < p.min_pulse_width_s * 1e6:
                self.spn_pulse_width_us.setValue(max(p.min_pulse_width_s * 1e6, 200.0))

        # Clamp existing spinbox values silently or with notification if out of new model's bounds
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
                f"⚠ [SW 동기화 모드 활성] 선택된 {variant} 모델은 TSP-Link 하드웨어 트리거 미지원 모델입니다. "
                f"{nodes}대 연동 시 소프트웨어 동기화로 동작하며 1~20ms의 시작 타이밍 편차(Skew)가 발생할 수 있습니다."
            )
        if self.combined_config is not None:
            msgs.append(
                f"⚡ [출력 결합 활성: {self.combined_config.mode.value}] "
                f"합산 목표={self.combined_config.total_target_level} (Master {self.combined_config.master_level} + Slave {self.combined_config.slave_level}) | "
                f"Slave 10% 마진 Compliance={self.combined_config.slave_compliance}"
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
        """Requirement 7: Populate per-node / per-channel independent waveform & source function controls."""
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

            chk_ind = QCheckBox("개별 설정 사용")
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

            self.tbl_channels.setItem(
                r, 7, QTableWidgetItem(f"{lch.override_start_level:g}, {lch.override_stop_level:g}")
            )
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

    def open_combining_wizard(self) -> None:
        dlg = OutputCombiningWizardDialog(self.driver.profile, self)
        if dlg.exec() == dlg.DialogCode.Accepted and dlg.combined_config is not None:
            self.combined_config = dlg.combined_config
            if self.spn_nodes.value() < 2 and self.driver.profile.channels < 2:
                self.spn_nodes.setValue(2)
            self._update_sync_warning_banner()

    # --- Phase 3: High-Speed Pulse, ACAL & SOA Envelope Chart ---
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
        self.plot_soa.plot(dc_v, dc_i, pen=pg.mkPen("#0070C0", width=2.5), name="DC SOA Boundary")

        if "PULSE" in curves:
            pv, pi = curves["PULSE"]
            self.plot_soa.plot(pv, pi, pen=pg.mkPen("#059669", width=2.5, style=Qt.PenStyle.DashLine), name="Pulse Extended SOA")

        recipe = self._build_current_recipe()
        cfg = ChannelConfig(logical_channel=LogicalChannel(1, p.channel_names[0]))
        p_cfg = self._build_pulse_digitizer_config()
        recipe.pulse_width_s = p_cfg.pulse_width_s
        recipe.pulse_period_s = p_cfg.pulse_period_s

        rep = SafetyAndSOAValidator.validate_soa_and_recipe(
            p,
            cfg,
            recipe,
            ambient_temp_c=p_cfg.ambient_temp_c,
            is_sink_quadrant=p_cfg.sink_mode_enable,
        )

        max_lvl = max(abs(self.spn_start.value()), abs(self.spn_stop.value()))
        comp = abs(self.spn_compliance.value())
        op_v = max_lvl if recipe.source_func == SourceFunction.VOLTAGE else comp
        op_i = comp if recipe.source_func == SourceFunction.VOLTAGE else max_lvl

        pt_color = "#16A34A" if rep.passed else "#DC2626"
        self.plot_soa.plot(
            [op_v],
            [op_i],
            pen=None,
            symbol="o",
            symbolSize=12,
            symbolBrush=pt_color,
            name=f"Operating Point ({op_v:.3g}V, {op_i:.3g}A)",
        )

        duty_pct = (p_cfg.pulse_width_s / max(p_cfg.pulse_period_s, p_cfg.pulse_width_s)) * 100.0
        max_duty_pct = SafetyAndSOAValidator.compute_thermal_and_sink_max_duty(
            p, op_v, op_i, p_cfg.ambient_temp_c, p_cfg.sink_mode_enable
        ) * 100.0
        acal_st = HighSpeedPulseDigitizerEngine.check_and_run_2461_acal(p, p_cfg, execute_now=False)

        status_header = "✔ [SOA & Duty Cycle 안전 검증 통과]" if rep.passed else "✖ [SOA / Duty Cycle 안전 한계 위반 차단]"
        err_detail = ("\n• " + "\n• ".join(rep.errors)) if rep.errors else ""
        self.lbl_soa_status.setText(
            f"{status_header}\n"
            f"• 동작점: |V|={op_v:.3g} V, |I|={op_i:.3g} A (전력 P={op_v*op_i:.2f} W)\n"
            f"• 현재 펄스 Duty: {duty_pct:.2f}%  /  열·Sink 반영 최대 허용 Duty: {max_duty_pct:.2f}%\n"
            f"• {acal_st.message}{err_detail}"
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

    # --- Arbitrary Waveform Editor Methods ---
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
                steps.append(
                    WaveformStep(
                        step_index=len(steps) + 1,
                        duration_s=max(1e-6, dur),
                        level=lvl,
                        compliance_limit=lim,
                        measure_enable=meas,
                    )
                )
            except Exception:
                continue
        return steps

    def _load_square_preset(self) -> None:
        self.custom_steps = WaveformBuilder.build_square_wave(
            high_level=self.spn_stop.value(),
            low_level=self.spn_start.value(),
            high_time_s=self.spn_delay.value(),
            low_time_s=self.spn_delay.value(),
            cycles=5,
            compliance=self.spn_compliance.value(),
        )
        self._populate_waveform_table()
        self._apply_waveform_table_to_recipe()

    def _load_triangle_preset(self) -> None:
        self.custom_steps = WaveformBuilder.build_triangle_wave(
            min_level=self.spn_start.value(),
            max_level=self.spn_stop.value(),
            steps_per_half_cycle=10,
            step_duration_s=self.spn_delay.value(),
            cycles=2,
            compliance=self.spn_compliance.value(),
        )
        self._populate_waveform_table()
        self._apply_waveform_table_to_recipe()

    def _load_staircase_preset(self) -> None:
        self.custom_steps = WaveformBuilder.build_staircase_wave(
            start_level=self.spn_start.value(),
            stop_level=self.spn_stop.value(),
            num_steps=11,
            step_duration_s=self.spn_delay.value(),
            dual_return=True,
            compliance=self.spn_compliance.value(),
        )
        self._populate_waveform_table()
        self._apply_waveform_table_to_recipe()

    def _add_waveform_step(self) -> None:
        self.custom_steps = self._read_waveform_table_steps()
        next_idx = len(self.custom_steps) + 1
        self.custom_steps.append(
            WaveformStep(
                step_index=next_idx,
                duration_s=self.spn_delay.value(),
                level=self.spn_stop.value(),
                compliance_limit=self.spn_compliance.value(),
                measure_enable=True,
            )
        )
        self._populate_waveform_table()
        self.refresh_waveform_preview()

    def _import_waveform_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "파형 CSV/Excel 파일 불러오기", "", "Waveform Files (*.csv *.xlsx *.xls)"
        )
        if not path:
            return
        try:
            self.custom_steps = WaveformBuilder.import_steps_from_file(path, self.spn_compliance.value())
            self._populate_waveform_table()
            self._apply_waveform_table_to_recipe()
        except Exception as exc:
            QMessageBox.critical(self, "파형 임포트 오류", str(exc))

    def _export_waveform_file(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "파형 CSV 파일 저장", "custom_waveform.csv", "CSV Files (*.csv)")
        if not path:
            return
        self.custom_steps = self._read_waveform_table_steps()
        WaveformBuilder.export_steps_to_csv(self.custom_steps, path)
        QMessageBox.information(self, "저장 완료", f"파형 테이블이 저장되었습니다:\n{path}")

    def _apply_waveform_table_to_recipe(self) -> None:
        self.custom_steps = self._read_waveform_table_steps()
        self.cmb_sweep_type.setCurrentText(SweepType.LIST.value)
        self.refresh_waveform_preview()

    def _build_current_recipe(self) -> SweepRecipe:
        p = self.driver.profile
        if hasattr(self, "tbl_waveform") and self.tbl_waveform.rowCount() > 0:
            self.custom_steps = self._read_waveform_table_steps()
        pw_s = self.spn_pulse_width_us.value() * 1e-6 if hasattr(self, "spn_pulse_width_us") else 0.0002
        per_s = self.spn_pulse_period_ms.value() * 1e-3 if hasattr(self, "spn_pulse_period_ms") else 0.010
        use_dig = self.chk_digitizer.isChecked() if hasattr(self, "chk_digitizer") else False
        sr = self.spn_sample_rate.value() if hasattr(self, "spn_sample_rate") else 1000000
        rep_cnt = max(1, self.spn_repeat_count.value())

        return SweepRecipe(
            name=f"{p.model}_{self.cmb_sweep_type.currentText().lower()}",
            sweep_type=SweepType(self.cmb_sweep_type.currentText()),
            source_func=SourceFunction(self.cmb_source_func.currentText()),
            measure_func=(
                MeasureFunction.CURRENT
                if self.cmb_source_func.currentText() == SourceFunction.VOLTAGE.value
                else MeasureFunction.VOLTAGE
            ),
            start_level=self.spn_start.value(),
            stop_level=self.spn_stop.value(),
            points=self.spn_points.value(),
            delay_s=self.spn_delay.value(),
            compliance_limit=self.spn_compliance.value(),
            nplc=self.spn_nplc.value(),
            pulse_width_s=pw_s,
            pulse_period_s=per_s,
            repeat_mode=RepeatMode(self.cmb_repeat_mode.currentText()),
            repeat_count=rep_cnt,
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
        self.plot_preview.plot(
            pv.time_s,
            pv.level_va,
            pen=pg.mkPen(color="#0E7490", width=2),
            name="Source Waveform",
        )
        if len(pv.measure_times_s) > 0:
            self.plot_preview.plot(
                pv.measure_times_s,
                pv.measure_levels_va,
                pen=None,
                symbol="o",
                symbolSize=6,
                symbolBrush="#DC2626",
                name="Measure Points",
            )
        tot_str = "∞ (무한 연속 출력 -1)" if pv.is_infinite_continuous else f"{pv.total_duration_s:.4f} s ({pv.total_measure_points:,} pts)"
        self.lbl_preview_summary.setText(
            f"[파형 프리뷰 요약] 1주기 시간: {pv.single_cycle_duration_s:.4f} s ({pv.single_cycle_points} pts)  |  "
            f"총 소요 시간/포인트: {tot_str}  |  "
            f"종료 동작: {'Idle(0V) 복귀' if recipe.end_Return_to_bias else '마지막 레벨 Hold'}"
        )

    def _on_dut_changed(self, dut_str: str) -> None:
        if isinstance(self.driver.transport, VirtualSMUTransport):
            self.driver.transport.state.dut_type = VirtualDUTType(dut_str)

    def _on_interlock_toggled(self, checked: bool) -> None:
        if isinstance(self.driver.transport, VirtualSMUTransport):
            self.driver.transport.state.interlock_engaged = checked

    def emergency_stop(self) -> None:
        nodes = list(range(1, self.spn_nodes.value() + 1))
        self.driver.emergency_abort_all(active_nodes=nodes)
        QMessageBox.warning(self, "ALL STOP 실행 완료", f"전체 노드({nodes}) 출력 차단 및 트리거 강제 중단이 완료되었습니다.")

    def execute_measurement(self) -> None:
        # Ensure all inputs are clamped to model range before run
        self._validate_and_clamp_spinbox(self.spn_start, "source_level", "시작 레벨")
        self._validate_and_clamp_spinbox(self.spn_stop, "source_level", "종료 레벨")
        self._validate_and_clamp_spinbox(self.spn_compliance, "compliance", "Compliance Limit")
        self._sync_logical_channels_from_table()

        p = self.driver.profile
        ch_name = p.channel_names[0]
        recipe = self._build_current_recipe()
        p_cfg = self._build_pulse_digitizer_config()
        cfg = ChannelConfig(
            logical_channel=LogicalChannel(node_id=1, channel_name=ch_name),
            source_func=recipe.source_func,
            measure_func=recipe.measure_func,
            compliance_limit=self.spn_compliance.value(),
            nplc=self.spn_nplc.value(),
            sense_mode=SenseMode(self.cmb_sense.currentText()),
            output_off_mode=OutputOffMode(self.cmb_offmode.currentText()),
            contact_check_enabled=self.chk_contact.isChecked(),
        )
        try:
            res = self.engine.run_measurement(
                cfg=cfg,
                recipe=recipe,
                node_count=self.spn_nodes.value(),
                specific_variant=self._selected_variant_code(),
                logical_channels=self.logical_channels,
                combined_config=self.combined_config,
                pulse_config=p_cfg,
            )
        except Exception as exc:
            QMessageBox.critical(self, "Pre-Flight 안전 검증 / 실행 차단", str(exc))
            return

        self.last_result = res
        self.run_history.append(res)
        self._refresh_plot()
        self._populate_table(res)
        self.refresh_soa_chart()

        # Sync any open detached pop-out graph windows automatically
        for dwin in list(self.detached_windows):
            if dwin.isVisible():
                dwin.update_data(res, self.run_history)

        if self.chk_auto_export.isChecked():
            auto_csv = self.exporter.export_streaming_csv(res)
            res.warnings.append(f"Auto-Export 저장 완료: {auto_csv.name}")

        m = res.metrics
        vth_s = f"{m['v_threshold_v']:.3f} V" if m.get("v_threshold_v") is not None else "N/A"
        dvdi_s = f"{m['dv_di_median_ohm']:.2f} Ω" if m.get("dv_di_median_ohm") is not None else "N/A"
        rt_s = f"{m['rise_time_10_90_s']*1e6:.1f} µs" if m.get("rise_time_10_90_s") is not None else "N/A"
        ft_s = f"{m['fall_time_90_10_s']*1e6:.1f} µs" if m.get("fall_time_90_10_s") is not None else "N/A"
        ov_s = f"{m.get('overshoot_pct', 0.0):.1f}%"
        warn_s = f" | 알림: {'; '.join(res.warnings)}" if res.warnings else ""
        self.lbl_metrics.setText(
            f"[측정 완료: {len(res.points):,} pts | 모드: {res.recipe.acquisition_mode.value}]  "
            f"V: [{m['v_min']:.3f}V ~ {m['v_max']:.3f}V] | I_max: {m['i_max']:.4e}A (Top: {m.get('pulse_top_i', 0.0):.4e}A) | "
            f"Vth: {vth_s} | dV/dI: {dvdi_s} | "
            f"펄스 Rise(10-90%): {rt_s} | Fall: {ft_s} | Overshoot: {ov_s} | "
            f"Compliance: {m['compliance_hit_count']}건{warn_s}"
        )

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
            self.plot_iv.plot(dx, dy, pen=pg.mkPen(color="#94A3B8", width=1, style=Qt.PenStyle.DashLine), name="Prev Run")

        pts = self.last_result.points
        x, y, is_time = self._current_plot_xy(pts)
        self.plot_iv.setLabel("bottom", "Time" if is_time else "Voltage", units="s" if is_time else "V")
        self.plot_iv.setLabel("left", "log10(|Current|)" if self.chk_log_y.isChecked() else "Current", units="log A" if self.chk_log_y.isChecked() else "A")

        dx, dy = lttb_downsample(x, y, threshold=2000)
        self.plot_iv.plot(
            dx,
            dy,
            pen=pg.mkPen(color="#0070C0", width=2),
            symbol="o",
            symbolSize=4,
            symbolBrush="#0070C0",
            name=f"{self.last_result.model} ({len(pts)} pts)",
        )

        if self.chk_dual_cursors.isChecked() and len(x) >= 2:
            x_min, x_max = float(np.min(x)), float(np.max(x))
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
        c = compute_dual_cursor_metrics(
            x, y, float(self.cursor_line1.value()), float(self.cursor_line2.value())
        )
        x_unit = "s" if is_time else "V"
        self.lbl_cursor_readout.setText(
            f"[듀얼 커서] C1=({c.x1:.4g}{x_unit}, {c.y1:.4e}A) | C2=({c.x2:.4g}{x_unit}, {c.y2:.4e}A) | "
            f"ΔX={c.delta_x:.4g}{x_unit}, ΔY={c.delta_y:.4e}A | "
            f"기울기(Slope)={c.slope:.3e} | 등가저항(1/Slope)={c.inverse_slope_ohm:.3e} Ω"
        )

    def _populate_table(self, res: MeasurementResult) -> None:
        display_pts = res.points[:2000]
        comp_bg = QColor("#FEF3C7")

        # Update last point onto the top-left Live DMM banner as well
        if display_pts and hasattr(self, "lbl_live_dmm"):
            last_p = display_pts[-1]
            r_str = f"{last_p.resistance_ohm:.4e} Ω" if last_p.resistance_ohm != float("inf") else "INF Ω"
            self.lbl_live_dmm.setText(
                f"전압(V): {last_p.voltage_v:+.5f} V  |  전류(I): {last_p.current_a:+.5e} A  |  "
                f"저항(R): {r_str}  |  전력(P): {last_p.power_w:+.4e} W"
            )

        # 1. Populate Docked Side-by-Side Live Measurement Table in Tab 1 (right next to the graph)
        if hasattr(self, "tbl_live_data"):
            self.tbl_live_data.setRowCount(len(display_pts))
            for r, p in enumerate(display_pts):
                live_vals = [
                    str(p.index),
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

        # 2. Populate Full Reading Buffer Table in Tab 5
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

    def export_excel(self) -> None:
        if not self.last_result:
            QMessageBox.information(self, "데이터 없음", "먼저 측정을 실행해 주세요.")
            return
        path = self.exporter.export_excel_report(self.last_result)
        QMessageBox.information(self, "Excel 저장 완료", f"구조화된 Excel 리포트가 생성되었습니다:\n{path}")

    def export_history_comparison(self) -> None:
        if not self.run_history:
            QMessageBox.information(self, "데이터 없음", "비교할 Run History 측정 이력이 없습니다.")
            return
        path = self.exporter.export_run_history_comparison_excel(self.run_history)
        QMessageBox.information(
            self, "Run History 비교 리포트 생성 완료", f"총 {len(self.run_history)}회 실행 비교 Excel 리포트가 저장되었습니다:\n{path}"
        )

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
        QMessageBox.information(
            self, "Parquet & SQLite 저장 완료", f"고속 아카이브 파일이 생성되었습니다:\n• Parquet: {pq_path}\n• SQLite: {db_path}"
        )

    def export_plot_image(self) -> None:
        if not self.last_result:
            QMessageBox.information(self, "데이터 없음", "먼저 측정을 실행해 주세요.")
            return
        mode = "TIME" if self.cmb_plot_view.currentIndex() == 1 else "IV"
        png_path = self.exporter.export_plot_image(self.last_result, fmt="png", mode=mode, history_overlays=self.run_history[:-1])
        svg_path = self.exporter.export_plot_image(self.last_result, fmt="svg", mode=mode, history_overlays=self.run_history[:-1])
        QMessageBox.information(
            self, "그래프 이미지 저장 완료", f"PNG 및 SVG 고해상도 그래프가 저장되었습니다:\n• PNG: {png_path}\n• SVG: {svg_path}"
        )


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
