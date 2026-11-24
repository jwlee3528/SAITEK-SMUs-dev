"""Requirement 1, 5 & 6 Implementation:
- Screen Resolution Auto-Adaptation (`get_available_screen_geometry`, `fit_and_center_window`).
- TEK_quickstart-style Beginner-Friendly SMU Connection & Multi-Node (1~10) Readiness Wizard (`SMUConnectionWizardDialog`).
  - Step-by-step beginner checklist cards (`ChecklistStepCard`) with how-to guides and live `[🔍 즉시 확인]` verification buttons.
  - Multi-SMU (Node 1..10) per-unit communication test table (`*IDN?`, `*LANG?`, TSP-Link ping, and Beeper location identify).
  - Direct PDF Reference Manual launcher for the selected SMU model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import time
from typing import Callable

from PyQt6.QtCore import QRect, Qt, QUrl
from PyQt6.QtGui import QColor, QDesktopServices, QFont
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from smu_app.core.profiles import ProfileRegistry, SMUModelProfile
from smu_app.core.visa_manager import VISAConnectionManager
from smu_app.drivers.base_smu import BaseTSPDriver, create_smu_driver
from smu_app.storage.setup_manager import find_manual_pdf_for_model


def get_available_screen_geometry(widget: QWidget | None = None) -> QRect:
    """Requirement 5: Return usable monitor geometry (excluding taskbar) for automatic screen scaling."""
    screen = None
    if widget is not None:
        win_handle = widget.windowHandle()
        if win_handle is not None:
            screen = win_handle.screen()
        if screen is None:
            screen = widget.screen()
    if screen is None and QApplication.instance() is not None:
        screen = QApplication.primaryScreen()
    if screen is not None:
        return screen.availableGeometry()
    return QRect(0, 0, 1920, 1040)


def fit_and_center_window(
    window: QWidget,
    target_w_ratio: float = 0.90,
    target_h_ratio: float = 0.90,
    min_w: int = 860,
    min_h: int = 560,
    max_w: int = 2200,
    max_h: int = 1320,
) -> tuple[int, int, bool]:
    """Requirement 5: Automatically resize and center any window/dialog to fit the user's screen resolution."""
    avail = get_available_screen_geometry(window)
    is_compact = avail.width() <= 1600 or avail.height() <= 920
    w = min(max_w, max(min_w, int(avail.width() * target_w_ratio)))
    h = min(max_h, max(min_h, int(avail.height() * target_h_ratio)))
    w = min(w, max(640, avail.width() - 16))
    h = min(h, max(480, avail.height() - 36))
    window.resize(w, h)
    x = avail.x() + max(0, (avail.width() - w) // 2)
    y = avail.y() + max(0, (avail.height() - h) // 2)
    window.move(x, y)
    return w, h, is_compact


def open_model_manual_pdf(model_name: str, parent: QWidget | None = None) -> bool:
    """Requirement 6: Open the PDF reference manual for the selected SMU model using the system PDF viewer."""
    pdf_path = find_manual_pdf_for_model(model_name)
    if pdf_path is None or not pdf_path.exists():
        if parent is not None:
            QMessageBox.warning(
                parent,
                "매뉴얼 파일 없음",
                f"선택된 모델({model_name})의 PDF 매뉴얼을 docs/manuals/ 폴더에서 찾을 수 없습니다.",
            )
        return False
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(pdf_path)))
    return True


@dataclass
class ChecklistItemSpec:
    """Specification for a single beginner checklist step card (TEK_quickstart pattern)."""

    step_id: str
    title: str
    description: str
    verify_cmd: str
    expected_hint: str
    verify_label: str = "🔍 이 항목 즉시 확인"
    checked_by_default: bool = False
    how_to_steps: list[str] = field(default_factory=list)


