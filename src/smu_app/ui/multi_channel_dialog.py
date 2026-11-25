"""Standalone Popup Dialog for Multi-SMU (1~10 Nodes), Mixed-Model Connection Mode (A: TSP-Link vs B: Multi-VISA USB/LAN),
and Per-Channel Independent Waveform/Source Configuration.

Features:
- User-selectable Connection Mode A (TSP-Link HW Sync < 1 us, same-family mixed models like 2450+2461) vs
  Mode B (USB/LAN Individual Multi-Session SW Sync 1~20 ms, all 8 models freely mixable).
- Clear comparison card showing supported model combinations and synchronization precision.
- Live compatibility validation banner when mixing models across 24xx and 26xx families.
- Per-node SMU Model selector (all 8 models), per-node VISA resource selector (Mode B), and per-channel
  independent waveform/source overrides.
"""

from __future__ import annotations

from dataclasses import replace
import time
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from smu_app.core.models import (
    LogicalChannel,
    MultiUnitSyncMode,
    SourceFunction,
    SweepType,
)
from smu_app.core.profiles import ProfileRegistry, SMUModelProfile
from smu_app.core.visa_manager import VISAConnectionManager
from smu_app.drivers.base_smu import create_smu_driver
from smu_app.sync.cluster_manager import ChannelRole, TSPLinkClusterManager
from smu_app.ui.connection_wizard import fit_and_center_window


