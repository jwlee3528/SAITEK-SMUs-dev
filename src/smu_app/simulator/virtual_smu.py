"""Physics-Based Virtual SMU Simulator (Ohm, Shockley Diode, RC Transient + Gaussian Noise).

Implements v3.0 Value-Add 5:
Allows 100% offline verification of Phase 1-3 features (8 models, TSP commands, binary buffers,
Interlock/Contact Check simulation, I-V sweeps, and pulse digitizing) without physical hardware.
"""

from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass, field
from enum import Enum

from smu_app.core.command_spy import CommandSpy
from smu_app.core.models import (
    ChannelConfig,
    ReadingPoint,
    SourceFunction,
    SweepRecipe,
)
from smu_app.core.profiles import ProfileRegistry, SMUModelProfile


class VirtualDUTType(str, Enum):
    RESISTOR_1K = "RESISTOR_1K"          # 1 kOhm precision resistor
    POWER_RESISTOR_1OHM = "POWER_1OHM"   # 1 Ohm high-current power load
    SILICON_DIODE = "SILICON_DIODE"      # Shockley diode (Is=1e-12 A, n=1.25, Rs=1.5 Ohm, Vbd=75V)
    RC_LOAD = "RC_LOAD"                  # R=100 Ohm, tau=200us transient load
    HV_INSULATOR = "HV_INSULATOR"        # 10 GOhm leakage + 650V avalanche breakdown


@dataclass
class VirtualHardwareState:
    """Simulated hardware registers for an SMU node."""

    model: str = "2450"
    language: str = "TSP"
    interlock_engaged: bool = True
    contact_resistance_ohm: float = 1.2
    internal_temp_c: float = 26.5
    output_enabled: bool = False
    dut_type: VirtualDUTType = VirtualDUTType.SILICON_DIODE
    noise_level: float = 1e-7
    aborted: bool = False
    last_buffer: list[ReadingPoint] = field(default_factory=list)