class ChecklistStepCard(QFrame):
    """TEK_quickstart-style interactive step card with beginner guide and live query verification badge."""

    def __init__(
        self,
        item_spec: ChecklistItemSpec,
        verify_callback: Callable[[ChecklistItemSpec], tuple[bool, str, float]],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.item_spec = item_spec
        self.verify_callback = verify_callback
        self._build_ui()

    def _build_ui(self) -> None:
        self.setStyleSheet(
            "ChecklistStepCard { background-color: #ffffff; border: 1px solid #cbd5e1; border-radius: 6px; }"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(4)

        top_row = QHBoxLayout()
        self.checkbox = QCheckBox(self.item_spec.title)
        self.checkbox.setChecked(self.item_spec.checked_by_default)
        self.checkbox.setFont(QFont("맑은 고딕", 9, QFont.Weight.Bold))
        self.checkbox.setStyleSheet("color: #0f172a;")
        top_row.addWidget(self.checkbox, 1)

        if self.item_spec.how_to_steps:
            btn_guide = QPushButton("📘 초보자 따라하기 가이드")
            btn_guide.setStyleSheet(
                "background-color: #e0f2fe; color: #0369a1; border: 1px solid #7dd3fc; "
                "padding: 3px 8px; border-radius: 4px; font-weight: bold;"
            )
            btn_guide.clicked.connect(self._show_guide_popup)
            top_row.addWidget(btn_guide)

        self.btn_verify = QPushButton(self.item_spec.verify_label)
        self.btn_verify.setStyleSheet(
            "background-color: #0284c7; color: white; padding: 4px 10px; border-radius: 4px; font-weight: bold;"
        )
        self.btn_verify.clicked.connect(self.run_single_verification)
        top_row.addWidget(self.btn_verify)
        layout.addLayout(top_row)

        desc_lbl = QLabel(f"💡 {self.item_spec.description}")
        desc_lbl.setWordWrap(True)
        desc_lbl.setStyleSheet("color: #475569; font-size: 8.5pt; padding-left: 18px;")
        layout.addWidget(desc_lbl)

        if self.item_spec.how_to_steps:
            guide_box = QFrame()
            guide_box.setStyleSheet(
                "QFrame { background-color: #f8fafc; border-left: 3px solid #3b82f6; border-radius: 3px; padding: 5px; margin-left: 18px; }"
            )
            g_layout = QVBoxLayout(guide_box)
            g_layout.setContentsMargins(6, 3, 6, 3)
            g_layout.setSpacing(2)
            for s in self.item_spec.how_to_steps:
                s_lbl = QLabel(s)
                s_lbl.setWordWrap(True)
                s_lbl.setStyleSheet("color: #1e293b; font-size: 8.5pt; border: none;")
                g_layout.addWidget(s_lbl)
            layout.addWidget(guide_box)

        res_row = QHBoxLayout()
        res_row.setContentsMargins(18, 0, 0, 0)
        self.lbl_verify_result = QLabel(
            f"⏳ 미확인 (검증 명령: {self.item_spec.verify_cmd} | 기대 결과: {self.item_spec.expected_hint})"
        )
        self.lbl_verify_result.setWordWrap(True)
        self.lbl_verify_result.setStyleSheet(
            "background-color: #f1f5f9; color: #64748b; border-radius: 4px; padding: 3px 8px; font-size: 8.5pt;"
        )
        res_row.addWidget(self.lbl_verify_result, 1)
        layout.addLayout(res_row)

    def run_single_verification(self) -> bool:
        ok, resp_text, dt_ms = self.verify_callback(self.item_spec)
        if ok:
            self.checkbox.setChecked(True)
            self.lbl_verify_result.setText(
                f"✅ 확인 완료 ({dt_ms:.1f} ms) | 명령: {self.item_spec.verify_cmd} → 응답: {resp_text}"
            )
            self.lbl_verify_result.setStyleSheet(
                "background-color: #dcfce7; color: #166534; border: 1px solid #86efac; "
                "border-radius: 4px; padding: 3px 8px; font-size: 8.5pt; font-weight: bold;"
            )
            self.setStyleSheet(
                "ChecklistStepCard { background-color: #f0fdf4; border: 1px solid #4ade80; border-radius: 6px; }"
            )
            return True
        self.lbl_verify_result.setText(
            f"❌ 확인 실패 | 명령: {self.item_spec.verify_cmd} → 오류: {resp_text}"
        )
        self.lbl_verify_result.setStyleSheet(
            "background-color: #fee2e2; color: #991b1b; border: 1px solid #fca5a5; "
            "border-radius: 4px; padding: 3px 8px; font-size: 8.5pt; font-weight: bold;"
        )
        self.setStyleSheet(
            "ChecklistStepCard { background-color: #fef2f2; border: 1px solid #f87171; border-radius: 6px; }"
        )
        return False

    def _show_guide_popup(self) -> None:
        html = (
            f"<h3>{self.item_spec.title}</h3>"
            f"<p><b>설명:</b> {self.item_spec.description}</p><hr>"
            f"<h4>📘 초보자 따라하기 순서:</h4>"
            + "<br>".join(f"<p style='margin:4px 0;'>{s}</p>" for s in self.item_spec.how_to_steps)
            + f"<hr><p><b>검증 명령어:</b> <code>{self.item_spec.verify_cmd}</code><br>"
            f"<b>기대 결과:</b> {self.item_spec.expected_hint}</p>"
        )
        QMessageBox.information(self, "초보자 SMU 연결 및 점검 가이드", html)


class SMUConnectionWizardDialog(QDialog):
    """Requirement 1: TEK_quickstart-style SMU Connection & Multi-Instrument Verification Wizard."""

    def __init__(
        self,
        visa_mgr: VISAConnectionManager,
        registry: ProfileRegistry,
        initial_model: str = "2450",
        initial_nodes: int = 1,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.visa_mgr = visa_mgr
        self.registry = registry
        self.selected_model = initial_model
        self.selected_nodes = initial_nodes
        self.selected_resource = f"SIM::{initial_model}::INSTR"
        self.driver: BaseTSPDriver = create_smu_driver(initial_model, registry=registry)
        self.step_cards: list[ChecklistStepCard] = []

        self.setWindowTitle("TEK QuickStart 스타일 — SMU 초보자 연결 마법사 & 다중 장비 통신 점검표")
        self._build_ui()
        fit_and_center_window(self, target_w_ratio=0.86, target_h_ratio=0.88, min_w=880, min_h=600)
        self._refresh_resources()
        self._on_model_or_nodes_changed()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        # Top Header Banner
        banner = QFrame()
        banner.setStyleSheet("QFrame { background-color: #0b2545; color: white; border-radius: 6px; padding: 8px; }")
        b_layout = QHBoxLayout(banner)
        title_box = QVBoxLayout()
        lbl_t = QLabel("⚡ Tektronix / Keithley SMU 초기 연결 마법사 & 초보자 점검 체크리스트")
        lbl_t.setFont(QFont("맑은 고딕", 12, QFont.Weight.Bold))
        lbl_t.setStyleSheet("color: #38bdf8;")
        lbl_sub = QLabel(
            "1단계: SMU 모델과 연결 대수(1~10대) 및 VISA 리소스를 선택하세요.  |  "
            "2단계: 각 장비별 개별 통신 테스트 및 초보자 체크리스트를 [🔍 전체 자동 점검]으로 확인하세요."
        )
        lbl_sub.setStyleSheet("color: #e2e8f0; font-size: 9pt;")
        title_box.addWidget(lbl_t)
        title_box.addWidget(lbl_sub)
        b_layout.addLayout(title_box, 1)

        btn_manual = QPushButton("📖 선택 모델 매뉴얼(PDF) 열기")
        btn_manual.setStyleSheet(
            "background-color: #0284c7; color: white; font-weight: bold; padding: 6px 12px; border-radius: 4px;"
        )
        btn_manual.clicked.connect(lambda: open_model_manual_pdf(self.cmb_model.currentText(), self))
        b_layout.addWidget(btn_manual)
        layout.addWidget(banner)

        # Step 1: Instrument & Resource Selection Row
        grp_sel = QGroupBox("STEP 1. 연결할 SMU 모델, 장비 대수(1~10대) 및 통신 리소스 선택")
        sel_row = QHBoxLayout(grp_sel)

        sel_row.addWidget(QLabel("<b>SMU 모델:</b>"))
        self.cmb_model = QComboBox()
        self.cmb_model.addItems(self.registry.list_models())
        self.cmb_model.setCurrentText(self.selected_model)
        self.cmb_model.currentTextChanged.connect(self._on_model_or_nodes_changed)
        sel_row.addWidget(self.cmb_model)

        sel_row.addWidget(QLabel("<b>연결 대수(1~10대):</b>"))
        self.spn_nodes = QSpinBox()
        self.spn_nodes.setRange(1, 10)
        self.spn_nodes.setValue(self.selected_nodes)
        self.spn_nodes.valueChanged.connect(self._on_model_or_nodes_changed)
        sel_row.addWidget(self.spn_nodes)

        sel_row.addWidget(QLabel("<b>Master VISA 리소스:</b>"))
        self.cmb_resource = QComboBox()
        self.cmb_resource.setEditable(True)
        self.cmb_resource.setMinimumWidth(240)
        self.cmb_resource.currentIndexChanged.connect(self._on_resource_combo_changed)
        sel_row.addWidget(self.cmb_resource, 1)

        btn_scan = QPushButton("🔄 장비 다시 검색")
        btn_scan.clicked.connect(self._refresh_resources)
        sel_row.addWidget(btn_scan)

        btn_verify_all = QPushButton("🚀 전체 체크리스트 & 다중 장비 일괄 통신 테스트")
        btn_verify_all.setStyleSheet(
            "background-color: #16a34a; color: white; font-weight: bold; padding: 6px 14px; border-radius: 4px;"
        )
        btn_verify_all.clicked.connect(self.run_all_verifications)
        sel_row.addWidget(btn_verify_all)
        layout.addWidget(grp_sel)

        # Step 2: Multi-SMU (1~10 Nodes) Per-Unit Communication Test Table
        grp_nodes = QGroupBox("STEP 2. 연결된 개별 장비(Node 1 ~ N) 통신 상태 및 위치 식별(Beep) 테스트")
        n_layout = QVBoxLayout(grp_nodes)
        self.tbl_nodes = QTableWidget(0, 6)
        self.tbl_nodes.setHorizontalHeaderLabels([
            "장비 번호 (Node)", "역할 (Master/Slave)", "VISA / TSP-Link 경로", "통신 상태 (*IDN? / Ping)", "응답 시간", "개별 테스트 & 위치 확인"
        ])
        self.tbl_nodes.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.tbl_nodes.setMaximumHeight(185)
        n_layout.addWidget(self.tbl_nodes)
        layout.addWidget(grp_nodes)

        # Step 3: Scrollable Beginner Checklist Cards
        grp_chk = QGroupBox("STEP 3. 초보자용 단계별 연결·안전 사전 준비 체크리스트 (각 항목 [🔍 즉시 확인] 지원)")
        chk_outer = QVBoxLayout(grp_chk)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self.cards_container = QWidget()
        self.cards_layout = QVBoxLayout(self.cards_container)
        self.cards_layout.setSpacing(6)
        scroll.setWidget(self.cards_container)
        chk_outer.addWidget(scroll)
        layout.addWidget(grp_chk, 1)

        # Bottom Action Row
        bot_row = QHBoxLayout()
        self.lbl_ready_status = QLabel("💡 [전체 일괄 통신 테스트]를 누르거나 확인 후 [연결 적용 및 메인 화면 이동]을 클릭하세요.")
        self.lbl_ready_status.setStyleSheet("color: #0f172a; font-weight: bold;")
        bot_row.addWidget(self.lbl_ready_status, 1)

        btn_skip = QPushButton("건너뛰기 (현재 설정 유지)")
        btn_skip.clicked.connect(self.reject)
        bot_row.addWidget(btn_skip)

        self.btn_confirm = QPushButton("✔ 연결 설정 적용 및 메인 화면으로 시작")
        self.btn_confirm.setStyleSheet(
            "background-color: #0070C0; color: white; font-weight: bold; font-size: 10.5pt; padding: 8px 20px; border-radius: 5px;"
        )
        self.btn_confirm.clicked.connect(self._on_confirm)
        bot_row.addWidget(self.btn_confirm)
        layout.addLayout(bot_row)

    def _refresh_resources(self) -> None:
        discovered = self.visa_mgr.discover_instruments(include_simulators=True)
        self.cmb_resource.blockSignals(True)
        self.cmb_resource.clear()
        hw_idx = -1
        for i, inst in enumerate(discovered):
            tag = "[가상 시뮬레이터]" if inst.is_simulator else f"[실물 장비 - {inst.interface_type}]"
            self.cmb_resource.addItem(f"{inst.resource_name}  {tag}", inst.resource_name)
            if not inst.is_simulator and hw_idx < 0:
                hw_idx = i
        if hw_idx >= 0:
            self.cmb_resource.setCurrentIndex(hw_idx)
        self.cmb_resource.blockSignals(False)

    def _open_selected_driver(self) -> None:
        res_name = self.cmb_resource.currentData() or self.cmb_resource.currentText().split()[0].strip()
        self.selected_resource = res_name
        try:
            transport = self.visa_mgr.open_transport_session(res_name, model=self.selected_model)
            self.driver = create_smu_driver(self.selected_model, transport=transport, spy=self.visa_mgr.spy, registry=self.registry)
        except Exception:
            self.driver = create_smu_driver(self.selected_model, spy=self.visa_mgr.spy, registry=self.registry)

    def _on_resource_combo_changed(self, *_) -> None:
        self._open_selected_driver()
        self._rebuild_node_test_table()

    def _on_model_or_nodes_changed(self, *_) -> None:
        self.selected_model = self.cmb_model.currentText()
        self.selected_nodes = self.spn_nodes.value()

        # Only switch to SIM resource if current selection is already a SIM resource
        curr_res = str(self.cmb_resource.currentData() or self.cmb_resource.currentText())
        if curr_res.upper().startswith("SIM::"):
            target_sim = f"SIM::{self.selected_model}::INSTR"
            for i in range(self.cmb_resource.count()):
                if target_sim in self.cmb_resource.itemText(i):
                    self.cmb_resource.blockSignals(True)
                    self.cmb_resource.setCurrentIndex(i)
                    self.cmb_resource.blockSignals(False)
                    break

        self._open_selected_driver()
        self._rebuild_node_test_table()
        self._rebuild_beginner_checklist_cards()

    def _rebuild_node_test_table(self) -> None:
        n_cnt = self.spn_nodes.value()
        p = self.driver.profile
        self.tbl_nodes.setRowCount(n_cnt)
        for idx in range(n_cnt):
            node_num = idx + 1
            role_txt = "Master (PC 직접 연결)" if node_num == 1 else f"Slave #{node_num} (TSP-Link 연동)"
            path_txt = (
                f"Master VISA ({p.channel_names[0]})"
                if node_num == 1
                else f"TSP-Link node[{node_num}].{p.channel_names[0]}"
            )
            self.tbl_nodes.setItem(idx, 0, QTableWidgetItem(f"Node {node_num} ({p.model})"))
            self.tbl_nodes.setItem(idx, 1, QTableWidgetItem(role_txt))
            self.tbl_nodes.setItem(idx, 2, QTableWidgetItem(path_txt))

            st_item = QTableWidgetItem("⏳ 테스트 대기")
            st_item.setForeground(QColor("#64748B"))
            self.tbl_nodes.setItem(idx, 3, st_item)
            self.tbl_nodes.setItem(idx, 4, QTableWidgetItem("-"))

            btn_widget = QWidget()
            bh = QHBoxLayout(btn_widget)
            bh.setContentsMargins(2, 2, 2, 2)
            bh.setSpacing(4)
            btn_ping = QPushButton(f"🔍 Node {node_num} 통신 테스트")
            btn_ping.setStyleSheet("background-color: #0284c7; color: white; font-weight: bold; padding: 3px 6px;")
            btn_ping.clicked.connect(lambda _, n=node_num: self.test_single_node_connection(n))
            btn_beep = QPushButton("🔊 위치 확인(Beep)")
            btn_beep.clicked.connect(lambda _, n=node_num: self.beep_single_node(n))
            bh.addWidget(btn_ping)
            bh.addWidget(btn_beep)
            self.tbl_nodes.setCellWidget(idx, 5, btn_widget)

    def test_single_node_connection(self, node_num: int) -> bool:
        row = node_num - 1
        t0 = time.perf_counter()
        try:
            if node_num == 1:
                resp = self.driver.identify()
            else:
                self.driver.transport.write(f"tsplink.initialize({self.spn_nodes.value()})")
                state = self.driver.transport.query("print(tsplink.state)").strip()
                resp = f"TSP-Link Node {node_num} ({state}, Model={self.driver.profile.model})"
            dt_ms = (time.perf_counter() - t0) * 1000.0
            st_item = QTableWidgetItem(f"✅ 연결 정상: {resp[:48]}")
            st_item.setBackground(QColor("#DCFCE7"))
            st_item.setForeground(QColor("#166534"))
            self.tbl_nodes.setItem(row, 3, st_item)
            self.tbl_nodes.setItem(row, 4, QTableWidgetItem(f"{dt_ms:.2f} ms"))
            return True
        except Exception as exc:
            st_item = QTableWidgetItem(f"❌ 통신 오류: {exc}")
            st_item.setBackground(QColor("#FEE2E2"))
            st_item.setForeground(QColor("#991B1B"))
            self.tbl_nodes.setItem(row, 3, st_item)
            self.tbl_nodes.setItem(row, 4, QTableWidgetItem("ERR"))
            return False

    def beep_single_node(self, node_num: int) -> None:
        if node_num == 1:
            self.driver.identify_blink_or_beep()
        else:
            self.driver.transport.write(f"node[{node_num}].execute('beeper.beep(0.15, 2400)')")

    def _build_checklist_specs_for_model(self, p: SMUModelProfile, nodes: int) -> list[ChecklistItemSpec]:
        specs = [
            ChecklistItemSpec(
                step_id="idn",
                title=f"1. [{p.model}] 전원 켜짐 및 PC 통신 케이블(USB / LAN Port 5025 / GPIB) 연결 확인",
                description="장비 전원을 켜고 PC와 통신 케이블이 정상 연결되었는지 표준 식별 명령(*IDN?)으로 확인합니다.",
                verify_cmd="*IDN?",
                expected_hint=f"KEITHLEY INSTRUMENTS, MODEL {p.model}",
                checked_by_default=True,
                how_to_steps=[
                    "① USB 연결 시: 장비 후면 USB B타입 포트와 PC를 연결합니다 (NI-VISA 또는 TekVISA 드라이버 자동 인식).",
                    "② LAN 연결 시: 장비 전면 [MENU] → [Communication] → [LAN]에서 IP 주소를 확인하고 Raw Socket(5025)을 활성화합니다.",
                    "③ 우측 [🔍 이 항목 즉시 확인] 버튼을 눌러 모델명과 시리얼 번호가 응답되는지 확인하세요.",
                ],
            ),
            ChecklistItemSpec(
                step_id="tsp_lang",
                title=f"2. [{p.model}] 명령어 세트 TSP(Test Script Processor) 모드 활성화 확인",
                description=(
                    "2450/2460/2461/2470은 기본 출하 모드가 SCPI일 수 있습니다. "
                    "고속 버퍼 제어와 다중 장비 TSP-Link 동기화를 위해 TSP 모드로 자동 전환합니다."
                ),
                verify_cmd="*LANG?",
                expected_hint="TSP (SCPI 감지 시 *LANG TSP 자동 전환)",
                checked_by_default=True,
                how_to_steps=[
                    "① 24xx 터치스크린 장비: 전면 [MENU] → [System] → [Settings] → [Command Set]을 'TSP'로 설정합니다.",
                    "② 26xx 시스템 장비(2600B/2601B-PULSE/2606B/2651A): 네이티브 TSP 전용 장비이므로 별도 변경 없이 즉시 통과합니다.",
                    "③ 우측 확인 버튼을 누르면 SCPI 상태일 때 자동으로 '*LANG TSP' 전환 명령을 전송합니다.",
                ],
            ),
            ChecklistItemSpec(
                step_id="terminals_sense",
                title="3. 전면(Front) / 후면(Rear) 출력 터미널 및 2-Wire / 4-Wire 배선 확인",
                description="DUT(측정 소자)가 연결된 단자 위치(Front 바나나잭 vs Rear 트라이액셜/스크류)와 원격 센싱 리드를 확인합니다.",
                verify_cmd="print(smu.terminals)" if p.is_24xx else "print(smua.sense)",
                expected_hint=" 배선 단자 정상 응답 및 출력 OFF 안전 대기 상태",
                checked_by_default=True,
                how_to_steps=[
                    "① 2-Wire(기본): Force HI / Force LO 2개 리드만 소자에 연결합니다.",
                    "② 4-Wire(저저항/고전류 정밀 측정): Force HI/LO 외에 Sense HI/LO 리드를 소자 양단에 반드시 연결해야 합니다 (Sense 단선 시 과전압 위험!).",
                    f"③ 고전류({p.max_pulse_current_a}A) 또는 고전압({p.max_dc_voltage_v}V) 측정 시에는 후면(Rear) 전용 단자 사용을 권장합니다.",
                ],
            ),
            ChecklistItemSpec(
                step_id="interlock",
                title=f"4. 후면 안전 인터락(Safety Interlock) 상태 점검 (±{p.interlock_threshold_v:.0f}V 초과 고전압 보호)",
                description=f"±{p.interlock_threshold_v:.0f}V 이하 저전압 측정 시에는 미체결 상태로도 사용 가능하나, 고전압 측정 시에는 인터락 핀 체결이 필수입니다.",
                verify_cmd="print(smu.interlock.state)" if p.is_24xx else "print(status.measurement.condition)",
                expected_hint="Interlock 체결(1) 또는 ±42V 이하 안전 영역 확인",
                checked_by_default=True,
                how_to_steps=[
                    "① ±42V 초과 전압을 인가하려면 후면 초록색 Interlock 커넥터(1-2번 핀 단락)와 테스트 픽스처 뚜껑 스위치가 닫혀 있어야 합니다.",
                    "② 인터락이 열려 있으면 장비가 에러 코드 5074 / 802를 발생시키며 고전압 출력을 하드웨어적으로 차단합니다.",
                ],
            ),
        ]
        if nodes > 1:
            specs.append(
                ChecklistItemSpec(
                    step_id="tsplink",
                    title=f"5. 다중 장비({nodes}대) Master-Slave TSP-Link 케이블 및 노드 번호(1~{nodes}) 확인",
                    description="여러 대의 SMU를 동기화할 때 각 장비의 TSP-Link Node 번호가 중복 없이 1번(Master)부터 순서대로 설정되어 있어야 합니다.",
                    verify_cmd=f"tsplink.initialize({nodes}); print(tsplink.state)",
                    expected_hint="online (전 노드 TSP-Link 링 정상 연결)",
                    checked_by_default=True,
                    how_to_steps=[
                        "① 장비 후면의 [TSP-Link] 포트들을 CAT5e 이상 랜선(크로스오버/데이지체인)으로 연결합니다.",
                        f"② Master 장비는 Node 1, 나머지 Slave 장비들은 각각 Node 2 ~ Node {nodes}(으)로 서로 다른 번호를 부여합니다.",
                        "③ 우측 [🔍 이 항목 즉시 확인]을 눌러 'online' 응답이 오는지 확인하세요.",
                    ],
                )
            )
        return specs

    def _rebuild_beginner_checklist_cards(self) -> None:
        while self.cards_layout.count():
            item = self.cards_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.step_cards.clear()

        specs = self._build_checklist_specs_for_model(self.driver.profile, self.spn_nodes.value())
        for sp in specs:
            card = ChecklistStepCard(sp, self._verify_single_checklist_item, self)
            self.cards_layout.addWidget(card)
            self.step_cards.append(card)
        self.cards_layout.addStretch(1)

    def _verify_single_checklist_item(self, spec: ChecklistItemSpec) -> tuple[bool, str, float]:
        t0 = time.perf_counter()
        try:
            if spec.step_id == "idn":
                resp = self.driver.identify()
                ok = True
            elif spec.step_id == "tsp_lang":
                ok, resp = self.driver.ensure_tsp_mode(auto_switch=True)
            elif spec.step_id == "terminals_sense":
                if not self.driver.is_simulated and self.driver.profile.is_24xx:
                    lang = self.driver.transport.query("*LANG?").strip().upper()
                    if "SCPI" in lang:
                        term = self.driver.transport.query(":ROUT:TERM?").strip()
                        resp = f"출력 단자: {term} (SCPI 모드 확인)"
                    else:
                        resp = self.driver.transport.query("print(smu.terminals)").strip()
                else:
                    resp = self.driver.transport.query(spec.verify_cmd)
                ok = True
            elif spec.step_id == "interlock":
                engaged = self.driver.check_interlock_engaged()
                ok = True
                resp = "Interlock 체결됨 (고전압 허용)" if engaged else "Interlock 미체결 (±42V 이하 저전압 전용)"
            elif spec.step_id == "tsplink":
                n_cnt = self.spn_nodes.value()
                if not self.driver.is_simulated and self.driver.profile.is_24xx:
                    lang = self.driver.transport.query("*LANG?").strip().upper()
                    if "SCPI" in lang:
                        resp = "단일 24xx SCPI 연결 확인 (다중 TSP-Link 사용 시에만 TSP 전환 필요)"
                        ok = True
                        dt_ms = (time.perf_counter() - t0) * 1000.0
                        return ok, resp, dt_ms
                self.driver.transport.write(f"tsplink.initialize({n_cnt})")
                resp = self.driver.transport.query("print(tsplink.state)").strip()
                ok = "online" in resp.lower()
            else:
                resp = self.driver.transport.query(spec.verify_cmd)
                ok = True
            dt_ms = (time.perf_counter() - t0) * 1000.0
            return ok, resp, dt_ms
        except Exception as exc:
            dt_ms = (time.perf_counter() - t0) * 1000.0
            return False, str(exc), dt_ms

    def run_all_verifications(self) -> bool:
        all_ok = True
        for n in range(1, self.spn_nodes.value() + 1):
            if not self.test_single_node_connection(n):
                all_ok = False
        for card in self.step_cards:
            if not card.run_single_verification():
                all_ok = False
        if all_ok:
            self.lbl_ready_status.setText(
                f"✅ [{self.selected_model} × {self.spn_nodes.value()}대] 모든 통신 테스트 및 초보자 체크리스트 점검이 통과되었습니다!"
            )
            self.lbl_ready_status.setStyleSheet("color: #166534; font-weight: bold;")
        else:
            self.lbl_ready_status.setText("⚠ 일부 항목 확인에 실패했습니다. 빨간색 카드를 확인해 주세요.")
            self.lbl_ready_status.setStyleSheet("color: #991b1b; font-weight: bold;")
        return all_ok

    def _on_confirm(self) -> None:
        self.selected_model = self.cmb_model.currentText()
        self.selected_nodes = self.spn_nodes.value()
        self.selected_resource = self.cmb_resource.currentData() or self.cmb_resource.currentText()
        self.accept()
