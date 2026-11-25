"""Standalone Popup Dialog for Multi-SMU (1~10 Nodes) & Per-Channel Independent Waveform/Source Configuration.

Features:
- Dedicated wide dialog for configuring 1~10 SMU nodes and 1/2/4-CH logical channels (e.g. 2606B 4-CH x N).
- Allows independent per-node / per-channel override of Waveform Mode (LINEAR/DUAL/LIST/PULSE) and
  Source Function (VOLTAGE vs CURRENT), Start~Stop levels, Compliance limit, and Role (PARALLEL_DUT / FET_GATE / FET_DRAIN / FET_SOURCE).
"""

from __future__ import annotations

from dataclasses import replace
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from smu_app.core.models import LogicalChannel, SourceFunction, SweepType
from smu_app.core.profiles import SMUModelProfile
from smu_app.sync.cluster_manager import ChannelRole
from smu_app.ui.connection_wizard import fit_and_center_window


class MultiChannelConfigDialog(QDialog):
    """Dedicated popup window for 1~10 SMU Node-Channel mapping & independent per-unit waveform/source separation."""

    def __init__(
        self,
        profile: SMUModelProfile,
        logical_channels: list[LogicalChannel],
        variant_code: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.profile = profile
        self.variant_code = variant_code or profile.model
        self.result_channels: list[LogicalChannel] = [replace(c) for c in logical_channels]

        self.setWindowTitle(f"🔗 다중 장비·채널별 개별 파형/소스 설정 (1~10대) — {profile.display_name}")
        self._build_ui()
        fit_and_center_window(self, target_w_ratio=0.82, target_h_ratio=0.70, min_w=980, min_h=500)
        self._populate_table()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        sync_ok = self.profile.supports_tsplink_for_variant(self.variant_code)
        sync_badge = (
            "✅ TSP-Link 3-Line 하드웨어 동기화 지원 (<1µs 정밀 동기화)"
            if sync_ok
            else f"⚠ {self.variant_code} 모델은 TSP-Link 미지원 — SW 멀티세션 동기화(1~20ms Skew)로 동작합니다."
        )
        info = QLabel(
            f"<b>다중 장비 논리 채널 매핑 및 개별 파형/소스 분리 설정 ({len(self.result_channels)}개 채널):</b><br>"
            f"• {sync_badge}<br>"
            "• 각 행의 <b>[개별 설정]</b>을 체크하면 해당 장비/채널만 메인 설정과 다르게 <b>파형 모드(LINEAR/DUAL/LIST 등)</b>와 "
            "<b>소스 기능(VOLTAGE vs CURRENT)</b>, 시작~종료 레벨, Compliance Limit을 독립 제어합니다."
        )
        info.setWordWrap(True)
        info.setStyleSheet(
            "background-color: #eff6ff; color: #0b2545; border: 1px solid #bfdbfe; border-radius: 5px; padding: 8px;"
        )
        layout.addWidget(info)

        # Quick Role Preset Row
        preset_row = QHBoxLayout()
        btn_all_par = QPushButton("모두 병렬 DUT 테스트(PARALLEL_DUT)로 설정")
        btn_all_par.clicked.connect(self._preset_all_parallel)
        btn_fet = QPushButton("3단자 FET 프리셋 (CH1=DRAIN, CH2=GATE, CH3=SOURCE)")
        btn_fet.clicked.connect(self._preset_fet_roles)
        preset_row.addWidget(btn_all_par)
        preset_row.addWidget(btn_fet)
        preset_row.addStretch(1)
        layout.addLayout(preset_row)

        self.tbl_channels = QTableWidget(0, 9)
        self.tbl_channels.setAlternatingRowColors(True)
        self.tbl_channels.verticalHeader().setVisible(False)
        self.tbl_channels.setHorizontalHeaderLabels([
            "논리 채널 ID",
            "TSP 객체 경로",
            "활성화",
            "할당 역할 (Role)",
            "개별 설정",
            "장비별 파형 모드",
            "장비별 소스 기능",
            "시작, 종료 (Start, Stop)",
            "개별 Limit",
        ])
        self.tbl_channels.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.tbl_channels, 1)

        bot = QHBoxLayout()
        bot.addStretch(1)
        btn_cancel = QPushButton("취소")
        btn_cancel.clicked.connect(self.reject)
        bot.addWidget(btn_cancel)

        btn_apply = QPushButton("✔ 다중 채널 설정 메인 화면에 적용")
        btn_apply.setStyleSheet(
            "background-color: #2563eb; color: white; font-weight: bold; padding: 8px 20px; border-radius: 5px;"
        )
        btn_apply.clicked.connect(self._on_apply)
        bot.addWidget(btn_apply)
        layout.addLayout(bot)

    def _populate_table(self) -> None:
        self.tbl_channels.setRowCount(len(self.result_channels))
        for r, lch in enumerate(self.result_channels):
            self.tbl_channels.setItem(r, 0, QTableWidgetItem(lch.label))
            self.tbl_channels.setItem(r, 1, QTableWidgetItem(lch.tsp_prefix))

            chk_en = QCheckBox("ON")
            chk_en.setChecked(lch.enabled)
            chk_en.toggled.connect(lambda checked, idx=r: self._set_attr(idx, "enabled", checked))
            self.tbl_channels.setCellWidget(r, 2, chk_en)

            cmb_role = QComboBox()
            for role in ChannelRole:
                cmb_role.addItem(role.value)
            cmb_role.setCurrentText(lch.role)
            cmb_role.currentTextChanged.connect(lambda val, idx=r: self._set_attr(idx, "role", val))
            self.tbl_channels.setCellWidget(r, 3, cmb_role)

            chk_ind = QCheckBox("개별 분리")
            chk_ind.setChecked(lch.independent_override)
            chk_ind.toggled.connect(lambda checked, idx=r: self._set_attr(idx, "independent_override", checked))
            self.tbl_channels.setCellWidget(r, 4, chk_ind)

            cmb_swp = QComboBox()
            for st in SweepType:
                cmb_swp.addItem(st.value)
            cmb_swp.setCurrentText(lch.override_sweep_type)
            cmb_swp.currentTextChanged.connect(lambda val, idx=r: self._set_attr(idx, "override_sweep_type", val))
            self.tbl_channels.setCellWidget(r, 5, cmb_swp)

            cmb_src = QComboBox()
            cmb_src.addItems([SourceFunction.VOLTAGE.value, SourceFunction.CURRENT.value])
            cmb_src.setCurrentText(lch.override_source_func)
            cmb_src.currentTextChanged.connect(lambda val, idx=r: self._set_attr(idx, "override_source_func", val))
            self.tbl_channels.setCellWidget(r, 6, cmb_src)

            self.tbl_channels.setItem(
                r, 7, QTableWidgetItem(f"{lch.override_start_level:g}, {lch.override_stop_level:g}")
            )
            self.tbl_channels.setItem(r, 8, QTableWidgetItem(f"{lch.override_compliance:g}"))

    def _set_attr(self, idx: int, attr: str, val) -> None:
        if 0 <= idx < len(self.result_channels):
            setattr(self.result_channels[idx], attr, val)

    def _preset_all_parallel(self) -> None:
        for ch in self.result_channels:
            ch.role = ChannelRole.PARALLEL_DUT.value
        self._populate_table()

    def _preset_fet_roles(self) -> None:
        roles = [ChannelRole.FET_DRAIN.value, ChannelRole.FET_GATE.value, ChannelRole.FET_SOURCE.value]
        for idx, ch in enumerate(self.result_channels):
            ch.role = roles[idx] if idx < len(roles) else ChannelRole.PARALLEL_DUT.value
        self._populate_table()

    def _sync_text_columns(self) -> None:
        for r, lch in enumerate(self.result_channels):
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

    def _on_apply(self) -> None:
        self._sync_text_columns()
        self.accept()
