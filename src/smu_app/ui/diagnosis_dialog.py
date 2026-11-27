"""Auto-Measurement Coach Popup Dialog (15-Rule Diagnosis Report + Model Thresholds + 1-Click Auto-Fix).

Displayed when the user clicks the compact 1-line diagnosis banner in the main window.
"""

from __future__ import annotations

from typing import Callable

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QFont
from PyQt6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from smu_app.core.models import DiagnosisItem, DiagnosisReport, DiagnosisSeverity
from smu_app.ui.connection_wizard import fit_and_center_window


class AutoDiagnosisDialog(QDialog):
    """Interactive 'Auto-Measurement Coach' popup displaying detected setup issues,
    model-calibrated thresholds, 1-click Auto-Fix buttons, and the full 15-item checklist.
    """

    def __init__(
        self,
        report: DiagnosisReport,
        apply_fix_callback: Callable[[DiagnosisItem], str] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.report = report
        self.apply_fix_callback = apply_fix_callback
        self.applied_fixes: list[str] = []

        self.setWindowTitle(f"🩺 SMU 자동 측정 코치 — 15대 셋업·품질 자동 진단 ({report.model})")
        self._build_ui()
        fit_and_center_window(self, target_w_ratio=0.72, target_h_ratio=0.78, min_w=860, min_h=560)

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        layout.setContentsMargins(12, 10, 12, 10)

        # -----------------------------------------------------------------
        # 1. HEADER STATUS BANNER
        # -----------------------------------------------------------------
        r = self.report
        if r.critical_count > 0:
            bg, bd, fg = "#fef2f2", "#ef4444", "#991b1b"
            headline = f"🛑 셋업 오류 감지: 심각 {r.critical_count}건 · 주의 {r.warning_count}건 (전체 {r.total_rules_checked}개 항목 검사)"
        elif r.warning_count > 0:
            bg, bd, fg = "#fffbeb", "#f59e0b", "#92400e"
            headline = f"⚠ 측정 셋업 주의 {r.warning_count}건 감지 (전체 {r.total_rules_checked}개 항목 검사)"
        else:
            bg, bd, fg = "#ecfdf5", "#10b981", "#065f46"
            headline = f"✅ 측정 셋업 및 데이터 품질 정상 — 전체 {r.total_rules_checked}개 자동 진단 항목 통과"

        header = QFrame()
        header.setStyleSheet(f"QFrame {{ background-color: {bg}; border: 1.5px solid {bd}; border-radius: 6px; }}")
        hv = QVBoxLayout(header)
        hv.setContentsMargins(12, 8, 12, 8)
        hv.setSpacing(3)

        lbl_h = QLabel(headline)
        lbl_h.setFont(QFont("맑은 고딕", 11, QFont.Weight.Bold))
        lbl_h.setStyleSheet(f"color: {fg}; border: none;")
        hv.addWidget(lbl_h)

        lbl_sub = QLabel(
            "※ 본 진단은 시료(DUT)의 물리적 불량 판정이 아닌, SMU 설정·배선·노이즈·타이밍 등 측정 과정의 이상을 자동 분석하는 '측정 코치'입니다."
        )
        lbl_sub.setStyleSheet("color: #475569; font-size: 8.5pt; border: none;")
        hv.addWidget(lbl_sub)
        layout.addWidget(header)

        # -----------------------------------------------------------------
        # 2. MODEL-SPECIFIC ADAPTIVE THRESHOLDS BADGE
        # -----------------------------------------------------------------
        if r.thresholds_summary:
            th_parts = [f"<b>{k}:</b> {v}" for k, v in r.thresholds_summary.items()]
            lbl_th = QLabel("🎯 <b>모델 맞춤형 진단 임계값 자동 적용:</b> " + "  |  ".join(th_parts))
            lbl_th.setWordWrap(True)
            lbl_th.setStyleSheet(
                "background-color: #eff6ff; color: #1e3a8a; border: 1px solid #bfdbfe; border-radius: 5px; padding: 6px 10px; font-size: 8.4pt;"
            )
            layout.addWidget(lbl_th)

        # -----------------------------------------------------------------
        # 3. TABS: [감지된 항목 & 자동 보정] + [전체 15대 항목 상세표]
        # -----------------------------------------------------------------
        tabs = QTabWidget()

        # Tab 1: Detected Issues & 1-Click Auto-Fix Cards
        issues_tab = QWidget()
        it_v = QVBoxLayout(issues_tab)
        it_v.setContentsMargins(4, 6, 4, 4)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        inner_v = QVBoxLayout(inner)
        inner_v.setSpacing(8)

        issues = r.issues
        if not issues:
            ok_card = QLabel(
                "🎉 <b>모든 15개 자동 진단 항목이 정상입니다!</b><br><br>"
                "• 출력/리미트 (Compliance, Readback 오차, 단자, 인터락): 정상<br>"
                "• 배선/접촉 (Open, Short, 2/4-Wire, 극성): 정상<br>"
                "• 노이즈/정밀도 (60Hz 노이즈, AutoRange 스파이크, 레인지, 드리프트): 정상<br>"
                "• 타이밍/동적특성 (Settling, 부하 발진, 샘플링 속도): 정상"
            )
            ok_card.setWordWrap(True)
            ok_card.setStyleSheet(
                "background-color: #f0fdf4; color: #166534; border: 1px solid #86efac; border-radius: 6px; padding: 14px; font-size: 9.5pt;"
            )
            inner_v.addWidget(ok_card)
        else:
            for item in issues:
                inner_v.addWidget(self._build_issue_card(item))
        inner_v.addStretch(1)
        scroll.setWidget(inner)
        it_v.addWidget(scroll)
        tabs.addTab(issues_tab, f"🩺 감지된 항목 & 자동 보정 ({len(issues)}건)")

        # Tab 2: Full 15-Item Diagnostic Matrix Table
        all_tab = QWidget()
        at_v = QVBoxLayout(all_tab)
        tbl = QTableWidget(len(r.items), 5)
        tbl.setAlternatingRowColors(True)
        tbl.verticalHeader().setVisible(False)
        tbl.setHorizontalHeaderLabels(["영역", "진단 항목", "판정", "감지 근거 (실측 데이터)", "권장 조치"])
        for row_i, item in enumerate(r.items):
            tbl.setItem(row_i, 0, QTableWidgetItem(item.category))
            tbl.setItem(row_i, 1, QTableWidgetItem(item.title))

            st_txt = (
                "🛑 심각"
                if item.severity == DiagnosisSeverity.CRITICAL
                else ("⚠ 주의" if item.severity == DiagnosisSeverity.WARNING else "✅ 정상")
            )
            st_item = QTableWidgetItem(st_txt)
            if item.severity == DiagnosisSeverity.CRITICAL:
                st_item.setBackground(QColor("#fee2e2"))
                st_item.setForeground(QColor("#991b1b"))
            elif item.severity == DiagnosisSeverity.WARNING:
                st_item.setBackground(QColor("#fef3c7"))
                st_item.setForeground(QColor("#92400e"))
            else:
                st_item.setBackground(QColor("#dcfce7"))
                st_item.setForeground(QColor("#166534"))
            tbl.setItem(row_i, 2, st_item)
            tbl.setItem(row_i, 3, QTableWidgetItem(item.evidence))
            tbl.setItem(row_i, 4, QTableWidgetItem(item.recommendation))

        hdr = tbl.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        hdr.setStretchLastSection(True)
        for col_i, w in enumerate([110, 205, 72, 235]):
            tbl.setColumnWidth(col_i, w)
        at_v.addWidget(tbl)
        tabs.addTab(all_tab, f"📋 전체 15대 진단 결과표 ({len(r.items)}항목)")

        layout.addWidget(tabs, 1)

        # -----------------------------------------------------------------
        # 4. BOTTOM STATUS & CLOSE ROW
        # -----------------------------------------------------------------
        bot = QHBoxLayout()
        self.lbl_fix_status = QLabel("💡 [🔧 자동 보정 적용] 버튼을 누르면 권장 설정이 메인 화면에 즉시 반영됩니다.")
        self.lbl_fix_status.setStyleSheet("color: #334155; font-weight: bold; font-size: 8.8pt;")
        bot.addWidget(self.lbl_fix_status, 1)

        btn_close = QPushButton("확인 (닫기)")
        btn_close.setStyleSheet(
            "background-color: #2563eb; color: white; font-weight: bold; padding: 6px 18px; border-radius: 4px;"
        )
        btn_close.clicked.connect(self.accept)
        bot.addWidget(btn_close)
        layout.addLayout(bot)

    def _build_issue_card(self, item: DiagnosisItem) -> QFrame:
        is_crit = item.severity == DiagnosisSeverity.CRITICAL
        bd = "#f87171" if is_crit else "#fbbf24"
        bg = "#fff5f5" if is_crit else "#fffbeb"
        badge = "🛑 심각" if is_crit else "⚠ 주의"
        badge_fg = "#b91c1c" if is_crit else "#b45309"

        card = QFrame()
        card.setStyleSheet(f"QFrame {{ background-color: {bg}; border: 1px solid {bd}; border-radius: 6px; }}")
        cv = QVBoxLayout(card)
        cv.setContentsMargins(10, 8, 10, 8)
        cv.setSpacing(4)

        top = QHBoxLayout()
        lbl_title = QLabel(f"<span style='color:{badge_fg};'><b>[{badge}]</b></span> <b>{item.title}</b>  "
                           f"<span style='color:#64748b; font-size:8.3pt;'>({item.category})</span>")
        lbl_title.setStyleSheet("border: none; font-size: 9.5pt;")
        top.addWidget(lbl_title, 1)

        if item.fix_action_id and self.apply_fix_callback is not None:
            btn_fix = QPushButton(f"🔧 {item.fix_label or '자동 보정 적용'}")
            btn_fix.setStyleSheet(
                "background-color: #2563eb; color: white; font-weight: bold; padding: 4px 10px; border-radius: 4px;"
            )
            btn_fix.clicked.connect(lambda _, it=item, b=btn_fix: self._on_apply_fix(it, b))
            top.addWidget(btn_fix)
        cv.addLayout(top)

        lbl_body = QLabel(
            f"• <b>감지 근거:</b> {item.evidence}<br>"
            f"• <b>원인 분석:</b> {item.cause}<br>"
            f"• <b>권장 해결책:</b> <span style='color:#0f766e;'><b>{item.recommendation}</b></span>"
        )
        lbl_body.setWordWrap(True)
        lbl_body.setStyleSheet("color: #1e293b; font-size: 8.8pt; border: none;")
        cv.addWidget(lbl_body)
        return card

    def _on_apply_fix(self, item: DiagnosisItem, btn: QPushButton) -> None:
        if self.apply_fix_callback is None:
            return
        msg = self.apply_fix_callback(item)
        self.applied_fixes.append(item.rule_id)
        btn.setText("✅ 보정 적용 완료")
        btn.setEnabled(False)
        btn.setStyleSheet("background-color: #16a34a; color: white; font-weight: bold; padding: 4px 10px; border-radius: 4px;")
        self.lbl_fix_status.setText(f"✅ {msg}")
        self.lbl_fix_status.setStyleSheet("color: #166534; font-weight: bold; font-size: 8.8pt;")