class VirtualSMUPhysicsEngine:
    """Computes realistic V/I/R/P and transient responses for a simulated DUT."""

    @staticmethod
    def evaluate_point(
        source_func: SourceFunction,
        source_level: float,
        compliance_limit: float,
        t_step_s: float,
        dut: VirtualDUTType,
        noise_std: float = 1e-7,
    ) -> tuple[float, float, bool]:
        """Return (voltage_v, current_a, in_compliance) for a single source point."""
        rng = random.Random(int(abs(source_level * 1e6) + int(t_step_s * 1e6)) ^ 0x5A17)
        noise = rng.gauss(0.0, noise_std)

        if dut == VirtualDUTType.RESISTOR_1K:
            r = 1000.0
            if source_func == SourceFunction.VOLTAGE:
                v = source_level
                i_ideal = v / r + noise
            else:
                i_ideal = source_level
                v = i_ideal * r + noise * r

        elif dut == VirtualDUTType.POWER_RESISTOR_1OHM:
            r = 1.0
            if source_func == SourceFunction.VOLTAGE:
                v = source_level
                i_ideal = v / r + noise * 1e3
            else:
                i_ideal = source_level
                v = i_ideal * r + noise

        elif dut == VirtualDUTType.SILICON_DIODE:
            # Shockley diode equation with series resistance approximation and reverse breakdown at -75V
            vt = 0.02585  # kT/q at 300K
            n = 1.25
            i_sat = 1e-11
            r_series = 1.5
            if source_func == SourceFunction.VOLTAGE:
                v = source_level
                if v >= 0:
                    # Iterative solve for V = Vd + I*Rs
                    vd = min(v, 0.85)
                    for _ in range(8):
                        id_val = i_sat * (math.exp(min(vd / (n * vt), 35.0)) - 1.0)
                        f = vd + id_val * r_series - v
                        df = 1.0 + (i_sat / (n * vt)) * math.exp(min(vd / (n * vt), 35.0)) * r_series
                        vd -= f / max(df, 1e-9)
                    i_ideal = i_sat * (math.exp(min(vd / (n * vt), 35.0)) - 1.0) + noise
                elif v < -75.0:
                    # Reverse breakdown region
                    i_ideal = -i_sat - ((-75.0 - v) / 10.0) ** 2 + noise
                else:
                    i_ideal = -i_sat + (v / 1e9) + noise * 1e-3
            else:
                i_ideal = source_level
                if i_ideal > 0:
                    v = n * vt * math.log(max(i_ideal / i_sat, 1.0) + 1.0) + i_ideal * r_series
                else:
                    v = i_ideal * 1e6

        elif dut == VirtualDUTType.RC_LOAD:
            r_steady = 100.0
            tau = 0.0005  # 500us RC time constant
            if source_func == SourceFunction.VOLTAGE:
                v = source_level
                i_steady = v / r_steady
                i_peak = v / 20.0
                i_ideal = i_steady + (i_peak - i_steady) * math.exp(-max(t_step_s, 0.0) / tau) + noise
            else:
                i_ideal = source_level
                v = i_ideal * r_steady * (1.0 - 0.5 * math.exp(-max(t_step_s, 0.0) / tau))

        else:  # HV_INSULATOR
            r_ins = 1e10  # 10 GOhm
            v_bd = 650.0
            if source_func == SourceFunction.VOLTAGE:
                v = source_level
                if abs(v) < v_bd:
                    i_ideal = (v / r_ins) + noise * 1e-4
                else:
                    sign = 1.0 if v >= 0 else -1.0
                    i_ideal = sign * (1e-6 * math.exp(min((abs(v) - v_bd) / 15.0, 20.0)))
            else:
                i_ideal = source_level
                v = min(abs(i_ideal) * r_ins, v_bd)

        # Apply Compliance clamping
        in_comp = False
        if source_func == SourceFunction.VOLTAGE:
            if abs(i_ideal) >= abs(compliance_limit):
                i_actual = math.copysign(abs(compliance_limit), i_ideal)
                in_comp = True
            else:
                i_actual = i_ideal
            v_actual = v
        else:
            if abs(v) >= abs(compliance_limit):
                v_actual = math.copysign(abs(compliance_limit), v)
                in_comp = True
            else:
                v_actual = v
            i_actual = i_ideal

        return float(v_actual), float(i_actual), in_comp


