"""Phase 3 High-Speed Pulse & 1 MS/s Digitizer Engine (2461, 2601B-PULSE, 2651A, 2600B).

Implements v3.0 Section 05-2 (High-Speed Pulse & Digitizing) & Section 06-B (ACAL & Precision):
1. 2461 High-Current Pulse (10A/100V, min width 150us) + 1 MS/s Digitizer (`smu.digitize.func`, `smu.source.pulsetrain` / `pulsesweeplinear`)
   + Autocalibration (`ACAL`) temperature drift (+/-5°C) monitoring and remote `smu.cal.avan()` / `acal.run()` execution.
2. 2601B-PULSE High-Speed Pulser (10A/10V, min width 10us) + Dual 1 MS/s 18-bit Digitizer (`smua.pulser.*`, simultaneous V/I capture,
   automatic 3,500-point burst chunking, and 0..3uH load inductance compensation).
3. 2651A High-Power Pulse (50A/40V, min width 300us, 2000W) + Fast ADC (1us sampling `smua.measure.adc = smua.ADC_FAST`).
4. 2600B Dual Trigger-Timer Pulse (Period Timer 1 + Pulse Width Timer 2).
5. Realistic physics-based microsecond pulse transient waveform generator (rise/fall slew, RLC ringing overshoot, settling, and pulse top).
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
import random

from smu_app.core.models import (
    ChannelConfig,
    ReadingPoint,
    SourceFunction,
    SweepRecipe,
    SweepType,
)
from smu_app.core.profiles import SMUModelProfile


@dataclass
class PulseDigitizerConfig:
    """Specialized Phase 3 parameters for high-speed pulsing and 1 MS/s digitizing."""

    bias_level: float = 0.0
    pulse_level: float = 5.0
    pulse_width_s: float = 0.0002        # 200 us default
    pulse_period_s: float = 0.0040       # 4 ms default (5% duty)
    pulse_count: int = 1
    digitize_enable: bool = True
    sample_rate_sps: int = 1000000       # 1 MS/s
    aperture_s: float = 1e-6             # 1 us aperture
    pre_trigger_samples: int = 20
    post_trigger_samples: int = 40
    load_inductance_uh: float = 0.5      # 2601B-PULSE 0..3 uH inductive load compensation
    fast_adc_mode: bool = True           # 2651A Fast ADC vs Integrating ADC
    sink_mode_enable: bool = False       # 2651A dedicated Sink Mode
    ambient_temp_c: float = 25.0         # For thermal derating & 2461 ACAL drift check
    last_acal_temp_c: float = 23.0       # 2461 temperature at last Autocalibration


@dataclass
class ACALStatus:
    """Keithley 2461 Autocalibration (ACAL) monitoring & execution summary."""

    supported: bool
    current_temp_c: float
    last_acal_temp_c: float
    temp_delta_c: float
    acal_recommended: bool
    message: str
    executed_commands: list[str] = field(default_factory=list)


class HighSpeedPulseDigitizerEngine:
    """Compiles model-specific high-speed pulse & digitizer TSP scripts and simulates 1 MS/s transient captures."""

    MAX_2601B_BURST_PTS = 3500

    @classmethod
    def compute_burst_plan(
        cls,
        profile: SMUModelProfile,
        p_cfg: PulseDigitizerConfig,
    ) -> tuple[int, int, int]:
        """Compute (samples_per_pulse, pulses_per_burst, num_bursts) respecting 2601B-PULSE 3,500-pt burst limit."""
        if p_cfg.digitize_enable and profile.has_digitizer:
            pulse_samples = max(10, int(round(p_cfg.pulse_width_s * p_cfg.sample_rate_sps)))
            samples_per_pulse = pulse_samples + p_cfg.pre_trigger_samples + p_cfg.post_trigger_samples
        else:
            samples_per_pulse = 1  # Pulse-top single integrated measurement

        total_pulses = max(1, p_cfg.pulse_count)
        if profile.max_pulser_burst_points and p_cfg.digitize_enable:
            max_burst = profile.max_pulser_burst_points
            pulses_per_burst = max(1, max_burst // max(1, samples_per_pulse))
            num_bursts = math.ceil(total_pulses / pulses_per_burst)
        else:
            pulses_per_burst = total_pulses
            num_bursts = 1
        return samples_per_pulse, pulses_per_burst, num_bursts

    @classmethod
    def compile_pulse_digitizer_tsp(
        cls,
        profile: SMUModelProfile,
        cfg: ChannelConfig,
        recipe: SweepRecipe,
        p_cfg: PulseDigitizerConfig,
    ) -> list[str]:
        """Generate model-specific high-speed pulse and digitizer TSP command sequences."""
        pfx = cfg.logical_channel.tsp_prefix
        samples_per_pulse, pulses_per_burst, num_bursts = cls.compute_burst_plan(profile, p_cfg)

        # 1. Keithley 2461: 10A/100V Pulse + 1 MS/s Digitizer + TriggerFlow
        if profile.model == "2461":
            s_func = "smu.FUNC_DC_CURRENT" if recipe.source_func == SourceFunction.CURRENT else "smu.FUNC_DC_VOLTAGE"
            d_func = "smu.FUNC_DIGITIZE_CURRENT" if recipe.source_func == SourceFunction.VOLTAGE else "smu.FUNC_DIGITIZE_VOLTAGE"
            off_time = max(0.00015, p_cfg.pulse_period_s - p_cfg.pulse_width_s)
            cmds = [
                f"-- 2461 High-Current Pulse ({p_cfg.pulse_level} V/A, width={p_cfg.pulse_width_s*1e6:.0f}us) & 1 MS/s Digitizer",
                f"{pfx}.reset()",
                "defbuffer1.clear()",
                f"{pfx}.source.func = {s_func}",
            ]
            if p_cfg.digitize_enable:
                cmds.extend([
                    f"{pfx}.digitize.func = {d_func}",
                    f"{pfx}.digitize.samplerate = {min(profile.max_digitizer_rate_sps, p_cfg.sample_rate_sps)}",
                    f"{pfx}.digitize.count = {samples_per_pulse}",
                ])
            if recipe.sweep_type == SweepType.PULSE_SWEEP:
                s_tag = "i" if recipe.source_func == SourceFunction.CURRENT else "v"
                cmds.append(
                    f"{pfx}.source.pulsesweep{s_tag}linear("
                    f"{p_cfg.bias_level:.6g}, {recipe.start_level:.6g}, {recipe.stop_level:.6g}, "
                    f"{max(2, recipe.points)}, {p_cfg.pulse_width_s:.6g}, smu.ON, defbuffer1, 0, {off_time:.6g}, "
                    f"{recipe.compliance_limit:.6g}, {recipe.compliance_limit:.6g})"
                )
            else:
                s_tag = "i" if recipe.source_func == SourceFunction.CURRENT else "v"
                cmds.append(
                    f"{pfx}.source.pulsetrain{s_tag}("
                    f"{p_cfg.bias_level:.6g}, {p_cfg.pulse_level:.6g}, {p_cfg.pulse_width_s:.6g}, "
                    f"{max(1, p_cfg.pulse_count)}, smu.ON, defbuffer1, 0, {off_time:.6g}, "
                    f"{recipe.compliance_limit:.6g}, {recipe.compliance_limit:.6g})"
                )
            cmds.append("trigger.model.initiate()")
            return cmds

        # 2. Keithley 2601B-PULSE: 10A/10V 10us Pulser + Dual 1 MS/s 18-bit Digitizer + 3,500-pt Burst Management
        if profile.model == "2601B-PULSE":
            bias_v = -1e-12 if (p_cfg.bias_level == 0.0 and p_cfg.pulse_level < 0) else p_cfg.bias_level
            burst_pts = min(cls.MAX_2601B_BURST_PTS, samples_per_pulse * pulses_per_burst)
            return [
                f"-- 2601B-PULSE Dedicated 10us Pulser + Dual 1 MS/s 18-bit Digitizer (Bursts={num_bursts}, Pts/Burst={burst_pts})",
                f"{pfx}.reset()",
                f"{pfx}.nvbuffer1.clear(); {pfx}.nvbuffer2.clear()",
                f"{pfx}.pulser.enable = {pfx}.ENABLE",
                f"{pfx}.measure.autozero = {pfx}.AUTOZERO_OFF",
                f"{pfx}.source.highc = {pfx}.DISABLE",
                f"{pfx}.pulser.inductance = {max(0.0, min(3.0, p_cfg.load_inductance_uh)) * 1e-6:.6g}",
                f"{pfx}.pulser.bias.leveli = {bias_v:.6g}",
                f"{pfx}.pulser.pulse.leveli = {p_cfg.pulse_level:.6g}",
                f"{pfx}.pulser.pulse.width = {max(1e-5, p_cfg.pulse_width_s):.6g}",
                f"{pfx}.pulser.pulse.period = {max(p_cfg.pulse_width_s * 2, p_cfg.pulse_period_s):.6g}",
                f"{pfx}.pulser.protect.levelv = {min(10.0, recipe.compliance_limit):.6g}",
                f"{pfx}.pulser.measure.aperture = {max(1e-6, p_cfg.aperture_s):.6g}",
                f"{pfx}.pulser.measure.count = {burst_pts}",
                f"{pfx}.pulser.measure.digiv({pfx}.nvbuffer1, {pfx}.nvbuffer2)",
                f"{pfx}.pulser.initiate()",
            ]

        # 3. Keithley 2651A: 50A/40V High-Power Pulse + Fast ADC (1us sampling) + Dedicated Sink Mode
        if profile.model == "2651A":
            bias_val = -1e-12 if (p_cfg.bias_level == 0.0 and p_cfg.pulse_level < 0) else p_cfg.bias_level
            adc_const = f"{pfx}.ADC_FAST" if p_cfg.fast_adc_mode else f"{pfx}.ADC_INTEGRATE"
            sink_const = f"{pfx}.ENABLE" if p_cfg.sink_mode_enable else f"{pfx}.DISABLE"
            return [
                f"-- 2651A 50A/2000W High-Power Pulse + Fast ADC (1us interval, Sink={p_cfg.sink_mode_enable})",
                f"{pfx}.reset()",
                f"{pfx}.nvbuffer1.clear(); {pfx}.nvbuffer2.clear()",
                f"{pfx}.source.sink = {sink_const}",
                f"{pfx}.measure.adc = {adc_const}",
                f"{pfx}.measure.interval = {max(1e-6, p_cfg.aperture_s):.6g}",
                f"{pfx}.measure.count = {samples_per_pulse}",
                f"trigger.timer[1].delay = {max(p_cfg.pulse_period_s, 0.001):.6g}",
                f"trigger.timer[1].count = {max(1, p_cfg.pulse_count)}",
                f"trigger.timer[2].delay = {max(0.0003, p_cfg.pulse_width_s):.6g}",
                f"trigger.timer[2].count = 1",
                f"trigger.timer[2].stimulus = trigger.timer[1].EVENT_ID",
                f"{pfx}.source.leveli = {bias_val:.6g}",
                f"{pfx}.trigger.source.listi({{{p_cfg.pulse_level:.6g}}})",
                f"{pfx}.trigger.source.limiti = {recipe.compliance_limit:.6g}",
                f"{pfx}.trigger.measure.iv({pfx}.nvbuffer1, {pfx}.nvbuffer2)",
                f"{pfx}.trigger.endpulse.action = {pfx}.SOURCE_IDLE",
                f"{pfx}.trigger.endpulse.stimulus = trigger.timer[2].EVENT_ID",
                f"{pfx}.source.output = {pfx}.OUTPUT_ON",
                f"{pfx}.trigger.initiate()",
            ]

        # 4. Keithley 2600B / 2606B / 2450 / 2460 / 2470 Dual-Timer Pulse fallback
        if profile.is_26xx:
            bias_val = -1e-12 if (p_cfg.bias_level == 0.0 and p_cfg.pulse_level < 0) else p_cfg.bias_level
            return [
                f"-- {profile.model} Dual Trigger-Timer Pulse Train (Timer 1: Period, Timer 2: Pulse Width)",
                f"{pfx}.reset()",
                f"{pfx}.nvbuffer1.clear()",
                f"trigger.timer[1].delay = {p_cfg.pulse_period_s:.6g}",
                f"trigger.timer[1].count = {max(1, p_cfg.pulse_count)}",
                f"trigger.timer[2].delay = {p_cfg.pulse_width_s:.6g}",
                f"trigger.timer[2].count = 1",
                f"trigger.timer[2].stimulus = trigger.timer[1].EVENT_ID",
                f"{pfx}.source.levelv = {bias_val:.6g}",
                f"{pfx}.trigger.source.listv({{{p_cfg.pulse_level:.6g}}})",
                f"{pfx}.trigger.endpulse.action = {pfx}.SOURCE_IDLE",
                f"{pfx}.trigger.endpulse.stimulus = trigger.timer[2].EVENT_ID",
                f"{pfx}.source.output = {pfx}.OUTPUT_ON",
                f"{pfx}.trigger.initiate()",
            ]
        return [
            f"-- {profile.model} TriggerFlow Pulse Sequence",
            f"{pfx}.reset()",
            "defbuffer1.clear()",
            f"{pfx}.source.level = {p_cfg.pulse_level:.6g}",
            "trigger.model.initiate()",
        ]

    @staticmethod
    def check_and_run_2461_acal(
        profile: SMUModelProfile,
        p_cfg: PulseDigitizerConfig,
        execute_now: bool = False,
    ) -> ACALStatus:
        """Monitor 2461 internal temperature drift (+/-5°C threshold) and optionally generate remote ACAL commands."""
        delta = abs(p_cfg.ambient_temp_c - p_cfg.last_acal_temp_c)
        if not profile.has_acal:
            return ACALStatus(
                supported=False,
                current_temp_c=p_cfg.ambient_temp_c,
                last_acal_temp_c=p_cfg.last_acal_temp_c,
                temp_delta_c=delta,
                acal_recommended=False,
                message=f"{profile.model} 모델은 내장 Autocalibration(ACAL) 대상이 아닙니다 (2461 전용 기능).",
            )

        recommended = delta >= 5.0
        cmds: list[str] = []
        if execute_now:
            cmds = [
                "smu.source.output = smu.OFF",
                "acal.run()",
                "print(acal.count, acal.timestamp)",
            ]
            msg = (
                f"[2461 ACAL 원격 교정 완료] 온도 편차 ΔT={delta:.1f}°C "
                f"({p_cfg.last_acal_temp_c:.1f}°C → {p_cfg.ambient_temp_c:.1f}°C) 보정을 위해 acal.run()을 실행했습니다."
            )
            return ACALStatus(
                supported=True,
                current_temp_c=p_cfg.ambient_temp_c,
                last_acal_temp_c=p_cfg.ambient_temp_c,
                temp_delta_c=0.0,
                acal_recommended=False,
                message=msg,
                executed_commands=cmds,
            )

        if recommended:
            msg = (
                f"⚠ [2461 ACAL 실행 권장] 마지막 교정 이후 온도 변화(ΔT={delta:.1f}°C ≥ ±5.0°C)가 감지되었습니다. "
                "고속 펄스/디지타이저 정밀도 유지를 위해 원격 ACAL을 실행하세요."
            )
        else:
            msg = f"[2461 ACAL 상태 양호] 온도 편차 ΔT={delta:.1f}°C (< ±5.0°C 허용 범위 내)."

        return ACALStatus(
            supported=True,
            current_temp_c=p_cfg.ambient_temp_c,
            last_acal_temp_c=p_cfg.last_acal_temp_c,
            temp_delta_c=delta,
            acal_recommended=recommended,
            message=msg,
            executed_commands=cmds,
        )

    @classmethod
    def simulate_digitized_pulse_waveform(
        cls,
        profile: SMUModelProfile,
        cfg: ChannelConfig,
        recipe: SweepRecipe,
        p_cfg: PulseDigitizerConfig,
        r_load_ohm: float = 2.0,
    ) -> list[ReadingPoint]:
        """Generate realistic 1 MS/s (1us resolution) digitized V & I pulse transient waveforms.

        Simulates:
        - Pre-trigger bias baseline
        - Exponential rise edge (tau_rise depends on model pulser bandwidth & load inductance)
        - Damped RLC underdamped ringing overshoot at leading edge
        - Stable Pulse Top plateau
        - Exponential falling edge back to bias level
        """
        samples_per_pulse, _, _ = cls.compute_burst_plan(profile, p_cfg)
        dt = 1.0 / max(1000, p_cfg.sample_rate_sps)
        tau_rise = max(dt * 1.2, p_cfg.pulse_width_s * 0.03)
        omega_ring = 2.0 * math.pi / max(dt * 6.0, p_cfg.pulse_width_s * 0.12)
        damping = 1.6 / max(dt * 8.0, p_cfg.pulse_width_s * 0.25)
        overshoot_amp = 0.14 + 0.05 * min(3.0, p_cfg.load_inductance_uh)

        pulse_levels: list[float]
        if recipe.sweep_type == SweepType.PULSE_SWEEP:
            pulse_levels = recipe.generate_levels()
        else:
            pulse_levels = [p_cfg.pulse_level] * max(1, p_cfg.pulse_count)

        points: list[ReadingPoint] = []
        rng = random.Random(0x24612651)
        idx_counter = 1
        t_global = 0.0

        pre_n = p_cfg.pre_trigger_samples if p_cfg.digitize_enable else 0
        post_n = p_cfg.post_trigger_samples if p_cfg.digitize_enable else 0
        top_n = max(1, samples_per_pulse - pre_n - post_n)

        for p_idx, target_lvl in enumerate(pulse_levels):
            bias = p_cfg.bias_level
            span = target_lvl - bias

            if not p_cfg.digitize_enable:
                # Single integrated measurement at top of pulse
                t_global += p_cfg.pulse_period_s
                if recipe.source_func == SourceFunction.CURRENT:
                    i_val = target_lvl
                    v_val = min(recipe.compliance_limit, i_val * r_load_ohm)
                else:
                    v_val = target_lvl
                    i_val = min(recipe.compliance_limit, v_val / r_load_ohm)
                in_comp = abs(i_val if recipe.source_func == SourceFunction.VOLTAGE else v_val) >= abs(recipe.compliance_limit) * 0.999
                points.append(
                    ReadingPoint(
                        index=idx_counter,
                        timestamp_s=round(t_global, 7),
                        voltage_v=float(v_val),
                        current_a=float(i_val),
                        resistance_ohm=float(v_val / i_val) if abs(i_val) > 1e-12 else float("inf"),
                        power_w=float(v_val * i_val),
                        in_compliance=in_comp,
                        is_4wire=(cfg.sense_mode.value == "4WIRE"),
                        node_id=cfg.logical_channel.node_id,
                        channel=cfg.logical_channel.channel_name,
                    )
                )
                idx_counter += 1
                continue

            # High-speed 1 MS/s digitized waveform across Pre-Trigger, Pulse Width, and Post-Trigger
            for s_i in range(samples_per_pulse):
                t_local = s_i * dt
                t_on = (s_i - pre_n) * dt
                t_off = (s_i - (pre_n + top_n)) * dt
                noise = rng.gauss(0.0, 1e-4 * max(abs(target_lvl), 0.1))

                if s_i < pre_n:
                    instant_lvl = bias + noise
                elif s_i < pre_n + top_n:
                    rise_factor = 1.0 - math.exp(-t_on / tau_rise)
                    ring_factor = overshoot_amp * math.exp(-damping * t_on) * math.sin(omega_ring * t_on)
                    instant_lvl = bias + span * (rise_factor + ring_factor) + noise
                else:
                    fall_factor = math.exp(-max(0.0, t_off) / tau_rise)
                    instant_lvl = bias + span * fall_factor + noise

                if recipe.source_func == SourceFunction.CURRENT:
                    i_val = instant_lvl
                    v_ideal = i_val * r_load_ohm
                    in_comp = abs(v_ideal) >= abs(recipe.compliance_limit)
                    v_val = math.copysign(abs(recipe.compliance_limit), v_ideal) if in_comp else v_ideal
                else:
                    v_val = instant_lvl
                    i_ideal = v_val / r_load_ohm
                    in_comp = abs(i_ideal) >= abs(recipe.compliance_limit)
                    i_val = math.copysign(abs(recipe.compliance_limit), i_ideal) if in_comp else i_ideal

                r_val = (v_val / i_val) if abs(i_val) > 1e-12 else float("inf")
                points.append(
                    ReadingPoint(
                        index=idx_counter,
                        timestamp_s=round(t_global + t_local, 7),
                        voltage_v=float(v_val),
                        current_a=float(i_val),
                        resistance_ohm=float(r_val),
                        power_w=float(v_val * i_val),
                        in_compliance=in_comp,
                        is_4wire=(cfg.sense_mode.value == "4WIRE"),
                        node_id=cfg.logical_channel.node_id,
                        channel=cfg.logical_channel.channel_name,
                    )
                )
                idx_counter += 1

            t_global += max(p_cfg.pulse_period_s, samples_per_pulse * dt)

        return points
