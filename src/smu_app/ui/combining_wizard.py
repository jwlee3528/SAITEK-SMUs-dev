"""Phase 2 Interactive Output Combining Wizard Dialog (Parallel 2x Current / Series 2x Voltage).

Implements v3.0 Section 03-3 (Combining SMUs Wizard) & Challenge 5 Resolution:
- Interactive wiring diagram guide (Parallel Current vs Series Voltage).
- Enforces mandatory 4-step physical wiring verification checkboxes before enabling the 'Apply' button.
- Blocks forbidden combinations (Voltage sources in parallel, Current sources in series, or 2606B).
- Automatically computes 50/50 split source level and sets Slave compliance 10% lower than Master.
"""

from __future__ import annotations

from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from smu_app.core.models import CombiningMode, SourceFunction
from smu_app.core.profiles import SMUModelProfile
from smu_app.sync.cluster_manager import (
    CombinedOutputConfig,
    CombiningChecklistState,
    TSPLinkClusterManager,
)


class OutputCombiningWizardDialog(QDialog):
    """Modal wizard dialog for configuring and verifying 2-SMU Parallel or Series output combining."""

    def __init__(self, profile: SMUModelProfile, parent=None) -> None:
        super().__init__(parent)
        self.profile = profile
        self.combined_config: CombinedOutputConfig | None = None
        self.setWindowTitle(f"SMU 출력 결합(Combining SMUs) 안전 마법사 — {profile.display_name}")
        self.resize(640, 560)
        self._build_ui()
        self._update_diagram_and_preview()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        # 1. Mode & Target Parameters
        grp_mode = QGroupBox("1. 결합 방식 및 목표 출력 설정")
        form = QFormLayout(grp_mode)

        self.cmb_mode = QComboBox()
        self.cmb_mode.addItems([
            CombiningMode.PARALLEL_CURRENT.value,
            CombiningMode.SERIES_VOLTAGE.value,
        ])
        self.cmb_mode.currentTextChanged.connect(self._on_mode_changed)
        form.addRow("출력 결합 모드:", self.cmb_mode)

        self.lbl_source_func = QLabel("CURRENT (전류원 병렬 강제)")
        self.lbl_source_func.setStyleSheet("font-weight: bold; color: #0070C0;")
        form.addRow("자동 강제 소스 모드:", self.lbl_source_func)

        self.spn_target = QDoubleSpinBox()
        self.spn_target.setDecimals(4)
        self.spn_target.setRange(-2000.0, 2000.0)
        self.spn_target.setValue(min(10.0, self.profile.max_pulse_current_a * 1.5))
        self.spn_target.valueChanged.connect(self._update_diagram_and_preview)
        form.addRow("목표 합산 출력 (Total A 또는 V):", self.spn_target)

        self.spn_comp = QDoubleSpinBox()
        self.spn_comp.setDecimals(4)
        self.spn_comp.setRange(1e-5, 2000.0)
        self.spn_comp.setValue(20.0)
        self.spn_comp.valueChanged.connect(self._update_diagram_and_preview)
        form.addRow("Master Compliance 한계 (V 또는 A):", self.spn_comp)

        layout.addWidget(grp_mode)

        # 2. Wiring Diagram & Automatic 10% Margin Preview
        grp_diag = QGroupBox("2. 매뉴얼 표준 결선 다이어그램 및 10% 마진 자동 분배")
        diag_layout = QVBoxLayout(grp_diag)
        self.lbl_diagram = QLabel()
        self.lbl_diagram.setWordWrap(True)
        self.lbl_diagram.setStyleSheet(
            "background-color: #0B1F3A; color: #F8FAFC; padding: 10px; font-family: Consolas, '맑은 고딕'; border-radius: 4px;"
        )
        diag_layout.addWidget(self.lbl_diagram)
        layout.addWidget(grp_diag)

        # 3. Mandatory 4-Step Physical Wiring Verification Checklist Gate
        grp_chk = QGroupBox("3. 필수 물리 결선 확인 안전 게이트 (4개 항목 모두 확인 시 활성화)")
        chk_layout = QVBoxLayout(grp_chk)

        self.chk_1_off = QCheckBox(
            "① 결선 변경 전 두 SMU(Master/Slave)의 Output이 모두 OFF 상태임을 확인했습니다."
        )
        self.chk_2_polarity = QCheckBox(
            "② [병렬=전류원 동극성 병렬 / 직렬=전압원 직렬] 규칙을 준수했으며, 전압원 병렬·전류원 직렬이 아님을 확인했습니다."
        )
        self.chk_3_sense = QCheckBox(
            "③ 4-Wire Sense 리드 및 Guard 배선이 매뉴얼 Combining SMUs 다이어그램과 일치함을 확인했습니다."
        )
        self.chk_4_shield = QCheckBox(
            "④ 고전압/고전류(예: 2651A 100A/80V) 시험용 DUT Safety Shield 및 인터락 체결을 확인했습니다."
        )

        for cb in (self.chk_1_off, self.chk_2_polarity, self.chk_3_sense, self.chk_4_shield):
            cb.toggled.connect(self._update_gate_button)
            chk_layout.addWidget(cb)

        layout.addWidget(grp_chk)

        # 4. Action Buttons
        btn_row = QHBoxLayout()
        self.btn_apply = QPushButton("✔ 결합 구성 검증 및 적용 (안전 게이트 통과 필요)")
        self.btn_apply.setEnabled(False)
        self.btn_apply.setStyleSheet(
            "background-color: #0E7490; color: white; font-weight: bold; padding: 8px 16px;"
        )
        self.btn_apply.clicked.connect(self._on_apply)

        btn_cancel = QPushButton("취소")
        btn_cancel.clicked.connect(self.reject)

        btn_row.addStretch(1)
        btn_row.addWidget(btn_cancel)
        btn_row.addWidget(self.btn_apply)
        layout.addLayout(btn_row)

    def _on_mode_changed(self, mode_str: str) -> None:
        if mode_str == CombiningMode.PARALLEL_CURRENT.value:
            self.lbl_source_func.setText("CURRENT (전류원 병렬 강제 — 전압원 병렬 원천 차단)")
        else:
            self.lbl_source_func.setText("VOLTAGE (전압원 직렬 강제 — 전류원 직렬 원천 차단)")
        self._update_diagram_and_preview()

    def _update_diagram_and_preview(self) -> None:
        mode = CombiningMode(self.cmb_mode.currentText())
        tot = self.spn_target.value()
        comp = self.spn_comp.value()
        half = tot / 2.0
        slave_comp = round(comp * 0.90, 6)

        if mode == CombiningMode.PARALLEL_CURRENT:
            diag = (
                "[병렬 고전류 결합 (Parallel Current Combining)]\n"
                "  Master HI ───┬───► DUT HI (+)\n"
                "  Slave  HI ───┘\n"
                "  Master LO ───┬───► DUT LO (-)\n"
                "  Slave  LO ───┘\n\n"
                f"• 합산 출력 전류: {tot:.4g} A  ➜  Master: {half:.4g} A + Slave: {half:.4g} A (50:50 분담)\n"
                f"• 전압 Compliance: Master = {comp:.4g} V  |  Slave = {slave_comp:.4g} V (10% 마진 자동 차감)"
            )
        else:
            diag = (
                "[직렬 고전압 결합 (Series Voltage Combining)]\n"
                "  Master HI (+) ──────────► DUT HI (+)\n"
                "  Master LO ───► Slave HI   (직렬 브릿지 연결)\n"
                "  Slave  LO (-) ──────────► DUT LO (-)\n\n"
                f"• 합산 출력 전압: {tot:.4g} V  ➜  Master: {half:.4g} V + Slave: {half:.4g} V (50:50 분담)\n"
                f"• 전류 Compliance: Master = {comp:.4g} A  |  Slave = {slave_comp:.4g} A (10% 마진 자동 차감)"
            )
        if not self.profile.combining_supported:
            diag += f"\n\n[경고] {self.profile.model} 모델은 하드웨어 출력 결합이 금지된 모델입니다!"
        self.lbl_diagram.setText(diag)

    def _get_checklist_state(self) -> CombiningChecklistState:
        return CombiningChecklistState(
            power_off_verified=self.chk_1_off.isChecked(),
            cable_polarity_verified=self.chk_2_polarity.isChecked(),
            sense_lead_verified=self.chk_3_sense.isChecked(),
            safety_shield_verified=self.chk_4_shield.isChecked(),
        )

    def _update_gate_button(self) -> None:
        chk = CombiningChecklistState(
            power_off_verified=self.chk_1_off.isChecked(),
            cable_polarity_verified=self.chk_2_polarity.isChecked(),
            sense_lead_verified=self.chk_3_sense.isChecked(),
            safety_shield_verified=self.chk_4_shield.isChecked(),
        )
        self.btn_apply.setEnabled(chk.all_verified)

    def _on_apply(self) -> None:
        mode = CombiningMode(self.cmb_mode.currentText())
        s_func = (
            SourceFunction.CURRENT
            if mode == CombiningMode.PARALLEL_CURRENT
            else SourceFunction.VOLTAGE
        )
        chk = CombiningChecklistState(
            power_off_verified=self.chk_1_off.isChecked(),
            cable_polarity_verified=self.chk_2_polarity.isChecked(),
            sense_lead_verified=self.chk_3_sense.isChecked(),
            safety_shield_verified=self.chk_4_shield.isChecked(),
        )
        try:
            self.combined_config = TSPLinkClusterManager.build_combined_output_config(
                profile=self.profile,
                mode=mode,
                source_func=s_func,
                total_target_level=self.spn_target.value(),
                total_compliance_limit=self.spn_comp.value(),
                checklist=chk,
            )
        except Exception as exc:
            QMessageBox.critical(self, "출력 결합 안전 검증 차단", str(exc))
            return
        self.accept()
