"""Standalone Popup Dialog for Easy Parametric Waveform Generator & Arbitrary Step Editor.

Features:
- Spacious resizable popup window (`fit_and_center_window`) separating optional custom waveform editing from the main 3-tab measurement workspace.
- Easy Parametric Waveform Generator (Square/Pulse/Triangle/Staircase/Sine + High/Low + Duration + Repeat count xN or -1 infinite continuous).
- Real-time auto-preview update whenever any table cell is edited (`cellChanged`).
- Two clear apply actions:
  1) `✔ 설정만 메인 화면에 적용` (apply waveform & close dialog)
  2) `🚀 적용 후 즉시 장비 출력 & 측정` (apply waveform, close dialog, and trigger immediate SMU run)
"""

from __future__ import annotations

from dataclasses import replace
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
import pyqtgraph as pg

from smu_app.core.models import (
    RepeatMode,
    SourceFunction,
    SweepRecipe,
    SweepType,
    WaveformStep,
)
from smu_app.core.profiles import SMUModelProfile
from smu_app.features.waveform_builder import (
    EasyWaveformShape,
    WaveformBuilder,
)
from smu_app.storage.setup_manager import clamp_value_with_notice
from smu_app.ui.connection_wizard import fit_and_center_window


class WaveformEditorDialog(QDialog):
    """Dedicated popup window for generating, editing, importing, and previewing arbitrary SMU waveforms."""

    def __init__(
        self,
        profile: SMUModelProfile,
        source_func: SourceFunction,
        initial_steps: list[WaveformStep],
        default_delay_s: float = 0.005,
        default_compliance: float = 0.1,
        initial_repeat_count: int = 1,
        initial_repeat_mode: RepeatMode = RepeatMode.SINGLE,
        end_return_to_bias: bool = True,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.profile = profile
        self.source_func = source_func
        self.default_delay_s = default_delay_s
        self.default_compliance = default_compliance
        self.end_return_to_bias = end_return_to_bias

        self.result_steps: list[WaveformStep] = [replace(s) for s in initial_steps]
        self.result_repeat_mode: RepeatMode = initial_repeat_mode
        self.result_repeat_count: int = initial_repeat_count
        self.run_immediately_requested: bool = False
        self.reset_to_default_requested: bool = False
        self.summary_badge_text: str = ""
        self._populating_table: bool = False

        self.setWindowTitle(f"🌊 쉬운 파형 생성기 & 임의 파형 편집기 — {profile.display_name}")
        self._build_ui()
        fit_and_center_window(self, target_w_ratio=0.80, target_h_ratio=0.78, min_w=980, min_h=600)
        self._populate_table()
        self.refresh_preview()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        # 1. Easy Parametric Waveform Generator Box
        grp_easy = QGroupBox(
            "✨ 쉬운 파형 원클릭 생성기 (모형 · High/Low 레벨 · 유지시간 · 반복 개수 xN 또는 -1=무한 연속)"
        )
        grp_easy.setStyleSheet("QGroupBox { font-weight: bold; color: #0b2545; }")
        easy_row = QHBoxLayout(grp_easy)

        easy_row.addWidget(QLabel("파형 모형:"))
        self.cmb_easy_shape = QComboBox()
        for sh in EasyWaveformShape:
            self.cmb_easy_shape.addItem(sh.value)
        easy_row.addWidget(self.cmb_easy_shape)

        easy_row.addWidget(QLabel("High 레벨(V/A):"))
        self.spn_easy_high = QDoubleSpinBox()
        self.spn_easy_high.setDecimals(4)
        self.spn_easy_high.setRange(-1100.0, 1100.0)
        self.spn_easy_high.setValue(3.3)
        easy_row.addWidget(self.spn_easy_high)

        easy_row.addWidget(QLabel("Low 레벨(V/A):"))
        self.spn_easy_low = QDoubleSpinBox()
        self.spn_easy_low.setDecimals(4)
        self.spn_easy_low.setRange(-1100.0, 1100.0)
        self.spn_easy_low.setValue(0.0)
        easy_row.addWidget(self.spn_easy_low)

        easy_row.addWidget(QLabel("유지 시간(s):"))
        self.spn_easy_dur = QDoubleSpinBox()
        self.spn_easy_dur.setDecimals(5)
        self.spn_easy_dur.setRange(0.00001, 60.0)
        self.spn_easy_dur.setValue(0.010)
        easy_row.addWidget(self.spn_easy_dur)

        easy_row.addWidget(QLabel("반복 개수(-1=무한):"))
        self.spn_easy_repeat = QSpinBox()
        self.spn_easy_repeat.setRange(-1, 100000)
        self.spn_easy_repeat.setValue(
            -1 if self.result_repeat_mode == RepeatMode.CONTINUOUS else max(1, self.result_repeat_count)
        )
        self.spn_easy_repeat.valueChanged.connect(self._on_repeat_changed)
        easy_row.addWidget(self.spn_easy_repeat)

        btn_gen = QPushButton("⚡ 파형 즉시 생성")
        btn_gen.setStyleSheet(
            "background-color: #16a34a; color: white; font-weight: bold; padding: 6px 14px; border-radius: 4px;"
        )
        btn_gen.clicked.connect(self.generate_easy_waveform)
        easy_row.addWidget(btn_gen)
        layout.addWidget(grp_easy)

        # 2. Step Editing Toolbar
        tb = QHBoxLayout()
        btn_add = QPushButton("+ 스텝 추가")
        btn_add.setStyleSheet("font-weight: bold;")
        btn_add.clicked.connect(self._add_step)

        btn_del = QPushButton("- 선택 스텝 삭제")
        btn_del.setStyleSheet("background-color: #fee2e2; color: #991b1b; font-weight: bold;")
        btn_del.clicked.connect(self._delete_selected_steps)

        btn_clr = QPushButton("🗑 전체 스텝 초기화")
        btn_clr.clicked.connect(self._clear_steps)

        btn_imp = QPushButton("📂 CSV/Excel 불러오기")
        btn_imp.clicked.connect(self._import_file)

        btn_exp = QPushButton("💾 파형 CSV 저장")
        btn_exp.clicked.connect(self._export_file)

        lbl_auto_hint = QLabel("💡 표의 숫자를 수정하고 Enter를 누르면 우측 프리뷰가 실시간 자동 갱신됩니다.")
        lbl_auto_hint.setStyleSheet("color: #0369a1; font-weight: bold; padding-left: 8px;")

        for w in (btn_add, btn_del, btn_clr, btn_imp, btn_exp, lbl_auto_hint):
            tb.addWidget(w)
        tb.addStretch(1)
        layout.addLayout(tb)

        # 3. Main Splitter: Spacious Step Table (Left) | Dark Time-Domain Preview Plot (Right)
        split = QSplitter(Qt.Orientation.Horizontal)

        self.tbl_waveform = QTableWidget(0, 5)
        self.tbl_waveform.setAlternatingRowColors(True)
        self.tbl_waveform.verticalHeader().setVisible(False)
        self.tbl_waveform.setHorizontalHeaderLabels(["Step", "시간 (s)", "레벨 (V/A)", "Limit (A/V)", "측정 (1/0)"])
        hdr = self.tbl_waveform.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        hdr.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.tbl_waveform.setColumnWidth(0, 50)
        self.tbl_waveform.setColumnWidth(1, 90)
        self.tbl_waveform.setColumnWidth(2, 105)
        self.tbl_waveform.setColumnWidth(3, 90)
        self.tbl_waveform.cellChanged.connect(self._on_table_cell_changed)
        split.addWidget(self.tbl_waveform)

        pv_widget = QWidget()
        pv_v = QVBoxLayout(pv_widget)
        pv_v.setContentsMargins(6, 0, 0, 0)
        self.plot_preview = pg.PlotWidget()
        self.plot_preview.setBackground("#0b132b")
        self.plot_preview.setTitle("<span style='color:#e2e8f0; font-weight:bold;'>Time-Domain Waveform Preview (출력 전 파형 미리보기)</span>")
        self.plot_preview.getAxis("bottom").setPen(pg.mkPen("#94a3b8"))
        self.plot_preview.getAxis("bottom").setTextPen(pg.mkPen("#cbd5e1"))
        self.plot_preview.getAxis("left").setPen(pg.mkPen("#94a3b8"))
        self.plot_preview.getAxis("left").setTextPen(pg.mkPen("#cbd5e1"))
        self.plot_preview.setLabel("bottom", "<span style='color:#cbd5e1;'>Time</span>", units="s")
        self.plot_preview.setLabel("left", "<span style='color:#cbd5e1;'>Source Level</span>", units="V/A")
        self.plot_preview.showGrid(x=True, y=True, alpha=0.25)
        self.plot_preview.addLegend(labelTextColor="#f8fafc")
        pv_v.addWidget(self.plot_preview, 1)

        self.lbl_summary = QLabel()
        self.lbl_summary.setStyleSheet(
            "background-color: #eff6ff; color: #0b2545; border: 1px solid #bfdbfe; border-radius: 4px; padding: 6px; font-weight: bold;"
        )
        pv_v.addWidget(self.lbl_summary)
        split.addWidget(pv_widget)
        split.setSizes([440, 660])
        layout.addWidget(split, 1)

        # 4. Bottom Action Row (Reset Custom Waveform / Cancel / Apply Only / Apply & Run Immediately on SMU)
        bot = QHBoxLayout()

        self.btn_reset_waveform = QPushButton("↺ 커스텀 파형 해제 (기본 LINEAR 복귀)")
        self.btn_reset_waveform.setToolTip("적용된 커스텀 파형 설정을 모두 초기화하고 메인 화면의 기본 LINEAR 스위프 모드로 되돌립니다.")
        self.btn_reset_waveform.setStyleSheet(
            "background-color: #fee2e2; color: #991b1b; border: 1px solid #fca5a5; font-weight: bold; padding: 7px 14px; border-radius: 5px;"
        )
        self.btn_reset_waveform.clicked.connect(self._on_reset_to_default)
        bot.addWidget(self.btn_reset_waveform)

        bot.addStretch(1)

        btn_cancel = QPushButton("취소")
        btn_cancel.clicked.connect(self.reject)
        bot.addWidget(btn_cancel)

        self.btn_apply_only = QPushButton("✔ 설정만 메인 화면에 적용")
        self.btn_apply_only.setStyleSheet(
            "background-color: #2563eb; color: white; font-weight: bold; padding: 8px 18px; border-radius: 5px;"
        )
        self.btn_apply_only.clicked.connect(lambda: self._finalize(run_now=False))
        bot.addWidget(self.btn_apply_only)

        self.btn_apply_and_run = QPushButton("🚀 적용 후 즉시 장비 출력 & 측정")
        self.btn_apply_and_run.setStyleSheet(
            "background-color: #16a34a; color: white; font-weight: bold; padding: 8px 18px; border-radius: 5px;"
        )
        self.btn_apply_and_run.clicked.connect(lambda: self._finalize(run_now=True))
        bot.addWidget(self.btn_apply_and_run)

        layout.addLayout(bot)

    def _on_repeat_changed(self, val: int) -> None:
        if val <= 0:
            self.result_repeat_mode = RepeatMode.CONTINUOUS
            self.result_repeat_count = 1
        elif val == 1:
            self.result_repeat_mode = RepeatMode.SINGLE
            self.result_repeat_count = 1
        else:
            self.result_repeat_mode = RepeatMode.FINITE
            self.result_repeat_count = val
        self.refresh_preview()

    def generate_easy_waveform(self) -> None:
        max_lim = self.profile.max_dc_voltage_v if self.source_func == SourceFunction.VOLTAGE else self.profile.max_dc_current_a
        unit = "V" if self.source_func == SourceFunction.VOLTAGE else "A"
        high_v, notice_h = clamp_value_with_notice(self.spn_easy_high.value(), -max_lim, max_lim, "High 레벨", self.profile.model, unit)
        if notice_h:
            self.spn_easy_high.setValue(high_v)
            QMessageBox.warning(self, f"{self.profile.model} 범위 초과 자동 조정", notice_h)
        low_v, notice_l = clamp_value_with_notice(self.spn_easy_low.value(), -max_lim, max_lim, "Low 레벨", self.profile.model, unit)
        if notice_l:
            self.spn_easy_low.setValue(low_v)
            QMessageBox.warning(self, f"{self.profile.model} 범위 초과 자동 조정", notice_l)

        res = WaveformBuilder.build_easy_waveform(
            shape=self.cmb_easy_shape.currentText(),
            high_level=high_v,
            low_level=low_v,
            duration_s=self.spn_easy_dur.value(),
            repeat_count=self.spn_easy_repeat.value(),
            compliance=self.default_compliance,
        )
        self.result_steps = res.steps
        self.result_repeat_mode = res.repeat_mode
        self.result_repeat_count = res.repeat_count
        self._populate_table()
        self.refresh_preview()

    def _populate_table(self) -> None:
        self._populating_table = True
        try:
            self.tbl_waveform.setRowCount(len(self.result_steps))
            for r, s in enumerate(self.result_steps):
                it_idx = QTableWidgetItem(str(s.step_index))
                it_idx.setFlags(it_idx.flags() & ~Qt.ItemFlag.ItemIsEditable)
                it_idx.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.tbl_waveform.setItem(r, 0, it_idx)
                self.tbl_waveform.setItem(r, 1, QTableWidgetItem(f"{s.duration_s:.6g}"))
                self.tbl_waveform.setItem(r, 2, QTableWidgetItem(f"{s.level:.6g}"))
                self.tbl_waveform.setItem(r, 3, QTableWidgetItem(f"{(s.compliance_limit or self.default_compliance):.6g}"))
                self.tbl_waveform.setItem(r, 4, QTableWidgetItem("1" if s.measure_enable else "0"))
        finally:
            self._populating_table = False

    def _read_table_steps(self) -> list[WaveformStep]:
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

    def _on_table_cell_changed(self, *_) -> None:
        if self._populating_table:
            return
        self.result_steps = self._read_table_steps()
        self.refresh_preview()

    def _add_step(self) -> None:
        self.result_steps = self._read_table_steps()
        last_lvl = self.result_steps[-1].level if self.result_steps else 1.0
        self.result_steps.append(
            WaveformStep(len(self.result_steps) + 1, self.default_delay_s, last_lvl, self.default_compliance, True)
        )
        self._populate_table()
        self.refresh_preview()

    def _delete_selected_steps(self) -> None:
        self.result_steps = self._read_table_steps()
        sel_rows = sorted({idx.row() for idx in self.tbl_waveform.selectedIndexes()})
        if not sel_rows and self.tbl_waveform.rowCount() > 0:
            sel_rows = [self.tbl_waveform.rowCount() - 1]
        self.result_steps = WaveformBuilder.delete_steps(self.result_steps, sel_rows)
        self._populate_table()
        self.refresh_preview()

    def _clear_steps(self) -> None:
        self.result_steps = [WaveformStep(1, self.default_delay_s, 0.0, self.default_compliance, True)]
        self._populate_table()
        self.refresh_preview()

    def _import_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "파형 CSV/Excel 불러오기", "", "Waveform Files (*.csv *.xlsx *.xls)")
        if not path:
            return
        try:
            self.result_steps = WaveformBuilder.import_steps_from_file(path, self.default_compliance)
            self._populate_table()
            self.refresh_preview()
        except Exception as exc:
            QMessageBox.critical(self, "파형 임포트 오류", str(exc))

    def _export_file(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "파형 CSV 저장", "custom_waveform.csv", "CSV Files (*.csv)")
        if not path:
            return
        self.result_steps = self._read_table_steps()
        WaveformBuilder.export_steps_to_csv(self.result_steps, path)
        QMessageBox.information(self, "저장 완료", f"파형 테이블이 저장되었습니다:\n{path}")

    def refresh_preview(self) -> None:
        recipe = SweepRecipe(
            sweep_type=SweepType.LIST,
            source_func=self.source_func,
            delay_s=self.default_delay_s,
            compliance_limit=self.default_compliance,
            repeat_mode=self.result_repeat_mode,
            repeat_count=self.result_repeat_count,
            end_Return_to_bias=self.end_return_to_bias,
            custom_steps=list(self.result_steps),
        )
        pv = WaveformBuilder.generate_preview(recipe)
        self.plot_preview.clear()
        self.plot_preview.plot(pv.time_s, pv.level_va, pen=pg.mkPen("#38bdf8", width=2.4), name="Source Waveform")
        if len(pv.measure_times_s) > 0:
            self.plot_preview.plot(
                pv.measure_times_s,
                pv.measure_levels_va,
                pen=None,
                symbol="o",
                symbolSize=6,
                symbolBrush="#f472b6",
                name="Measure Points",
            )
        rep_txt = "무한연속(-1)" if pv.is_infinite_continuous else f"×{self.result_repeat_count}회 ({pv.total_duration_s:.4f}s, {pv.total_measure_points:,} pts)"
        self.summary_badge_text = f"{len(self.result_steps)}스텝 / {rep_txt}"
        self.lbl_summary.setText(
            f"[파형 프리뷰] 1주기: {pv.single_cycle_duration_s:.4f}s ({pv.single_cycle_points} pts)  |  총 출력: {rep_txt}"
        )

    def _on_reset_to_default(self) -> None:
        self.reset_to_default_requested = True
        self.run_immediately_requested = False
        self.accept()

    def _finalize(self, run_now: bool) -> None:
        self.result_steps = self._read_table_steps()
        if not self.result_steps:
            QMessageBox.warning(self, "스텝 없음", "최소 1개 이상의 파형 스텝이 필요합니다.")
            return
        self.run_immediately_requested = run_now
        self.accept()
