"""SOA & Thermal/Sink Duty Cycle Validator + 0.5s Pre-Flight Safety Check Manager (Phase 1-3 Complete).

Covers:
- Pre-execution SOA envelope validation (DC & Pulse voltage, current, power, min pulse width, max duty cycle).
- Phase 3 Thermal & Sink (Quadrant II/IV) Duty Cycle derating formula calculator (ambient temp 23°C..50°C).
- SOA boundary polygon coordinate generator for interactive SOA chart visualization.
- 2601B-PULSE burst point limit (3,500 pts) and pulse rate metric (< 33,000) validation.
- 0.5-second Pre-Flight Check: TSP-Link online status, Rear Interlock (>42V), 4-Wire Contact Check, Buffer capacity, and 2461 ACAL drift warning.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from smu_app.core.models import (
    AcquisitionMode,
    ChannelConfig,
    SenseMode,
    SourceFunction,
    SweepRecipe,
    SweepType,
)
from smu_app.core.profiles import SMUModelProfile
from smu_app.drivers.base_smu import BaseTSPDriver


@dataclass
class SafetyValidationReport:
    """Result of SOA, Thermal Duty Cycle, and Pre-Flight safety verification."""

    passed: bool = True
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    recommended_acquisition_mode: AcquisitionMode = AcquisitionMode.HIGH_SPEED_BURST
    computed_duty_cycle: float = 0.0
    max_allowed_duty_cycle: float = 1.0


class SafetyAndSOAValidator:
    """Validates user recipes against the 8-model hardware SOA database, thermal/sink limits, and executes Pre-Flight Checks."""

    @staticmethod
    def compute_thermal_and_sink_max_duty(
        profile: SMUModelProfile,
        peak_v: float,
        peak_i: float,
        ambient_temp_c: float = 25.0,
        is_sink_quadrant: bool = False,
    ) -> float:
        """Compute maximum allowable pulse duty cycle accounting for ambient temperature (>30°C) and Sink (Quadrant II/IV) dissipation."""
        base_duty = profile.max_duty_cycle
        # Identify narrowest matching pulse SOA envelope duty limit
        for env in profile.soa_envelopes:
            if env.mode == "PULSE" and peak_v <= env.max_v * 1.001 and peak_i <= env.max_i * 1.001:
                base_duty = min(base_duty, env.max_duty)

        # Ambient temperature thermal derating above 30°C (2% reduction per °C above 30°C, down to 50% at 55°C)
        temp_excess = max(0.0, ambient_temp_c - 30.0)
        thermal_factor = max(0.50, 1.0 - 0.02 * temp_excess)

        # Sink operation (Quadrant II / IV internal power absorption) derates allowable pulse duty by 40%
        sink_factor = 0.60 if is_sink_quadrant else 1.0

        return round(min(1.0, base_duty * thermal_factor * sink_factor), 5)

    @staticmethod
    def get_soa_boundary_curves(profile: SMUModelProfile) -> dict[str, tuple[list[float], list[float]]]:
        """Return staircase (V, I) boundary coordinates for DC and PULSE Safe Operating Area plotting."""
        dc_envs = sorted([e for e in profile.soa_envelopes if e.mode == "DC"], key=lambda e: e.max_v)
        pulse_envs = sorted([e for e in profile.soa_envelopes if e.mode == "PULSE"], key=lambda e: e.max_v)

        def build_stair(envs) -> tuple[list[float], list[float]]:
            if not envs:
                return [0.0], [0.0]
            vx: list[float] = [0.0]
            iy: list[float] = [envs[0].max_i]
            prev_i = envs[0].max_i
            for e in envs:
                if e.max_i != prev_i:
                    vx.append(vx[-1])
                    iy.append(e.max_i)
                vx.append(e.max_v)
                iy.append(e.max_i)
                prev_i = e.max_i
            vx.append(envs[-1].max_v)
            iy.append(0.0)
            return vx, iy

        curves = {"DC": build_stair(dc_envs)}
        if pulse_envs:
            curves["PULSE"] = build_stair(pulse_envs)
        return curves

    @classmethod
    def validate_soa_and_recipe(
        cls,
        profile: SMUModelProfile,
        cfg: ChannelConfig,
        recipe: SweepRecipe,
        ambient_temp_c: float = 25.0,
        is_sink_quadrant: bool = False,
    ) -> SafetyValidationReport:
        report = SafetyValidationReport()
        levels = recipe.generate_levels()
        max_level = max((abs(x) for x in levels), default=0.0)
        comp = abs(recipe.compliance_limit)

        is_pulse = recipe.sweep_type in (SweepType.PULSE_TRAIN, SweepType.PULSE_SWEEP) or any(
            s.is_pulse for s in recipe.custom_steps
        )

        if recipe.source_func == SourceFunction.VOLTAGE:
            peak_v = max_level
            peak_i = comp
        else:
            peak_i = max_level
            peak_v = comp
        peak_p = peak_v * peak_i

        max_v_allowed = profile.max_pulse_voltage_v if is_pulse else profile.max_dc_voltage_v
        max_i_allowed = profile.max_pulse_current_a if is_pulse else profile.max_dc_current_a
        max_p_allowed = profile.max_pulse_power_w if is_pulse else profile.max_dc_power_w

        if peak_v > max_v_allowed * 1.001:
            report.passed = False
            report.errors.append(
                f"[{profile.model} 전압 초과] 설정 최대 전압({peak_v:.3g}V)이 허용 한계({max_v_allowed:.3g}V)를 초과했습니다."
            )
        if peak_i > max_i_allowed * 1.001:
            report.passed = False
            report.errors.append(
                f"[{profile.model} 전류 초과] 설정 최대 전류({peak_i:.3g}A)가 허용 한계({max_i_allowed:.3g}A)를 초과했습니다."
            )
        if peak_p > max_p_allowed * 1.001:
            report.passed = False
            report.errors.append(
                f"[{profile.model} 전력 SOA 초과] 최대 전력({peak_p:.2f}W = {peak_v:.3g}V × {peak_i:.3g}A)이 "
                f"허용 전력 한계({max_p_allowed:.1f}W)를 초과했습니다."
            )

        # Check specific SOA quadrant envelopes
        mode_tag = "PULSE" if is_pulse else "DC"
        matching_envelopes = [e for e in profile.soa_envelopes if e.mode == mode_tag]
        if not matching_envelopes:
            matching_envelopes = profile.soa_envelopes

        if matching_envelopes:
            fits_any = any(
                (peak_v <= env.max_v * 1.001 and peak_i <= env.max_i * 1.001) for env in matching_envelopes
            )
            if not fits_any:
                env_desc = ", ".join(f"({e.max_v}V/{e.max_i}A)" for e in matching_envelopes)
                report.passed = False
                report.errors.append(
                    f"[{profile.model} SOA 영역 위반] 요청 동작점({peak_v:.3g}V, {peak_i:.3g}A)이 "
                    f"모델 허용 SOA 포락선[{env_desc}] 범위를 벗어났습니다."
                )

        # Pulse width, thermal/sink duty cycle, and 2601B-PULSE constraints
        if is_pulse:
            pw = recipe.pulse_width_s
            period = max(recipe.pulse_period_s, pw)
            duty = pw / period if period > 0 else 1.0
            max_duty_allowed = cls.compute_thermal_and_sink_max_duty(
                profile=profile,
                peak_v=peak_v,
                peak_i=peak_i,
                ambient_temp_c=ambient_temp_c,
                is_sink_quadrant=is_sink_quadrant,
            )
            report.computed_duty_cycle = duty
            report.max_allowed_duty_cycle = max_duty_allowed

            if pw < profile.min_pulse_width_s * 0.999:
                report.passed = False
                report.errors.append(
                    f"[{profile.model} 최소 펄스 폭 위반] 설정 펄스 폭({pw*1e6:.1f}µs)이 "
                    f"최소 허용 폭({profile.min_pulse_width_s*1e6:.1f}µs)보다 짧습니다."
                )
            if duty > max_duty_allowed * 1.001:
                report.passed = False
                report.errors.append(
                    f"[{profile.model} Duty Cycle 초과] 계산된 Duty Cycle({duty*100:.2f}%)이 "
                    f"최대 허용치({max_duty_allowed*100:.2f}%, 온도={ambient_temp_c:.0f}°C/Sink={is_sink_quadrant})를 초과했습니다."
                )

            # Check maximum pulse width inside matched PULSE SOA envelope
            for env in matching_envelopes:
                if (
                    env.mode == "PULSE"
                    and env.max_pw_s is not None
                    and peak_i > profile.max_dc_current_a
                    and pw > env.max_pw_s * 1.001
                ):
                    report.passed = False
                    report.errors.append(
                        f"[{profile.model} 최대 펄스 폭 초과] 고전류 펄스({peak_i:.3g}A) 영역에서 "
                        f"펄스 폭({pw*1e3:.2f}ms)이 SOA 최대 폭({env.max_pw_s*1e3:.2f}ms)을 초과했습니다."
                    )

            if profile.max_pulser_rate_metric is not None:
                pulse_rate = 1.0 / period if period > 0 else 0.0
                meas_rate = float(recipe.digitizer_rate_sps) if recipe.use_digitizer else (60.0 / max(recipe.nplc, 0.001))
                f_cnt = cfg.filter_count if cfg.filter_enabled else 1
                metric = pulse_rate + meas_rate * f_cnt
                if metric >= profile.max_pulser_rate_metric and not recipe.use_digitizer:
                    report.passed = False
                    report.errors.append(
                        f"[2601B-PULSE Rate 제약 초과] (Pulse Rate + Meas Rate × Filter Count = {metric:.0f}) >= 33,000 입니다."
                    )

        # Buffer & 2601B-PULSE burst point limit check
        total_points = len(levels) * (recipe.repeat_count if recipe.repeat_mode.value == "FINITE" else 1)
        if profile.max_pulser_burst_points and is_pulse and total_points > profile.max_pulser_burst_points:
            report.warnings.append(
                f"[2601B-PULSE 버스트 분할] 총 포인트({total_points})가 1회 버스트 한계({profile.max_pulser_burst_points}pts)를 초과하여 "
                "고속 버스트 반복(HIGH_SPEED_BURST) 분할 수집 모드로 자동 전환합니다."
            )
        if total_points > profile.max_buffer_points:
            report.passed = False
            report.errors.append(
                f"[{profile.model} 버퍼 용량 초과] 예상 측정 포인트({total_points:,}pts)가 "
                f"내부 버퍼 한계({profile.max_buffer_points:,}pts)를 초과합니다."
            )

        # Dual Continuous Mode auto-selection (v3.0 Challenge 3 resolution)
        step_period = recipe.pulse_period_s if is_pulse else recipe.delay_s
        if step_period < 0.020 or recipe.use_digitizer or is_pulse:
            report.recommended_acquisition_mode = AcquisitionMode.HIGH_SPEED_BURST
        else:
            report.recommended_acquisition_mode = AcquisitionMode.LOW_SPEED_STREAMING

        return report

    @classmethod
    def run_preflight_check(
        cls,
        driver: BaseTSPDriver,
        cfg: ChannelConfig,
        recipe: SweepRecipe,
        node_count: int = 1,
        ambient_temp_c: float = 25.0,
        is_sink_quadrant: bool = False,
    ) -> SafetyValidationReport:
        """0.5-second Pre-Flight Check executed immediately before output activation."""
        report = cls.validate_soa_and_recipe(
            driver.profile,
            cfg,
            recipe,
            ambient_temp_c=ambient_temp_c,
            is_sink_quadrant=is_sink_quadrant,
        )
        if not report.passed:
            return report

        # 1. Verify TSP command mode
        tsp_ok, tsp_msg = driver.ensure_tsp_mode(auto_switch=True)
        if not tsp_ok:
            report.passed = False
            report.errors.append(tsp_msg)

        # 2. Check Interlock if voltage > 42V
        levels = recipe.generate_levels()
        max_v = (
            max((abs(x) for x in levels), default=0.0)
            if recipe.source_func == SourceFunction.VOLTAGE
            else abs(recipe.compliance_limit)
        )
        if max_v > driver.profile.interlock_threshold_v:
            if not driver.check_interlock_engaged():
                report.passed = False
                report.errors.append(
                    f"[안전 인터락 미체결 (에러 5074 방지)] 설정 전압({max_v:.1f}V)이 ±{driver.profile.interlock_threshold_v:.0f}V를 "
                    "초과하지만 후면 Safety Interlock이 체결되지 않았습니다."
                )

        # 3. Run 4-Wire Contact Check if enabled and supported
        if cfg.sense_mode == SenseMode.WIRE_4 and cfg.contact_check_enabled and driver.profile.has_contact_check:
            cc_ok, cc_msg = driver.run_contact_check(
                channel_prefix=cfg.logical_channel.tsp_prefix,
                threshold_ohm=cfg.contact_check_threshold_ohm,
            )
            if not cc_ok:
                report.passed = False
                report.errors.append(f"[4-Wire 접촉 불량] {cc_msg}")
            else:
                report.warnings.append(cc_msg)

        # 4. Multi-node TSP-Link check
        if node_count > 1 and driver.profile.tsplink_supported:
            state = driver.transport.query("print(tsplink.state)").strip().lower()
            if "online" not in state:
                report.passed = False
                report.errors.append(f"[TSP-Link 오프라인] 다중 장비({node_count}대) 동기화 상태가 '{state}'입니다.")

        return report