class VirtualSMUTransport:
    """In-memory VISA-compatible transport session simulating all 8 Keithley SMU models."""

    def __init__(
        self,
        model: str = "2450",
        spy: CommandSpy | None = None,
        registry: ProfileRegistry | None = None,
    ) -> None:
        self.registry = registry or ProfileRegistry()
        self.profile: SMUModelProfile = self.registry.get(model)
        self.resource_name = f"SIM::{self.profile.model}::INSTR"
        self.spy = spy or CommandSpy()
        self.state = VirtualHardwareState(model=self.profile.model, language="TSP")

    def write(self, command: str) -> None:
        t0 = time.perf_counter()
        cmd = command.strip()
        if cmd.upper().startswith("*LANG"):
            parts = cmd.split()
            if len(parts) > 1:
                self.state.language = parts[1].upper()
        elif "abort" in cmd.lower() or "output = smu.off" in cmd.lower() or "output_off" in cmd.lower():
            self.state.aborted = True
            self.state.output_enabled = False
        elif "output = smu.on" in cmd.lower() or "output_on" in cmd.lower() or "initiate()" in cmd.lower():
            self.state.aborted = False
            self.state.output_enabled = True
        dt = (time.perf_counter() - t0) * 1000.0
        self.spy.record("TX", f"SIM-{self.profile.model}", cmd, elapsed_ms=dt)

    def query(self, command: str) -> str:
        t0 = time.perf_counter()
        cmd = command.strip()
        self.spy.record("TX", f"SIM-{self.profile.model}", cmd)
        resp = "0"
        if cmd.upper() == "*IDN?":
            resp = f"KEITHLEY INSTRUMENTS,MODEL {self.profile.model},SIM-{self.profile.model}-01,1.7.12-SIM"
        elif cmd.upper() == "*LANG?":
            resp = self.state.language
        elif "interlock" in cmd.lower():
            resp = "1" if self.state.interlock_engaged else "0"
        elif "tsplink.state" in cmd.lower():
            resp = "online"
        elif "contact.check" in cmd.lower():
            passed = self.state.contact_resistance_ohm <= 50.0
            resp = "true" if passed else "false"
        elif "contact.r" in cmd.lower():
            resp = f"{self.state.contact_resistance_ohm:.2f}, {self.state.contact_resistance_ohm + 0.3:.2f}"
        elif "errorqueue" in cmd.lower() or "eventlog" in cmd.lower():
            resp = "0\tNo error"
        elif "linefreq" in cmd.lower():
            resp = "60"
        dt = (time.perf_counter() - t0) * 1000.0
        self.spy.record("RX", f"SIM-{self.profile.model}", resp, elapsed_ms=dt)
        return resp

    def query_binary_values(self, command: str, datatype: str = "d") -> list[float]:
        t0 = time.perf_counter()
        self.spy.record("TX", f"SIM-{self.profile.model}", f"{command} [IEEE488.2 BINARY '{datatype}']")
        vals = [pt.current_a for pt in self.state.last_buffer]
        dt = (time.perf_counter() - t0) * 1000.0
        self.spy.record("RX", f"SIM-{self.profile.model}", f"<Binary Block: {len(vals)} points>", elapsed_ms=dt)
        return vals

    def device_clear(self) -> None:
        self.state.aborted = True
        self.state.output_enabled = False
        self.spy.record("SYS", f"SIM-{self.profile.model}", "VI_CLEAR (Simulated Immediate Emergency Stop)")

    def close(self) -> None:
        self.state.output_enabled = False
        self.spy.record("SYS", f"SIM-{self.profile.model}", "Virtual Session Closed")

    def simulate_sweep_execution(
        self,
        cfg: ChannelConfig,
        recipe: SweepRecipe,
        node_ids: list[int] | None = None,
    ) -> list[ReadingPoint]:
        """Generate physics-based reading buffer points for 1..N nodes."""
        nodes = node_ids or [cfg.logical_channel.node_id]
        levels = recipe.generate_levels()
        repeat_n = recipe.repeat_count if recipe.repeat_mode.value == "FINITE" else 1
        all_levels = levels * max(1, repeat_n)

        points: list[ReadingPoint] = []
        t_cursor = 0.0
        dt_step = max(recipe.delay_s, 1e-5)

        for idx, lv in enumerate(all_levels):
            if self.state.aborted:
                break
            t_cursor += dt_step
            for n_id in nodes:
                v_val, i_val, in_comp = VirtualSMUPhysicsEngine.evaluate_point(
                    source_func=recipe.source_func,
                    source_level=lv,
                    compliance_limit=recipe.compliance_limit,
                    t_step_s=t_cursor,
                    dut=self.state.dut_type,
                    noise_std=self.state.noise_level,
                )
                r_val = (v_val / i_val) if abs(i_val) > 1e-15 else float("inf")
                p_val = v_val * i_val

                # Limit test evaluation
                meas_val = i_val if recipe.measure_func.value == "CURRENT" else v_val
                passed = True
                if recipe.limit_low is not None and meas_val < recipe.limit_low:
                    passed = False
                if recipe.limit_high is not None and meas_val > recipe.limit_high:
                    passed = False

                status_bits = (1 if in_comp else 0) | (4 if cfg.sense_mode.value == "4WIRE" else 0)
                points.append(
                    ReadingPoint(
                        index=idx + 1,
                        timestamp_s=round(t_cursor, 6),
                        voltage_v=v_val,
                        current_a=i_val,
                        resistance_ohm=r_val,
                        power_w=p_val,
                        status_bits=status_bits,
                        in_compliance=in_comp,
                        is_overrange=False,
                        is_4wire=(cfg.sense_mode.value == "4WIRE"),
                        limit_passed=passed,
                        node_id=n_id,
                        channel=cfg.logical_channel.channel_name,
                    )
                )
                if in_comp and recipe.abort_on_limit:
                    self.state.aborted = True
                    break
        self.state.last_buffer = points
        self.state.output_enabled = False
        return points