class MultiChannelConfigDialog(QDialog):
    """Dedicated popup window for 1~10 SMU Node-Channel mapping, Mode A/B selection, and mixed-model configuration."""

    def __init__(
        self,
        profile: SMUModelProfile,
        logical_channels: list[LogicalChannel],
        variant_code: str = "",
        sync_mode: MultiUnitSyncMode = MultiUnitSyncMode.TSPLINK_HW,
        registry: ProfileRegistry | None = None,
        available_visa_resources: list[str] | None = None,
        visa_mgr: VISAConnectionManager | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.profile = profile
        self.registry = registry or ProfileRegistry()
        self.visa_mgr = visa_mgr or VISAConnectionManager()
        self.all_models = self.registry.list_models()
        self.variant_code = variant_code or profile.model
        self.result_sync_mode: MultiUnitSyncMode = sync_mode
        self.available_visa_resources: list[str] = list(available_visa_resources or [])
        self._row_conn_status: dict[int, tuple[bool | None, str]] = {}
        self.result_channels: list[LogicalChannel] = []
        for c in logical_channels:
            cp = replace(c)
            if not cp.model_name:
                cp.model_name = profile.model
            self.result_channels.append(cp)

        self.setWindowTitle(f"🔗 다중 장비·혼합 연결 및 채널별 설정 (1~10대) — Master: {profile.display_name}")
        self._build_ui()
        fit_and_center_window(self, target_w_ratio=0.88, target_h_ratio=0.78, min_w=1080, min_h=580)
        self._populate_table()
        self._refresh_compatibility_banner()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        # -----------------------------------------------------------------
        # 1. CONNECTION MODE A vs B SELECTOR WITH SUPPORT & PRECISION GUIDE
        # -----------------------------------------------------------------
        mode_frame = QFrame()
        mode_frame.setStyleSheet(
            "QFrame { background-color: #f8fafc; border: 1.5px solid #93c5fd; border-radius: 6px; }"
        )
        mode_v = QVBoxLayout(mode_frame)
        mode_v.setContentsMargins(10, 8, 10, 8)
        mode_v.setSpacing(6)

        lbl_mtitle = QLabel("⚡ 다중 장비 연결 방식 선택 (혼합 모델 지원 조합 & 동기화 정밀도 안내)")
        lbl_mtitle.setStyleSheet("color: #0b2545; font-weight: bold; font-size: 9.8pt; border: none;")
        mode_v.addWidget(lbl_mtitle)

        cards_row = QHBoxLayout()
        cards_row.setSpacing(10)

        self.mode_btn_group = QButtonGroup(self)

        # Card A: TSP-Link HW Sync
        card_a = QFrame()
        card_a.setStyleSheet(
            "QFrame { background-color: #ffffff; border: 1px solid #cbd5e1; border-radius: 5px; }"
        )
        ca_v = QVBoxLayout(card_a)
        ca_v.setContentsMargins(9, 7, 9, 7)
        ca_v.setSpacing(3)
        self.rad_mode_a = QRadioButton("방식 A: TSP-Link 케이블 연결 (하드웨어 정밀 동기화)")
        self.rad_mode_a.setStyleSheet("font-weight: bold; color: #1d4ed8; font-size: 9.2pt;")
        self.rad_mode_a.setChecked(self.result_sync_mode == MultiUnitSyncMode.TSPLINK_HW)
        self.mode_btn_group.addButton(self.rad_mode_a)
        ca_v.addWidget(self.rad_mode_a)
        lbl_a_desc = QLabel(
            "• <b>동기화 정밀도:</b> <span style='color:#047857;'><b>&lt; 1 µs (하드웨어 3-Sync-Line 동기화)</b></span><br>"
            "• <b>지원 혼합 조합:</b> <b>같은 계열끼리 혼합 가능</b> (예: 2450+2461, 2450+2470 / 2600B+2651A)<br>"
            "• <b>연결 방법:</b> Master 1대만 PC에 연결하고 나머지 장비는 후면 TSP-Link 케이블로 데이지체인 연결"
        )
        lbl_a_desc.setWordWrap(True)
        lbl_a_desc.setStyleSheet("color: #334155; font-size: 8.5pt; border: none;")
        ca_v.addWidget(lbl_a_desc)
        cards_row.addWidget(card_a, 1)

        # Card B: Multi-VISA USB/LAN SW Sync
        card_b = QFrame()
        card_b.setStyleSheet(
            "QFrame { background-color: #ffffff; border: 1px solid #cbd5e1; border-radius: 5px; }"
        )
        cb_v = QVBoxLayout(card_b)
        cb_v.setContentsMargins(9, 7, 9, 7)
        cb_v.setSpacing(3)
        self.rad_mode_b = QRadioButton("방식 B: USB / LAN 개별 연결 (SW 멀티세션 동기화)")
        self.rad_mode_b.setStyleSheet("font-weight: bold; color: #0f766e; font-size: 9.2pt;")
        self.rad_mode_b.setChecked(self.result_sync_mode == MultiUnitSyncMode.MULTI_VISA_SW)
        self.mode_btn_group.addButton(self.rad_mode_b)
        cb_v.addWidget(self.rad_mode_b)
        lbl_b_desc = QLabel(
            "• <b>동기화 정밀도:</b> <span style='color:#b45309;'><b>약 1 ~ 20 ms (PC 멀티스레드 SW 동기화)</b></span><br>"
            "• <b>지원 혼합 조합:</b> <span style='color:#0f766e;'><b>모든 8개 모델 자유 혼합 가능</b></span> (예: 2450+2461, 2450+2600B, 2470+2651A)<br>"
            "• <b>연결 방법:</b> TSP-Link 케이블 없이 각 장비를 PC에 USB/LAN으로 개별 연결 후 아래 표에서 VISA 선택"
        )
        lbl_b_desc.setWordWrap(True)
        lbl_b_desc.setStyleSheet("color: #334155; font-size: 8.5pt; border: none;")
        cb_v.addWidget(lbl_b_desc)
        cards_row.addWidget(card_b, 1)

        mode_v.addLayout(cards_row)
        layout.addWidget(mode_frame)

        self.rad_mode_a.toggled.connect(self._on_sync_mode_toggled)
        self.rad_mode_b.toggled.connect(self._on_sync_mode_toggled)

        # Live compatibility & sync status banner
        compat_row = QHBoxLayout()
        self.lbl_compat_banner = QLabel()
        self.lbl_compat_banner.setWordWrap(True)
        self.lbl_compat_banner.setStyleSheet(
            "background-color: #ecfdf5; color: #065f46; border: 1px solid #6ee7b7; border-radius: 5px; padding: 6px 10px; font-weight: bold;"
        )
        compat_row.addWidget(self.lbl_compat_banner, 1)

        self.btn_switch_to_b = QPushButton("➡ 방식 B (USB/LAN 개별)로 전환")
        self.btn_switch_to_b.setStyleSheet(
            "background-color: #0f766e; color: white; font-weight: bold; padding: 6px 10px; border-radius: 4px;"
        )
        self.btn_switch_to_b.clicked.connect(lambda: self.rad_mode_b.setChecked(True))
        self.btn_switch_to_b.setVisible(False)
        compat_row.addWidget(self.btn_switch_to_b)
        layout.addLayout(compat_row)

        # -----------------------------------------------------------------
        # 2. QUICK NODE MANAGEMENT & PRESET ROW
        # -----------------------------------------------------------------
        preset_row = QHBoxLayout()
        btn_add_node = QPushButton("+ 장비(Node) 추가")
        btn_add_node.setStyleSheet("background-color: #e0f2fe; color: #0369a1; font-weight: bold;")
        btn_add_node.clicked.connect(self._add_node_row)

        btn_del_node = QPushButton("- 마지막 Node 삭제")
        btn_del_node.setStyleSheet("background-color: #fee2e2; color: #b91c1c; font-weight: bold;")
        btn_del_node.clicked.connect(self._remove_last_node)

        btn_preset_mixed = QPushButton("🔀 2450 + 2461 혼합 프리셋")
        btn_preset_mixed.setStyleSheet("background-color: #eef2ff; color: #3730a3; font-weight: bold;")
        btn_preset_mixed.clicked.connect(self._preset_2450_plus_2461)

        btn_all_par = QPushButton("모두 병렬 DUT (PARALLEL_DUT)")
        btn_all_par.clicked.connect(self._preset_all_parallel)

        btn_fet = QPushButton("3단자 FET 프리셋 (DRAIN/GATE/SOURCE)")
        btn_fet.clicked.connect(self._preset_fet_roles)

        btn_rescan = QPushButton("🔄 USB/LAN 재검색")
        btn_rescan.setStyleSheet("background-color: #f1f5f9; color: #0f172a; font-weight: bold;")
        btn_rescan.clicked.connect(self._rescan_visa_resources)

        btn_test_all = QPushButton("🔍 전체 연결 확인 (*IDN?)")
        btn_test_all.setStyleSheet("background-color: #16a34a; color: white; font-weight: bold;")
        btn_test_all.clicked.connect(self.test_all_connections)

        for b in (btn_add_node, btn_del_node, btn_preset_mixed, btn_all_par, btn_fet, btn_rescan, btn_test_all):
            preset_row.addWidget(b)
        preset_row.addStretch(1)
        layout.addLayout(preset_row)

        # -----------------------------------------------------------------
        # 3. 12-COLUMN MULTI-SMU / MIXED-MODEL CONFIGURATION TABLE
        # -----------------------------------------------------------------
        self.tbl_channels = QTableWidget(0, 12)
        self.tbl_channels.setAlternatingRowColors(True)
        self.tbl_channels.verticalHeader().setVisible(False)
        self.tbl_channels.setHorizontalHeaderLabels([
            "논리 채널",
            "장비 모델 (혼합 가능)",
            "개별 VISA 리소스 (방식 B)",
            "연결 확인 (*IDN?)",
            "TSP 경로",
            "활성화",
            "역할 (Role)",
            "개별 설정",
            "파형 모드",
            "소스 기능",
            "시작, 종료",
            "개별 Limit",
        ])
        hdr = self.tbl_channels.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        hdr.setStretchLastSection(True)
        col_widths = [82, 108, 195, 138, 88, 52, 110, 68, 86, 80, 88, 70]
        for idx_w, w in enumerate(col_widths):
            self.tbl_channels.setColumnWidth(idx_w, w)
        layout.addWidget(self.tbl_channels, 1)

        # -----------------------------------------------------------------
        # 4. BOTTOM APPLY / CANCEL BUTTONS
        # -----------------------------------------------------------------
        bot = QHBoxLayout()
        bot.addStretch(1)
        btn_cancel = QPushButton("취소")
        btn_cancel.clicked.connect(self.reject)
        bot.addWidget(btn_cancel)

        btn_apply = QPushButton("✔ 다중 장비·혼합 연결 설정 메인 화면에 적용")
        btn_apply.setStyleSheet(
            "background-color: #2563eb; color: white; font-weight: bold; padding: 8px 20px; border-radius: 5px;"
        )
        btn_apply.clicked.connect(self._on_apply)
        bot.addWidget(btn_apply)
        layout.addLayout(bot)

    def _on_sync_mode_toggled(self, *_) -> None:
        self.result_sync_mode = (
            MultiUnitSyncMode.TSPLINK_HW if self.rad_mode_a.isChecked() else MultiUnitSyncMode.MULTI_VISA_SW
        )
        self._populate_table()
        self._refresh_compatibility_banner()

    def _populate_table(self) -> None:
        self.tbl_channels.setRowCount(len(self.result_channels))
        is_mode_b = self.result_sync_mode == MultiUnitSyncMode.MULTI_VISA_SW

        for r, lch in enumerate(self.result_channels):
            self.tbl_channels.setItem(r, 0, QTableWidgetItem(lch.label))

            cmb_model = QComboBox()
            cmb_model.addItems(self.all_models)
            cmb_model.setCurrentText(lch.model_name or self.profile.model)
            cmb_model.currentTextChanged.connect(lambda val, idx=r: self._on_row_model_changed(idx, val))
            self.tbl_channels.setCellWidget(r, 1, cmb_model)

            cmb_visa = QComboBox()
            cmb_visa.setEditable(True)
            if is_mode_b:
                m_name = lch.model_name or self.profile.model
                candidates = list(self.available_visa_resources)
                sim_default = f"SIM::{m_name}::INSTR"
                if sim_default not in candidates:
                    candidates.append(sim_default)
                cmb_visa.addItems(candidates)
                if lch.visa_resource:
                    cmb_visa.setCurrentText(lch.visa_resource)
                else:
                    cmb_visa.setCurrentText(sim_default)
                    lch.visa_resource = sim_default
                cmb_visa.currentTextChanged.connect(lambda val, idx=r: self._set_attr(idx, "visa_resource", val.strip()))
                cmb_visa.setEnabled(True)
            else:
                cmb_visa.addItem("(Master TSP-Link 공유)")
                cmb_visa.setEnabled(False)
            self.tbl_channels.setCellWidget(r, 2, cmb_visa)

            # Column 3: Per-row *IDN? connection test button + status badge
            conn_cell = QWidget()
            ch_box = QHBoxLayout(conn_cell)
            ch_box.setContentsMargins(2, 1, 2, 1)
            ch_box.setSpacing(3)
            st_ok, st_txt = self._row_conn_status.get(r, (None, "미확인"))
            btn_chk = QPushButton("🔍" if st_ok is None else ("✅" if st_ok else "❌"))
            btn_chk.setFixedWidth(28)
            btn_chk.setToolTip("이 장비의 VISA 연결 상태(*IDN?)를 즉시 확인합니다.")
            btn_chk.clicked.connect(lambda _, idx=r: self.test_row_connection(idx))
            lbl_st = QLabel(st_txt)
            lbl_st.setStyleSheet(
                "color: #166534; font-weight: bold; font-size: 8pt;"
                if st_ok is True
                else ("color: #b91c1c; font-weight: bold; font-size: 8pt;" if st_ok is False else "color: #64748b; font-size: 8pt;")
            )
            ch_box.addWidget(btn_chk)
            ch_box.addWidget(lbl_st, 1)
            self.tbl_channels.setCellWidget(r, 3, conn_cell)

            self.tbl_channels.setItem(r, 4, QTableWidgetItem(lch.tsp_prefix))

            chk_en = QCheckBox("ON")
            chk_en.setChecked(lch.enabled)
            chk_en.toggled.connect(lambda checked, idx=r: self._on_row_enable_toggled(idx, checked))
            self.tbl_channels.setCellWidget(r, 5, chk_en)

            cmb_role = QComboBox()
            for role in ChannelRole:
                cmb_role.addItem(role.value)
            cmb_role.setCurrentText(lch.role)
            cmb_role.currentTextChanged.connect(lambda val, idx=r: self._set_attr(idx, "role", val))
            self.tbl_channels.setCellWidget(r, 6, cmb_role)

            chk_ind = QCheckBox("개별")
            chk_ind.setChecked(lch.independent_override)
            chk_ind.toggled.connect(lambda checked, idx=r: self._set_attr(idx, "independent_override", checked))
            self.tbl_channels.setCellWidget(r, 7, chk_ind)

            cmb_swp = QComboBox()
            for st in SweepType:
                cmb_swp.addItem(st.value)
            cmb_swp.setCurrentText(lch.override_sweep_type)
            cmb_swp.currentTextChanged.connect(lambda val, idx=r: self._set_attr(idx, "override_sweep_type", val))
            self.tbl_channels.setCellWidget(r, 8, cmb_swp)

            cmb_src = QComboBox()
            cmb_src.addItems([SourceFunction.VOLTAGE.value, SourceFunction.CURRENT.value])
            cmb_src.setCurrentText(lch.override_source_func)
            cmb_src.currentTextChanged.connect(lambda val, idx=r: self._set_attr(idx, "override_source_func", val))
            self.tbl_channels.setCellWidget(r, 9, cmb_src)

            self.tbl_channels.setItem(
                r, 10, QTableWidgetItem(f"{lch.override_start_level:g}, {lch.override_stop_level:g}")
            )
            self.tbl_channels.setItem(r, 11, QTableWidgetItem(f"{lch.override_compliance:g}"))

    def _set_attr(self, idx: int, attr: str, val) -> None:
        if 0 <= idx < len(self.result_channels):
            setattr(self.result_channels[idx], attr, val)

    def _on_row_enable_toggled(self, idx: int, checked: bool) -> None:
        self._set_attr(idx, "enabled", checked)
        self._refresh_compatibility_banner()

    def _on_row_model_changed(self, idx: int, model_name: str) -> None:
        if not (0 <= idx < len(self.result_channels)):
            return
        lch = self.result_channels[idx]
        lch.model_name = model_name
        prof = self.registry.get(model_name)
        lch.channel_name = prof.channel_names[0]
        if self.result_sync_mode == MultiUnitSyncMode.MULTI_VISA_SW and (
            not lch.visa_resource or lch.visa_resource.startswith("SIM::")
        ):
            lch.visa_resource = f"SIM::{model_name}::INSTR"
        self.tbl_channels.setItem(idx, 0, QTableWidgetItem(lch.label))
        self.tbl_channels.setItem(idx, 4, QTableWidgetItem(lch.tsp_prefix))
        self._row_conn_status.pop(idx, None)
        self._refresh_compatibility_banner()

    def _rescan_visa_resources(self) -> None:
        discovered = self.visa_mgr.discover_instruments(include_simulators=True)
        self.available_visa_resources = [d.resource_name for d in discovered]
        self._populate_table()

    def test_row_connection(self, row: int) -> tuple[bool, str]:
        """Query *IDN? on the selected row's VISA resource (Mode B) or TSP-Link node (Mode A) and update status badge."""
        if not (0 <= row < len(self.result_channels)):
            return False, "잘못된 행"
        lch = self.result_channels[row]
        m_name = lch.model_name or self.profile.model
        res_addr = (
            lch.visa_resource
            if (self.result_sync_mode == MultiUnitSyncMode.MULTI_VISA_SW and lch.visa_resource)
            else (self.available_visa_resources[0] if self.available_visa_resources else f"SIM::{m_name}::INSTR")
        )
        t0 = time.perf_counter()
        try:
            transport = self.visa_mgr.open_transport_session(res_addr, model=m_name)
            drv = create_smu_driver(m_name, transport=transport, spy=self.visa_mgr.spy, registry=self.registry)
            try:
                idn = drv.identify().strip()
            finally:
                transport.close()
            dt_ms = (time.perf_counter() - t0) * 1000.0
            short_lbl = f"{m_name} OK ({dt_ms:.1f}ms)"
            if m_name.split("-")[0] not in idn.upper() and not res_addr.upper().startswith("SIM::"):
                short_lbl = f"모델불일치 ({dt_ms:.0f}ms)"
            self._row_conn_status[row] = (True, short_lbl)
            self._populate_table()
            return True, idn
        except Exception as exc:
            self._row_conn_status[row] = (False, f"실패: {str(exc)[:18]}")
            self._populate_table()
            return False, str(exc)

    def test_all_connections(self) -> list[tuple[int, bool, str]]:
        """Verify *IDN? across every enabled node/channel row."""
        results: list[tuple[int, bool, str]] = []
        for idx, lch in enumerate(self.result_channels):
            if not lch.enabled:
                continue
            ok, msg = self.test_row_connection(idx)
            results.append((lch.node_id, ok, msg))
        return results

    def _refresh_compatibility_banner(self) -> None:
        ok, errs, notes = TSPLinkClusterManager.validate_mixed_model_compatibility(
            channels=self.result_channels,
            sync_mode=self.result_sync_mode,
            master_model=self.profile.model,
            registry=self.registry,
        )
        if not ok:
            self.lbl_compat_banner.setText("⚠ " + " ".join(errs))
            self.lbl_compat_banner.setStyleSheet(
                "background-color: #fef2f2; color: #b91c1c; border: 1.5px solid #f87171; border-radius: 5px; padding: 6px 10px; font-weight: bold;"
            )
            self.btn_switch_to_b.setVisible(True)
        else:
            msg = " ".join(notes) if notes else (
                f"단일 채널 모드 ({self.profile.model}) — [+ 장비(Node) 추가] 버튼으로 2대 이상 혼합 연결을 구성할 수 있습니다."
            )
            self.lbl_compat_banner.setText("✅ " + msg)
            self.lbl_compat_banner.setStyleSheet(
                "background-color: #ecfdf5; color: #065f46; border: 1px solid #6ee7b7; border-radius: 5px; padding: 6px 10px; font-weight: bold;"
            )
            self.btn_switch_to_b.setVisible(False)

    def _add_node_row(self) -> None:
        existing_nodes = {c.node_id for c in self.result_channels}
        next_node = (max(existing_nodes) + 1) if existing_nodes else 1
        if next_node > TSPLinkClusterManager.MAX_NODES:
            QMessageBox.warning(self, "최대 노드 초과", f"최대 {TSPLinkClusterManager.MAX_NODES}대까지 구성할 수 있습니다.")
            return
        m_name = self.profile.model
        ch_name = self.profile.channel_names[0]
        self.result_channels.append(
            LogicalChannel(
                node_id=next_node,
                channel_name=ch_name,
                role=ChannelRole.PARALLEL_DUT.value,
                enabled=True,
                model_name=m_name,
                visa_resource=f"SIM::{m_name}::INSTR",
            )
        )
        self._populate_table()
        self._refresh_compatibility_banner()

    def _remove_last_node(self) -> None:
        if len(self.result_channels) <= 1:
            return
        last_node_id = max(c.node_id for c in self.result_channels)
        self.result_channels = [c for c in self.result_channels if c.node_id != last_node_id]
        self._populate_table()
        self._refresh_compatibility_banner()

    def _preset_2450_plus_2461(self) -> None:
        """One-click preset configuring Node 1 = 2450 (Source/Drain) + Node 2 = 2461 (High-Current/Pulse)."""
        self.result_channels = [
            LogicalChannel(
                node_id=1,
                channel_name="smu",
                role=ChannelRole.PARALLEL_DUT.value,
                enabled=True,
                model_name="2450",
                independent_override=True,
                override_sweep_type="LINEAR",
                override_source_func="VOLTAGE",
                override_start_level=0.0,
                override_stop_level=5.0,
                override_compliance=0.1,
                visa_resource=self.available_visa_resources[0] if self.available_visa_resources else "SIM::2450::INSTR",
            ),
            LogicalChannel(
                node_id=2,
                channel_name="smu",
                role=ChannelRole.PARALLEL_DUT.value,
                enabled=True,
                model_name="2461",
                independent_override=True,
                override_sweep_type="LINEAR",
                override_source_func="VOLTAGE",
                override_start_level=0.0,
                override_stop_level=5.0,
                override_compliance=1.0,
                visa_resource="SIM::2461::INSTR",
            ),
        ]
        self._populate_table()
        self._refresh_compatibility_banner()

    def _preset_all_parallel(self) -> None:
        for lch in self.result_channels:
            lch.role = ChannelRole.PARALLEL_DUT.value
        self._populate_table()

    def _preset_fet_roles(self) -> None:
        roles = [ChannelRole.FET_DRAIN.value, ChannelRole.FET_GATE.value, ChannelRole.FET_SOURCE.value]
        for idx, lch in enumerate(self.result_channels):
            lch.role = roles[idx] if idx < len(roles) else ChannelRole.FET_SUBSTRATE.value
        self._populate_table()

    def _sync_text_cells(self) -> None:
        for r, lch in enumerate(self.result_channels):
            if r >= self.tbl_channels.rowCount():
                break
            item_range = self.tbl_channels.item(r, 10)
            if item_range:
                parts = [x.strip() for x in item_range.text().split(",")]
                try:
                    lch.override_start_level = float(parts[0])
                    if len(parts) > 1:
                        lch.override_stop_level = float(parts[1])
                except Exception:
                    pass
            item_lim = self.tbl_channels.item(r, 11)
            if item_lim:
                try:
                    lch.override_compliance = float(item_lim.text().strip())
                except Exception:
                    pass

    def _on_apply(self) -> None:
        self._sync_text_cells()
        ok, errs, _ = TSPLinkClusterManager.validate_mixed_model_compatibility(
            channels=self.result_channels,
            sync_mode=self.result_sync_mode,
            master_model=self.profile.model,
            registry=self.registry,
        )
        if not ok:
            QMessageBox.warning(
                self,
                "혼합 연결 방식 호환성 오류",
                "\n".join(errs) + "\n\n상단에서 '방식 B: USB / LAN 개별 연결'을 선택하시면 모든 모델을 자유롭게 혼합 사용할 수 있습니다.",
            )
            return
        self.accept()
