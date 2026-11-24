"""Requirement 4 Implementation: Detachable / Pop-out Graph Window (`DetachedGraphWindow`).

Allows the user to pop out the measurement graph into one or more separate resizable windows
with independent I-V / Time-Domain view switching, Log(|I|) toggle, Multi-Channel Swim Lanes / Overlay,
Interactive Dual Cursors (ΔX, ΔY, Slope, 1/R), and direct PNG/SVG image export.
"""

from __future__ import annotations

import numpy as np
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)
import pyqtgraph as pg

from smu_app.analysis.iv_pulse_analyzer import (
    compute_dual_cursor_metrics,
    lttb_downsample,
)
from smu_app.core.models import MeasurementResult
from smu_app.storage.exporter import DataExporter
from smu_app.ui.connection_wizard import fit_and_center_window


class DetachedGraphWindow(QDialog):
    """Independent pop-out window displaying interactive SMU measurement curves and Dual Cursors."""

    _PALETTE = ["#0070C0", "#059669", "#D97706", "#7C3AED", "#DC2626", "#0891B2", "#4F46E5", "#B45309"]

    def __init__(
        self,
        result: MeasurementResult | None,
        run_history: list[MeasurementResult] | None = None,
        exporter: DataExporter | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.WindowMaximizeButtonHint
            | Qt.WindowType.WindowMinimizeButtonHint
            | Qt.WindowType.WindowCloseButtonHint
        )
        self.result = result
        self.run_history = list(run_history or [])
        self.exporter = exporter or DataExporter()

        title_suffix = f"{result.model} ({result.recipe_name})" if result else "대기 중"
        self.setWindowTitle(f"📈 SMU 분리형 상세 분석 그래프 창 — {title_suffix}")
        self._build_ui()
        fit_and_center_window(self, target_w_ratio=0.78, target_h_ratio=0.78, min_w=820, min_h=540)
        self.update_data(result, self.run_history)

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel("<b>그래프 뷰 모드:</b>"))
        self.cmb_view = QComboBox()
        self.cmb_view.addItems([
            "I-V Characteristic (V vs I)",
            "Time-Domain Waveform (Time vs I)",
            "Time-Domain Voltage (Time vs V)",
        ])
        self.cmb_view.currentTextChanged.connect(self.refresh_plot)
        toolbar.addWidget(self.cmb_view)

        self.chk_log_y = QCheckBox("Y축 Log(|Y|) 스케일")
        self.chk_log_y.toggled.connect(self.refresh_plot)
        toolbar.addWidget(self.chk_log_y)

        self.chk_per_channel = QCheckBox("다중 장비(Node.CH) 색상 분리 표시")
        self.chk_per_channel.setChecked(True)
        self.chk_per_channel.toggled.connect(self.refresh_plot)
        toolbar.addWidget(self.chk_per_channel)

        self.chk_cursors = QCheckBox("듀얼 커서 (ΔX, ΔY, 1/R)")
        self.chk_cursors.setChecked(True)
        self.chk_cursors.toggled.connect(self.refresh_plot)
        toolbar.addWidget(self.chk_cursors)

        btn_clear = QPushButton("🧹 그래프 비우기")
        btn_clear.clicked.connect(lambda: self.update_data(None, []))
        toolbar.addWidget(btn_clear)

        btn_png = QPushButton("🖼 PNG/SVG 이미지 저장")
        btn_png.clicked.connect(self._export_image)
        toolbar.addWidget(btn_png)

        layout.addLayout(toolbar)

        self.lbl_cursor = QLabel("커서 분석 대기 중")
        self.lbl_cursor.setStyleSheet(
            "background-color: #EFF6FF; color: #0B1F3A; padding: 5px 10px; font-family: Consolas, '맑은 고딕'; font-weight: bold;"
        )
        layout.addWidget(self.lbl_cursor)

        pg.setConfigOptions(antialias=True, background="w", foreground="k")
        self.plot_widget = pg.PlotWidget(title="Detached High-Resolution SMU Graph")
        self.plot_widget.showGrid(x=True, y=True, alpha=0.35)
        self.plot_widget.addLegend()

        self.cursor1 = pg.InfiniteLine(pos=0.2, angle=90, movable=True, pen=pg.mkPen("#D97706", width=1.8, style=Qt.PenStyle.DashLine))
        self.cursor2 = pg.InfiniteLine(pos=0.8, angle=90, movable=True, pen=pg.mkPen("#059669", width=1.8, style=Qt.PenStyle.DashLine))
        self.cursor1.sigPositionChanged.connect(self._update_cursor_readout)
        self.cursor2.sigPositionChanged.connect(self._update_cursor_readout)
        layout.addWidget(self.plot_widget, 1)

    def update_data(self, result: MeasurementResult | None, run_history: list[MeasurementResult] | None = None) -> None:
        self.result = result
        if run_history is not None:
            self.run_history = list(run_history)
        self.refresh_plot()

    def _extract_xy(self, pts) -> tuple[np.ndarray, np.ndarray, str, str]:
        idx = self.cmb_view.currentIndex()
        if idx == 0:
            x = np.array([p.voltage_v for p in pts], dtype=np.float64)
            y = np.array([p.current_a for p in pts], dtype=np.float64)
            xu, yu = "V", "A"
        elif idx == 1:
            x = np.array([p.timestamp_s for p in pts], dtype=np.float64)
            y = np.array([p.current_a for p in pts], dtype=np.float64)
            xu, yu = "s", "A"
        else:
            x = np.array([p.timestamp_s for p in pts], dtype=np.float64)
            y = np.array([p.voltage_v for p in pts], dtype=np.float64)
            xu, yu = "s", "V"

        if self.chk_log_y.isChecked():
            y = np.log10(np.maximum(np.abs(y), 1e-15))
            yu = f"log10({yu})"
        return x, y, xu, yu

    def refresh_plot(self) -> None:
        self.plot_widget.clear()
        if not self.result or not self.result.points:
            return

        x_all, y_all, xu, yu = self._extract_xy(self.result.points)
        self.plot_widget.setLabel("bottom", "Time" if xu == "s" else "Voltage", units=xu)
        self.plot_widget.setLabel("left", "Value", units=yu)

        if self.chk_per_channel.isChecked():
            grouped: dict[str, list] = {}
            for p in self.result.points:
                key = f"Node{p.node_id}.{p.channel}"
                grouped.setdefault(key, []).append(p)
            for c_idx, (ch_key, ch_pts) in enumerate(grouped.items()):
                cx, cy, _, _ = self._extract_xy(ch_pts)
                dx, dy = lttb_downsample(cx, cy, threshold=2000)
                color = self._PALETTE[c_idx % len(self._PALETTE)]
                self.plot_widget.plot(
                    dx,
                    dy,
                    pen=pg.mkPen(color=color, width=2),
                    symbol="o",
                    symbolSize=5,
                    symbolBrush=color,
                    name=f"{ch_key} ({len(ch_pts)} pts)",
                )
        else:
            dx, dy = lttb_downsample(x_all, y_all, threshold=2000)
            self.plot_widget.plot(
                dx,
                dy,
                pen=pg.mkPen(color="#0070C0", width=2),
                symbol="o",
                symbolSize=5,
                symbolBrush="#0070C0",
                name=f"{self.result.model} ({len(self.result.points)} pts)",
            )

        if self.chk_cursors.isChecked() and len(x_all) >= 2:
            x_min, x_max = float(np.min(x_all)), float(np.max(x_all))
            span = max(1e-9, x_max - x_min)
            self.cursor1.setPos(x_min + 0.25 * span)
            self.cursor2.setPos(x_min + 0.75 * span)
            self.plot_widget.addItem(self.cursor1)
            self.plot_widget.addItem(self.cursor2)
            self._update_cursor_readout()

    def _update_cursor_readout(self) -> None:
        if not self.result or not self.result.points:
            return
        x_all, y_all, xu, yu = self._extract_xy(self.result.points)
        c = compute_dual_cursor_metrics(x_all, y_all, float(self.cursor1.value()), float(self.cursor2.value()))
        self.lbl_cursor.setText(
            f"[분리 창 듀얼 커서] C1=({c.x1:.4g}{xu}, {c.y1:.4e}{yu}) | C2=({c.x2:.4g}{xu}, {c.y2:.4e}{yu}) | "
            f"ΔX={c.delta_x:.4g}{xu}, ΔY={c.delta_y:.4e}{yu} | 기울기={c.slope:.3e} | 1/Slope={c.inverse_slope_ohm:.3e} Ω"
        )

    def _export_image(self) -> None:
        if not self.result:
            return
        mode = "TIME" if self.cmb_view.currentIndex() >= 1 else "IV"
        png = self.exporter.export_plot_image(self.result, fmt="png", mode=mode)
        QMessageBox.information(self, "그래프 저장 완료", f"분리된 그래프 이미지가 저장되었습니다:\n{png}")
