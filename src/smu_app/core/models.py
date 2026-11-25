"""Core Data Structures and Enumerations for Phase 1, 2, and 3 SMU Operations."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class SourceFunction(str, Enum):
    VOLTAGE = "VOLTAGE"
    CURRENT = "CURRENT"


class MeasureFunction(str, Enum):
    CURRENT = "CURRENT"
    VOLTAGE = "VOLTAGE"
    RESISTANCE = "RESISTANCE"
    POWER = "POWER"


class SenseMode(str, Enum):
    WIRE_2 = "2WIRE"
    WIRE_4 = "4WIRE"


class TerminalLocation(str, Enum):
    FRONT = "FRONT"
    REAR = "REAR"


class OutputOffMode(str, Enum):
    NORMAL = "NORMAL"
    HIGH_Z = "HIGH_Z"
    ZERO = "ZERO"
    GUARD = "GUARD"


class SweepType(str, Enum):
    LINEAR = "LINEAR"
    LOG = "LOG"
    DUAL = "DUAL"
    LIST = "LIST"
    PULSE_TRAIN = "PULSE_TRAIN"
    PULSE_SWEEP = "PULSE_SWEEP"


class RepeatMode(str, Enum):
    SINGLE = "SINGLE"
    FINITE = "FINITE"
    CONTINUOUS = "CONTINUOUS"


class AcquisitionMode(str, Enum):
    """v3.0 Dual Continuous Data Collection Strategy (Phase 2/3)."""

    LOW_SPEED_STREAMING = "LOW_SPEED_STREAMING"  # Step >= 20ms: real-time chunk polling
    HIGH_SPEED_BURST = "HIGH_SPEED_BURST"        # Step < 20ms / 1MS/s: internal buffer burst -> binary transfer


class CombiningMode(str, Enum):
    """Phase 2 Multi-SMU Output Combining Mode."""

    NONE = "NONE"
    PARALLEL_CURRENT = "PARALLEL_CURRENT"
    SERIES_VOLTAGE = "SERIES_VOLTAGE"


class MultiUnitSyncMode(str, Enum):
    """Multi-SMU physical connection & synchronization mode (Mode A vs Mode B)."""

    TSPLINK_HW = "TSPLINK_HW"        # 방식 A: TSP-Link 케이블 하드웨어 동기화 (< 1 us, 같은 계열 혼합 가능)
    MULTI_VISA_SW = "MULTI_VISA_SW"  # 방식 B: USB/LAN 개별 연결 SW 멀티세션 동기화 (1~20 ms, 전 모델 자유 혼합)


@dataclass
class LogicalChannel:
    """Phase 2 Node-Channel logical abstraction (supports 1~10 nodes, 1/2/4-CH models, mixed models, and per-node overrides)."""

    node_id: int = 1
    channel_name: str = "smu"  # "smu" for 24xx, "smua"/"smub" for 26xx
    role: str = "PRIMARY"      # e.g. "PRIMARY", "PARALLEL_DUT", "FET_GATE", "FET_DRAIN", "FET_SOURCE"
    enabled: bool = True
    # Mixed-model support (e.g. Node 1 = "2450", Node 2 = "2461"). Empty string inherits Master model.
    model_name: str = ""
    # Requirement 7: Independent per-node/per-channel waveform & source function override when nodes >= 2
    independent_override: bool = False
    override_sweep_type: str = "LINEAR"       # SweepType value
    override_source_func: str = "VOLTAGE"     # SourceFunction value ("VOLTAGE" or "CURRENT")
    override_start_level: float = 0.0
    override_stop_level: float = 1.2
    override_compliance: float = 0.1
    visa_resource: str = ""

    @property
    def label(self) -> str:
        return f"Node{self.node_id}.{self.channel_name}"

    @property
    def tsp_prefix(self) -> str:
        if self.node_id == 1:
            return self.channel_name
        if self.channel_name.startswith("node["):
            return self.channel_name
        return f"node[{self.node_id}].{self.channel_name}"


@dataclass
class ChannelConfig:
    """Configuration for an SMU logical channel (DC, Precision, and Safety parameters)."""

    logical_channel: LogicalChannel = field(default_factory=LogicalChannel)
    source_func: SourceFunction = SourceFunction.VOLTAGE
    measure_func: MeasureFunction = MeasureFunction.CURRENT
    source_level: float = 0.0
    source_range: float | None = None  # None = Auto range
    measure_range: float | None = None
    compliance_limit: float = 0.1      # Current limit (A) when sourcing V, or Voltage limit (V) when sourcing I
    ovp_limit_v: float | None = None
    nplc: float = 1.0
    sense_mode: SenseMode = SenseMode.WIRE_2
    terminals: TerminalLocation = TerminalLocation.FRONT
    output_off_mode: OutputOffMode = OutputOffMode.NORMAL
    auto_zero: str = "ON"              # "ON", "OFF", "ONCE"
    source_readback: bool = True
    high_capacitance: bool = False
    offset_compensated_ohms: bool = False
    filter_enabled: bool = False
    filter_type: str = "MOVING"        # "MOVING", "REPEAT", "MEDIAN"
    filter_count: int = 10
    contact_check_enabled: bool = False
    contact_check_threshold_ohm: float = 50.0
    breakdown_protection_enabled: bool = False


@dataclass
class WaveformStep:
    """Single step in an arbitrary waveform / list sweep / pulse sequence."""

    step_index: int
    duration_s: float
    level: float
    compliance_limit: float | None = None
    measure_enable: bool = True
    is_pulse: bool = False
    pulse_width_s: float | None = None
    pulse_off_time_s: float | None = None


@dataclass
class SweepRecipe:
    """Complete sweep or custom waveform recipe for Phase 1-3 execution."""

    name: str = "default_iv_sweep"
    sweep_type: SweepType = SweepType.LINEAR
    source_func: SourceFunction = SourceFunction.VOLTAGE
    measure_func: MeasureFunction = MeasureFunction.CURRENT
    start_level: float = 0.0
    stop_level: float = 5.0
    points: int = 51
    delay_s: float = 0.01
    compliance_limit: float = 0.1
    nplc: float = 1.0
    bias_level: float = 0.0
    pulse_width_s: float = 0.001
    pulse_period_s: float = 0.020
    repeat_mode: RepeatMode = RepeatMode.SINGLE
    repeat_count: int = 1
    acquisition_mode: AcquisitionMode = AcquisitionMode.HIGH_SPEED_BURST
    abort_on_limit: bool = True
    end_Return_to_bias: bool = True
    apply_negative_zero_fix: bool = True
    use_digitizer: bool = False
    digitizer_rate_sps: int = 100000
    limit_low: float | None = None
    limit_high: float | None = None
    custom_steps: list[WaveformStep] = field(default_factory=list)

    def generate_levels(self) -> list[float]:
        """Expand linear/log/dual/list levels into an explicit list of source points."""
        import numpy as np

        if self.sweep_type == SweepType.LIST and self.custom_steps:
            return [s.level for s in self.custom_steps]
        pts = max(2, int(self.points))
        if self.sweep_type == SweepType.LOG:
            start = self.start_level if self.start_level != 0 else 1e-6
            stop = self.stop_level if self.stop_level != 0 else 1.0
            sign = -1.0 if stop < 0 else 1.0
            arr = sign * np.logspace(np.log10(abs(start)), np.log10(abs(stop)), pts)
            return [float(x) for x in arr]
        arr = np.linspace(self.start_level, self.stop_level, pts)
        levels = [float(x) for x in arr]
        if self.sweep_type == SweepType.DUAL:
            levels = levels + list(reversed(levels[:-1]))
        return levels


@dataclass
class ReadingPoint:
    """Single synchronized source-measure reading captured from SMU buffer."""

    index: int
    timestamp_s: float
    voltage_v: float
    current_a: float
    resistance_ohm: float = float("nan")
    power_w: float = float("nan")
    status_bits: int = 0
    in_compliance: bool = False
    is_overrange: bool = False
    is_4wire: bool = False
    limit_passed: bool = True
    node_id: int = 1
    channel: str = "smu"


@dataclass
class MeasurementResult:
    """Aggregated measurement dataset with metadata and computed analysis metrics."""

    session_id: str
    model: str
    recipe_name: str
    started_at: datetime
    completed_at: datetime
    channel_config: ChannelConfig
    recipe: SweepRecipe
    points: list[ReadingPoint] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    aborted: bool = False
