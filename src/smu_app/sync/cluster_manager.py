"""Phase 2 Complete Implementation: Multi-SMU (1~10 Nodes) TSP-Link Hardware Sync,
Logical Channel Role Mapping (1/2/4-CH & FET Gate/Drain/Source), and Output Combining Engine.

Implements v3.0 Section 03 (MULTI-SMU SYNC):
- Node 1 (Master) .. Node 10 (Slave) automatic discovery, initialization, and 3 Sync Line hardware trigger routing.
- Non-TSPLink variant (2604B/2614B/2634B) software synchronization fallback with explicit skew warning.
- Logical channel abstraction across 1-CH (2450/2460/2461/2470/2601B-PULSE/2651A),
  2-CH (2602B/2612B/2636B), and 4-CH (2606B: 2 internal modules x 2 CH).
- Multi-terminal FET evaluation role assignment (GATE, DRAIN, SOURCE, SUBSTRATE, PARALLEL_DUT).
- Output Combining Wizard engine (Parallel 2x current / Series 2x voltage) with mandatory 4-step physical
  wiring checklist gate, 2606B block, 10% Slave compliance margin, and synchronized safe shutdown.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from smu_app.core.models import (
    ChannelConfig,
    CombiningMode,
    LogicalChannel,
    MultiUnitSyncMode,
    OutputOffMode,
    SourceFunction,
)
from smu_app.core.profiles import ProfileRegistry, SMUModelProfile
from smu_app.drivers.base_smu import BaseTSPDriver


class ChannelRole(str, Enum):
    """Logical role assigned to a Node-Channel in multi-SMU tests."""

    PARALLEL_DUT = "PARALLEL_DUT"      # Independent parallel DUT testing (1~10 DUTs simultaneously)
    FET_GATE = "FET_GATE"              # 3-terminal transistor Gate step/sweep channel
    FET_DRAIN = "FET_DRAIN"            # 3-terminal transistor Drain primary sweep channel
    FET_SOURCE = "FET_SOURCE"          # 3-terminal transistor Source common/ground channel
    FET_SUBSTRATE = "FET_SUBSTRATE"    # 4-terminal Body/Substrate bias channel
    COMBINED_MASTER = "COMBINED_MASTER"
    COMBINED_SLAVE = "COMBINED_SLAVE"


@dataclass
class CombiningChecklistState:
    """Mandatory 4-step physical wiring verification gate before activating combined SMU outputs."""

    power_off_verified: bool = False
    cable_polarity_verified: bool = False
    sense_lead_verified: bool = False
    safety_shield_verified: bool = False

    @property
    def all_verified(self) -> bool:
        return (
            self.power_off_verified
            and self.cable_polarity_verified
            and self.sense_lead_verified
            and self.safety_shield_verified
        )


@dataclass
class CombinedOutputConfig:
    """Validated master-slave output combining configuration (Parallel Current or Series Voltage)."""

    mode: CombiningMode = CombiningMode.NONE
    master_channel: LogicalChannel = field(default_factory=lambda: LogicalChannel(1, "smua", ChannelRole.COMBINED_MASTER.value))
    slave_channel: LogicalChannel = field(default_factory=lambda: LogicalChannel(2, "smua", ChannelRole.COMBINED_SLAVE.value))
    total_target_level: float = 0.0
    total_compliance_limit: float = 0.1
    master_level: float = 0.0
    slave_level: float = 0.0
    master_compliance: float = 0.1
    slave_compliance: float = 0.09
    pulse_only_enforced: bool = False
    checklist: CombiningChecklistState = field(default_factory=CombiningChecklistState)
    safety_notes: list[str] = field(default_factory=list)


@dataclass
class ClusterTopology:
    """Represents a 1..10 SMU Master-Slave cluster configuration and logical channel map."""

    master_model: str
    specific_variant: str
    node_count: int = 1
    sync_mode: MultiUnitSyncMode = MultiUnitSyncMode.TSPLINK_HW
    sync_line_arm: int = 1
    sync_line_trigger: int = 2
    sync_line_end: int = 3
    use_software_sync_fallback: bool = False
    logical_channels: list[LogicalChannel] = field(default_factory=list)
    combining_mode: CombiningMode = CombiningMode.NONE
    wiring_checklist_verified: bool = False
    warnings: list[str] = field(default_factory=list)


class TSPLinkClusterManager:
    """Manages 1~10 node TSP-Link initialization, 3 Sync Lines, logical channel mapping, and output combining."""

    MAX_NODES = 10

    def __init__(self, master_driver: BaseTSPDriver) -> None:
        self.master = master_driver

    def build_logical_channels(
        self,
        node_count: int,
        default_role: ChannelRole = ChannelRole.PARALLEL_DUT,
    ) -> list[LogicalChannel]:
        """Expand 1..node_count instruments into flat Node-Channel logical channels (handles 1CH, 2CH, 4CH 2606B)."""
        prof = self.master.profile
        channels: list[LogicalChannel] = []
        if prof.model == "2606B":
            # Each 2606B unit houses 2 internal TSP nodes x 2 channels = 4 channels per 1U chassis
            for chassis_idx in range(node_count):
                base_node = chassis_idx * 2 + 1
                for sub_offset in (0, 1):
                    n_id = base_node + sub_offset
                    for ch_name in ("smua", "smub"):
                        channels.append(
                            LogicalChannel(
                                node_id=n_id,
                                channel_name=ch_name,
                                role=default_role.value,
                                enabled=True,
                                model_name=prof.model,
                            )
                        )
            return channels

        for n_id in range(1, node_count + 1):
            for ch_name in prof.channel_names:
                channels.append(
                    LogicalChannel(
                        node_id=n_id,
                        channel_name=ch_name,
                        role=default_role.value,
                        enabled=True,
                        model_name=prof.model,
                    )
                )
        return channels

    @staticmethod
    def validate_mixed_model_compatibility(
        channels: list[LogicalChannel],
        sync_mode: MultiUnitSyncMode,
        master_model: str,
        registry: ProfileRegistry | None = None,
    ) -> tuple[bool, list[str], list[str]]:
        """Validate whether the selected multi-SMU model combination is compatible with Mode A (TSP-Link) or Mode B (Multi-VISA SW).

        Returns:
            (is_valid, errors, warnings_or_notes)
        """
        reg = registry or ProfileRegistry()
        active = [c for c in channels if c.enabled]
        if len(active) <= 1:
            return True, [], []

        models_used: list[str] = []
        families: set[str] = set()
        for c in active:
            m = (c.model_name or master_model).strip()
            models_used.append(m)
            prof = reg.get(m)
            families.add("24xx" if prof.is_24xx else "26xx")

        unique_models = list(dict.fromkeys(models_used))
        is_mixed = len(unique_models) > 1
        errors: list[str] = []
        notes: list[str] = []

        if sync_mode == MultiUnitSyncMode.TSPLINK_HW:
            if len(families) > 1:
                errors.append(
                    f"[방식 A (TSP-Link) 불가 조합] 선택한 모델({', '.join(unique_models)})에 24xx 계열과 26xx 계열이 함께 포함되어 있습니다. "
                    "24xx와 26xx는 TSP-Link 하드웨어 세대가 달라 직접 케이블 결합이 불가하므로 '방식 B (USB/LAN 개별 연결)'를 선택해 주세요."
                )
                return False, errors, notes
            if is_mixed:
                fam_lbl = next(iter(families))
                notes.append(
                    f"[방식 A (TSP-Link < 1µs) 혼합 호환] 동일 {fam_lbl} 계열 혼합 구성({', '.join(unique_models)}) — "
                    "Master 1대만 PC에 연결하고 후면 TSP-Link 케이블로 데이지체인 동기화합니다."
                )
            else:
                notes.append(
                    f"[방식 A (TSP-Link < 1µs)] {unique_models[0]} × {len(active)}CH 하드웨어 정밀 동기화 모드입니다."
                )
            return True, errors, notes

        # Mode B: MULTI_VISA_SW — all 8 models can be freely combined!
        if is_mixed:
            notes.append(
                f"[방식 B (USB/LAN 개별 연결, SW 동기화 1~20ms)] 이종 모델 혼합 구성({', '.join(unique_models)}) — "
                "TSP-Link 케이블 없이 각 장비의 개별 VISA 리소스로 동시 제어합니다."
            )
        else:
            notes.append(
                f"[방식 B (USB/LAN 개별 연결, SW 동기화 1~20ms)] {unique_models[0]} × {len(active)}CH 개별 VISA 멀티세션으로 동작합니다."
            )
        return True, errors, notes

    def validate_and_initialize_cluster(
        self,
        node_count: int,
        specific_variant: str | None = None,
    ) -> ClusterTopology:
        if not (1 <= node_count <= self.MAX_NODES):
            raise ValueError(f"지원 가능한 SMU 장비 대수는 1~{self.MAX_NODES}대입니다. (입력값: {node_count})")

        prof: SMUModelProfile = self.master.profile
        variant = (specific_variant or prof.model).upper()
        logical_chs = self.build_logical_channels(node_count)
        topo = ClusterTopology(
            master_model=prof.model,
            specific_variant=variant,
            node_count=node_count,
            logical_channels=logical_chs,
        )

        if node_count == 1 and prof.channels == 1:
            return topo

        if not prof.supports_tsplink_for_variant(variant):
            topo.use_software_sync_fallback = True
            topo.warnings.append(
                f"[SW 동기화 경고] {variant} 모델은 TSP-Link 하드웨어 포트를 지원하지 않습니다. "
                "개별 LAN/USB 세션 기반 SW 동기화 모드로 동작하며 1~20ms의 시작 타이밍 편차(Skew)가 발생할 수 있습니다."
            )
            return topo

        # Initialize TSP-Link ring across 1..node_count
        self.master.transport.write(f"tsplink.initialize({node_count})")
        state = self.master.transport.query("print(tsplink.state)").strip().lower()
        if "online" not in state:
            raise RuntimeError(
                f"TSP-Link 초기화 실패 (현재 상태: {state}). CAT5e 크로스오버 케이블 및 노드 번호(1~{node_count})를 확인하세요."
            )
        return topo

    def generate_tsplink_sync_script(
        self,
        topo: ClusterTopology,
        step_count: int,
        delay_s: float,
    ) -> list[str]:
        """Generate TSP-Link 3 Sync Line (Line 1: Arm, Line 2: Step Trigger, Line 3: Done) hardware sync commands."""
        active_chs = [c for c in topo.logical_channels if c.enabled]
        if len(active_chs) <= 1 or topo.use_software_sync_fallback:
            return []

        prof = self.master.profile
        cmds: list[str] = [
            f"-- TSP-Link Hardware Trigger Sync across {len(active_chs)} logical channels (Sync Lines 1, 2, 3)",
        ]
        if prof.is_24xx:
            cmds.extend([
                f"tsplink.line[{topo.sync_line_arm}].mode = tsplink.MODE_TRIGGER_OPEN_DRAIN",
                f"tsplink.line[{topo.sync_line_trigger}].mode = tsplink.MODE_TRIGGER_OPEN_DRAIN",
                "tsplink.line[1].reset(); tsplink.line[2].reset()",
            ])
            for ch in active_chs:
                if ch.node_id > 1:
                    cmds.append(
                        f"node[{ch.node_id}].execute(\"tsplink.line[1].mode = tsplink.MODE_TRIGGER_OPEN_DRAIN; "
                        f"trigger.model.setblock(1, trigger.BLOCK_WAIT, trigger.EVENT_TSPLINK1); "
                        f"trigger.model.initiate()\")"
                    )
            cmds.append("tsplink.line[1].assert()")
        else:
            # 26xx TSP-Link trigger lines (tsplink.trig[1..3])
            cmds.extend([
                "tsplink.trig[1].Reset()",
                "tsplink.trig[2].Reset()",
                "tsplink.trig[1].mode = tsplink.TRIG_FALLING",
                "tsplink.trig[2].mode = tsplink.TRIG_FALLING",
                "tsplink.trig[1].stimulus = smua.trigger.ARMED_EVENT_ID",
            ])
            for ch in active_chs:
                pfx = ch.tsp_prefix
                if pfx != "smua":
                    cmds.extend([
                        f"{pfx}.trigger.arm.stimulus = tsplink.trig[1].EVENT_ID",
                        f"{pfx}.trigger.source.stimulus = tsplink.trig[2].EVENT_ID",
                        f"{pfx}.trigger.count = {step_count}",
                        f"{pfx}.source.output = {pfx}.OUTPUT_ON",
                        f"{pfx}.trigger.initiate()",
                    ])
            cmds.append("tsplink.trig[1].assert()")
        return cmds

    @staticmethod
    def compute_combined_slave_compliance(
        master_compliance: float,
        mode: CombiningMode,
        profile: SMUModelProfile,
        wiring_verified: bool,
    ) -> tuple[float, list[str]]:
        """Enforce v3.0 Combining SMUs safety rules: wiring checklist gate + 10% Slave compliance margin."""
        if mode == CombiningMode.NONE:
            return master_compliance, []
        if not profile.combining_supported:
            raise ValueError(f"{profile.model} 모델(예: 2606B)은 하드웨어 출력 직/병렬 결합이 금지되어 있습니다.")
        if not wiring_verified:
            raise PermissionError(
                "출력 결합(직렬/병렬) 활성화 전 물리 결선 다이어그램 확인 체크리스트를 모두 통과해야 합니다."
            )
        slave_limit = round(master_compliance * 0.90, 6)
        notes = [
            f"출력 결합({mode.value}) 안전 규칙 적용: Slave Compliance를 Master({master_compliance}) 대비 10% 낮은 {slave_limit}로 자동 설정했습니다."
        ]
        if profile.model == "2651A":
            notes.append("2651A 고전력 결합 모드: DC 연속 출력이 차단되며 Pulse 전용 모드로 제한됩니다.")
        return slave_limit, notes

    @classmethod
    def build_combined_output_config(
        cls,
        profile: SMUModelProfile,
        mode: CombiningMode,
        source_func: SourceFunction,
        total_target_level: float,
        total_compliance_limit: float,
        checklist: CombiningChecklistState,
        master_ch: LogicalChannel | None = None,
        slave_ch: LogicalChannel | None = None,
    ) -> CombinedOutputConfig:
        """Validate and compute exact Master & Slave source/compliance values for Parallel or Series combining."""
        if mode == CombiningMode.NONE:
            raise ValueError("결합 모드가 선택되지 않았습니다.")
        if not profile.combining_supported:
            raise ValueError(f"{profile.model} 모델은 출력 직/병렬 결합이 하드웨어적으로 금지되어 있습니다.")

        # Hardware safety rule enforcement: NEVER parallel voltage sources or series current sources
        if mode == CombiningMode.PARALLEL_CURRENT and source_func == SourceFunction.VOLTAGE:
            raise ValueError(
                "[하드웨어 안전 차단] 전압원(Voltage Source) 병렬 연결은 금지됩니다! "
                "병렬 고전류 결합 시 반드시 두 장비를 전류원(Current Source) 모드로 설정해야 합니다."
            )
        if mode == CombiningMode.SERIES_VOLTAGE and source_func == SourceFunction.CURRENT:
            raise ValueError(
                "[하드웨어 안전 차단] 전류원(Current Source) 직렬 연결은 금지됩니다! "
                "직렬 고전압 결합 시 반드시 두 장비를 전압원(Voltage Source) 모드로 설정해야 합니다."
            )

        slave_comp, notes = cls.compute_combined_slave_compliance(
            master_compliance=total_compliance_limit,
            mode=mode,
            profile=profile,
            wiring_verified=checklist.all_verified,
        )

        # Split source level evenly (each SMU provides 50% of the doubled total current or voltage)
        half_level = round(total_target_level / 2.0, 6)
        max_single = (
            profile.max_pulse_current_a
            if mode == CombiningMode.PARALLEL_CURRENT
            else profile.max_pulse_voltage_v
        )
        if abs(half_level) > max_single * 1.001:
            raise ValueError(
                f"결합 후 개별 SMU 분담 출력({half_level})이 단일 장비 최대 허용치({max_single})를 초과합니다."
            )

        m_ch = master_ch or LogicalChannel(1, profile.channel_names[0], ChannelRole.COMBINED_MASTER.value)
        s_ch = slave_ch or (
            LogicalChannel(1, profile.channel_names[1], ChannelRole.COMBINED_SLAVE.value)
            if len(profile.channel_names) >= 2
            else LogicalChannel(2, profile.channel_names[0], ChannelRole.COMBINED_SLAVE.value)
        )

        return CombinedOutputConfig(
            mode=mode,
            master_channel=m_ch,
            slave_channel=s_ch,
            total_target_level=total_target_level,
            total_compliance_limit=total_compliance_limit,
            master_level=half_level,
            slave_level=half_level,
            master_compliance=total_compliance_limit,
            slave_compliance=slave_comp,
            pulse_only_enforced=(profile.model == "2651A"),
            checklist=checklist,
            safety_notes=notes,
        )

    def generate_safe_combined_shutdown_script(self, combined: CombinedOutputConfig) -> list[str]:
        """Synchronized safe shutdown sequence setting both Master & Slave to High-Z / 0 level."""
        m_pfx = combined.master_channel.tsp_prefix
        s_pfx = combined.slave_channel.tsp_prefix
        if self.master.profile.is_24xx:
            return [
                f"{s_pfx}.source.level = 0; {m_pfx}.source.level = 0",
                f"{s_pfx}.source.offmode = {s_pfx}.OFFMODE_HIGHZ; {m_pfx}.source.offmode = {m_pfx}.OFFMODE_HIGHZ",
                f"{s_pfx}.source.output = {s_pfx}.OFF; {m_pfx}.source.output = {m_pfx}.OFF",
            ]
        return [
            f"{s_pfx}.source.levelv = 0; {s_pfx}.source.leveli = 0",
            f"{m_pfx}.source.levelv = 0; {m_pfx}.source.leveli = 0",
            f"{s_pfx}.source.offmode = {s_pfx}.OUTPUT_HIGH_Z; {m_pfx}.source.offmode = {m_pfx}.OUTPUT_HIGH_Z",
            f"{s_pfx}.source.output = {s_pfx}.OUTPUT_OFF; {m_pfx}.source.output = {m_pfx}.OUTPUT_OFF",
        ]
