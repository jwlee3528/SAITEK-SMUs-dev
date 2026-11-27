"""Rule-Based 'Auto-Measurement Coach' Engine (15-Item Diagnostic Matrix with Model-Aware Adaptive Thresholds).

Focuses strictly on measurement setup, wiring, noise, and timing quality (NOT DUT physical failure guessing),
filtering 70~80% of beginner field setup errors automatically and proposing 1-click Auto-Fix actions.

4 Categories / 15 Rules:
1. 출력 및 리미트 진단 (4): Compliance 도달, 소스 리드백 오차(>1%), 전면/후면 단자 불일치, 인터락 차단
2. 배선 및 접촉 상태 진단 (4): 단선(Open), 단락(Short), 2-Wire vs 4-Wire 오설정, 극성 반전
3. 노이즈 및 측정 정밀도 진단 (4): 60Hz 전원 노이즈, 오토레인지 스파이크, 레인지 부적합(10배 과대), 장시간 드리프트
4. 타이밍 및 동적 특성 진단 (3): 안정화(Settling) 시간 부족, 용량성 부하 발진, 샘플링 속도 병목
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

import numpy as np

from smu_app.core.models import (
    DiagnosisItem,
    DiagnosisReport,
    DiagnosisSeverity,
    MeasurementResult,
    SenseMode,
    SourceFunction,
    SweepType,
    TerminalLocation,
)
from smu_app.core.profiles import SMUModelProfile


@dataclass(frozen=True)
class ModelDiagnosticThresholds:
    """Model-specific diagnostic thresholds adapted automatically from each SMU's hardware specification."""

    model: str
    readback_error_pct: float        # Source Readback allowable error (%)
    readback_min_eval_v: float       # Ignore near-zero points below this level to avoid false positives
    open_min_voltage_v: float        # Minimum applied voltage to judge Open circuit
    open_noise_floor_a: float        # Current below this at >= open_min_voltage_v indicates Open
    short_max_voltage_v: float       # Low-voltage threshold for immediate Short judgment
    short_max_resistance_ohm: float  # Resistance below this indicates Short
    low_r_4wire_threshold_ohm: float # Recommend 4-Wire Kelvin below this resistance
    interlock_voltage_v: float       # Safety interlock threshold from SMU profile
    range_oversize_ratio: float      # Fixed range > max_meas * ratio indicates resolution loss
    noise_cv_threshold: float        # Relative StdDev / fluctuation threshold when NPLC < 1
    drift_min_duration_s: float      # Duration threshold for AutoZero=OFF long-term drift warning
    settling_hyst_pct: float         # Dual-sweep forward vs reverse hysteresis threshold (%)
    oscillation_sign_flip_ratio: float
    bottleneck_delay_ratio: float    # Actual interval / configured delay ratio
    bottleneck_extra_ms: float       # Extra ms over configured delay

    def to_summary_dict(self) -> dict[str, str]:
        return {
            "적용 SMU 모델": self.model,
            "Readback 허용 오차": f"{self.readback_error_pct:g}% (|설정| ≥ {self.readback_min_eval_v*1e3:g}mV)",
            "단선(Open) 노이즈 기준": f"|I| < {self.open_noise_floor_a:.0e} A (V ≥ {self.open_min_voltage_v:g}V)",
            "단락(Short) 저항 기준": f"R < {self.short_max_resistance_ohm:g} Ω (또는 V ≤ {self.short_max_voltage_v:g}V 리미트)",
            "4-Wire 권장 저저항": f"R ≤ {self.low_r_4wire_threshold_ohm:g} Ω",
            "고전압 인터락 기준": f"±{self.interlock_voltage_v:g} V",
            "레인지 과대 배수": f"설정 레인지 ≥ 최대 측정값 × {self.range_oversize_ratio:g}",
        }


def get_thresholds_for_profile(profile: SMUModelProfile) -> ModelDiagnosticThresholds:
    """Return hardware-calibrated diagnostic thresholds tailored to the selected SMU model."""
    m = profile.model.upper()
    # High-power 50A SMU (2651A) has higher current noise floor & lower short-R boundary
    if m == "2651A":
        return ModelDiagnosticThresholds(
            model=profile.model,
            readback_error_pct=1.0,
            readback_min_eval_v=0.05,
            open_min_voltage_v=0.5,
            open_noise_floor_a=1e-6,
            short_max_voltage_v=0.05,
            short_max_resistance_ohm=0.1,
            low_r_4wire_threshold_ohm=10.0,
            interlock_voltage_v=profile.interlock_threshold_v,
            range_oversize_ratio=10.0,
            noise_cv_threshold=0.15,
            drift_min_duration_s=600.0,
            settling_hyst_pct=15.0,
            oscillation_sign_flip_ratio=0.55,
            bottleneck_delay_ratio=1.5,
            bottleneck_extra_ms=25.0,
        )
    # High-current 7A / 10A SMUs (2460, 2461, 2601B-PULSE)
    if m in ("2460", "2461", "2601B-PULSE"):
        return ModelDiagnosticThresholds(
            model=profile.model,
            readback_error_pct=1.0,
            readback_min_eval_v=0.02,
            open_min_voltage_v=0.5,
            open_noise_floor_a=1e-7,
            short_max_voltage_v=0.05,
            short_max_resistance_ohm=0.2,
            low_r_4wire_threshold_ohm=10.0,
            interlock_voltage_v=profile.interlock_threshold_v,
            range_oversize_ratio=10.0,
            noise_cv_threshold=0.15,
            drift_min_duration_s=600.0,
            settling_hyst_pct=15.0,
            oscillation_sign_flip_ratio=0.55,
            bottleneck_delay_ratio=1.5,
            bottleneck_extra_ms=25.0,
        )
    # High-voltage 1100V SMU (2470)
    if m == "2470":
        return ModelDiagnosticThresholds(
            model=profile.model,
            readback_error_pct=1.0,
            readback_min_eval_v=0.05,
            open_min_voltage_v=1.0,
            open_noise_floor_a=1e-8,
            short_max_voltage_v=0.1,
            short_max_resistance_ohm=1.0,
            low_r_4wire_threshold_ohm=10.0,
            interlock_voltage_v=profile.interlock_threshold_v,
            range_oversize_ratio=10.0,
            noise_cv_threshold=0.15,
            drift_min_duration_s=600.0,
            settling_hyst_pct=15.0,
            oscillation_sign_flip_ratio=0.55,
            bottleneck_delay_ratio=1.5,
            bottleneck_extra_ms=25.0,
        )
    # General precision SMUs (2450, 2600B, 2606B)
    return ModelDiagnosticThresholds(
        model=profile.model,
        readback_error_pct=1.0,
        readback_min_eval_v=0.01,
        open_min_voltage_v=0.5,
        open_noise_floor_a=1e-8,
        short_max_voltage_v=0.05,
        short_max_resistance_ohm=0.5,
        low_r_4wire_threshold_ohm=10.0,
        interlock_voltage_v=profile.interlock_threshold_v,
        range_oversize_ratio=10.0,
        noise_cv_threshold=0.15,
        drift_min_duration_s=600.0,
        settling_hyst_pct=15.0,
        oscillation_sign_flip_ratio=0.55,
        bottleneck_delay_ratio=1.5,
        bottleneck_extra_ms=25.0,
    )


def _pick_next_decade_range(max_val: float) -> float:
    """Pick a clean decade fixed range just above max_val (e.g. 0.0023 -> 0.01)."""
    if max_val <= 1e-9:
        return 1e-6
    exp = math.ceil(math.log10(max_val * 1.25))
    return float(10.0 ** max(-6, min(2, exp)))


class AutoDiagnosisEngine:
    """15-Rule Setup & Data Quality Auto-Diagnosis Coach for Tektronix/Keithley SMUs."""

    @classmethod
    def diagnose(
        cls,
        result: MeasurementResult,
        profile: SMUModelProfile,
        *,
        is_sink_mode: bool = False,
        interlock_engaged: bool = True,
        recent_error_messages: list[str] | None = None,
    ) -> DiagnosisReport:
        th = get_thresholds_for_profile(profile)
        pts = result.points
        cc = result.channel_config
        rc = result.recipe
        err_texts = " ".join(recent_error_messages or []) + " " + " ".join(result.warnings or [])

        items: list[DiagnosisItem] = []

        if not pts:
            return DiagnosisReport(
                model=profile.model,
                total_rules_checked=15,
                items=items,
                thresholds_summary=th.to_summary_dict(),
            )

        v_arr = np.array([p.voltage_v for p in pts], dtype=np.float64)
        i_arr = np.array([p.current_a for p in pts], dtype=np.float64)
        t_arr = np.array([p.timestamp_s for p in pts], dtype=np.float64)
        is_v_source = rc.source_func == SourceFunction.VOLTAGE
        src_arr = v_arr if is_v_source else i_arr
        meas_arr = i_arr if is_v_source else v_arr
        max_abs_v = float(np.max(np.abs(v_arr)))
        max_abs_i = float(np.max(np.abs(i_arr)))
        max_abs_meas = float(np.max(np.abs(meas_arr)))
        comp_hits = sum(1 for p in pts if p.in_compliance)

        # =====================================================================
        # CATEGORY 1: 출력 및 리미트 진단 (4개)
        # =====================================================================

        # 1-1. Compliance 도달
        flat_clip = False
        if len(meas_arr) >= 4 and rc.compliance_limit > 0:
            near_lim = int(np.sum(np.abs(meas_arr) >= rc.compliance_limit * 0.985))
            if near_lim >= 2:
                flat_clip = True
        if comp_hits > 0 or flat_clip:
            rec_lim = min(
                profile.max_dc_current_a if is_v_source else profile.max_dc_voltage_v,
                round(rc.compliance_limit * 2.0, 6),
            )
            unit = "A" if is_v_source else "V"
            items.append(DiagnosisItem(
                rule_id="COMPLIANCE_TRIP",
                category="1. 출력/리미트",
                title="컴플라이언스(Compliance) 리미트 도달 감지",
                severity=DiagnosisSeverity.CRITICAL,
                difficulty="하",
                evidence=f"Compliance 도달 {max(comp_hits, 2 if flat_clip else 0)}회 / 설정 Limit={rc.compliance_limit:g}{unit}",
                cause="출력 전류 또는 전압이 설정된 보호 한계(Limit)에 걸려 그래프가 평평하게 잘렸습니다(Clipping).",
                recommendation=f"시료 손상 위험이 없는지 확인 후 Compliance Limit을 상향 조정(예: {rec_lim:g}{unit})하세요.",
                fix_action_id="RAISE_COMPLIANCE",
                fix_label=f"Compliance {rec_lim:g}{unit}로 상향",
                fix_params={"compliance_limit": rec_lim},
            ))
        else:
            items.append(DiagnosisItem(
                rule_id="COMPLIANCE_TRIP",
                category="1. 출력/리미트",
                title="컴플라이언스(Compliance) 상태",
                severity=DiagnosisSeverity.OK,
                difficulty="하",
                evidence="Compliance 도달 0회",
                cause="정상 범위 내 출력",
                recommendation="조치 불필요",
            ))

        # 1-2. 소스 리드백 오차 (>1%)
        rb_errs: list[float] = []
        if cc.source_readback:
            for p in pts:
                if p.commanded_level is not None and abs(p.commanded_level) >= th.readback_min_eval_v:
                    actual_src = p.voltage_v if is_v_source else p.current_a
                    pct = abs(actual_src - p.commanded_level) / abs(p.commanded_level) * 100.0
                    if pct > th.readback_error_pct:
                        rb_errs.append(pct)
        if rb_errs:
            max_err = max(rb_errs)
            items.append(DiagnosisItem(
                rule_id="READBACK_ERROR",
                category="1. 출력/리미트",
                title="소스 리드백(Source Readback) 출력 오차 초과",
                severity=DiagnosisSeverity.WARNING,
                difficulty="하",
                evidence=f"오차 {th.readback_error_pct:g}% 초과 포인트 {len(rb_errs)}개 (최대 오차 {max_err:.2f}%)",
                cause="설정한 소스 목표값과 실제 출력 측정값 사이에 1% 이상의 차이가 발생했습니다 (과부하, 리미트 도달 또는 케이블 전압 강하).",
                recommendation="부하 상태 및 Compliance 설정을 확인하고, 대전류/저저항 측정이라면 4-Wire(Kelvin) 배선으로 전환하세요.",
                fix_action_id="ENABLE_4WIRE",
                fix_label="4-Wire(Kelvin) 배선 전환",
                fix_params={"sense_mode": "4WIRE"},
            ))
        else:
            rb_ev = "Readback ON — 설정값 대비 오차 1% 이내 정상" if cc.source_readback else "Readback OFF (속도 우선 모드)"
            items.append(DiagnosisItem(
                rule_id="READBACK_ERROR",
                category="1. 출력/리미트",
                title="소스 리드백(Source Readback) 오차",
                severity=DiagnosisSeverity.OK,
                difficulty="하",
                evidence=rb_ev,
                cause="정상",
                recommendation="정밀 검증이 필요할 때만 Source Readback을 ON 하세요.",
            ))

        # Helper: Open circuit signature
        is_open_sig = is_v_source and max_abs_v >= th.open_min_voltage_v and max_abs_i < th.open_noise_floor_a

        # 1-3. 전면/후면 단자 불일치
        if is_open_sig and cc.terminals == TerminalLocation.REAR:
            items.append(DiagnosisItem(
                rule_id="TERMINAL_MISMATCH",
                category="1. 출력/리미트",
                title="전면(Front) / 후면(Rear) 출력 단자 불일치 의심",
                severity=DiagnosisSeverity.CRITICAL,
                difficulty="하",
                evidence=f"현재 단자={cc.terminals.value}, 최대 인가={max_abs_v:.3g}V이나 전류가 {max_abs_i:.2e}A(Open)로 고착",
                cause="장비가 후면(REAR) 단자로 설정되어 있으나 실제 케이블은 전면(FRONT) 바나나잭에 연결되었을 가능성이 높습니다.",
                recommendation="케이블 연결 위치를 확인하고 전면 단자 사용 시 출력 단자를 FRONT로 전환하세요.",
                fix_action_id="SET_TERMINALS",
                fix_label="출력 단자 FRONT로 전환",
                fix_params={"terminals": "FRONT"},
            ))
        else:
            items.append(DiagnosisItem(
                rule_id="TERMINAL_MISMATCH",
                category="1. 출력/리미트",
                title="전면/후면 출력 단자 설정",
                severity=DiagnosisSeverity.OK,
                difficulty="하",
                evidence=f"단자 설정={cc.terminals.value}",
                cause="단자 불일치 패턴 없음",
                recommendation="조치 불필요",
            ))

        # 1-4. 인터락(Interlock) 차단
        interlock_err = ("5074" in err_texts) or ("802" in err_texts) or ("인터락" in err_texts) or (
            not interlock_engaged and max_abs_v > th.interlock_voltage_v
        )
        if interlock_err:
            items.append(DiagnosisItem(
                rule_id="INTERLOCK_TRIP",
                category="1. 출력/리미트",
                title="고전압 안전 인터락(Interlock) 차단 감지",
                severity=DiagnosisSeverity.CRITICAL,
                difficulty="하",
                evidence=f"최대 설정/측정 전압 {max_abs_v:.2g}V (모델 인터락 기준 ±{th.interlock_voltage_v:g}V)",
                cause="안전 인터락(Safety Interlock) 회로가 열려 있어 고전압 출력이 하드웨어적으로 차단되었습니다.",
                recommendation="장비 후면 초록색 Interlock 커넥터 체결 및 테스트 픽스처 뚜껑 스위치 닫힘 상태를 확인하세요.",
            ))
        else:
            items.append(DiagnosisItem(
                rule_id="INTERLOCK_TRIP",
                category="1. 출력/리미트",
                title="안전 인터락(Interlock) 상태",
                severity=DiagnosisSeverity.OK,
                difficulty="하",
                evidence=f"최대 전압 {max_abs_v:.2g}V / 인터락 기준 ±{th.interlock_voltage_v:g}V",
                cause="정상",
                recommendation="조치 불필요",
            ))

        # =====================================================================
        # CATEGORY 2: 배선 및 접촉 상태 진단 (4개)
        # =====================================================================

        # 2-1. 단선/접촉 불량 (Open)
        if is_open_sig:
            items.append(DiagnosisItem(
                rule_id="OPEN_CIRCUIT",
                category="2. 배선/접촉",
                title="회로 열림(Open) / 프로브 접촉 불량 감지",
                severity=DiagnosisSeverity.WARNING,
                difficulty="하",
                evidence=f"인가 전압 최대 {max_abs_v:.3g}V 대비 최대 전류 {max_abs_i:.2e}A (< {th.open_noise_floor_a:.0e}A)",
                cause="전압을 인가했으나 전류가 노이즈 플로어 수준에서 변동이 없습니다. 프로브 미접촉 또는 케이블 단선이 의심됩니다.",
                recommendation="프로브 팁 접촉 상태, 케이블 단선 여부 및 전면/후면 단자 스위치를 확인하세요 (초고저항 절연체 시료라면 정상일 수 있음).",
            ))
        else:
            items.append(DiagnosisItem(
                rule_id="OPEN_CIRCUIT",
                category="2. 배선/접촉",
                title="회로 연결(Open) 상태",
                severity=DiagnosisSeverity.OK,
                difficulty="하",
                evidence=f"최대 전류 {max_abs_i:.3e}A",
                cause="정상 도통 확인",
                recommendation="조치 불필요",
            ))

        # 2-2. 단락 (Short)
        low_v_comp = any(
            abs(p.voltage_v) <= th.short_max_voltage_v and p.in_compliance and abs(p.current_a) > 1e-4
            for p in pts
        )
        valid_r = [abs(p.resistance_ohm) for p in pts if math.isfinite(p.resistance_ohm) and abs(p.current_a) > 1e-4]
        median_r = float(np.median(valid_r)) if valid_r else float("inf")
        if low_v_comp or (valid_r and median_r < th.short_max_resistance_ohm):
            items.append(DiagnosisItem(
                rule_id="SHORT_CIRCUIT",
                category="2. 배선/접촉",
                title="시료 또는 배선 단락(Short) 의심",
                severity=DiagnosisSeverity.CRITICAL,
                difficulty="하",
                evidence=f"중앙 저항 {median_r:.3g} Ω (기준 < {th.short_max_resistance_ohm:g} Ω) / 저전압 리미트={low_v_comp}",
                cause="아주 낮은 전압에서도 즉시 전류 리미트에 도달하거나 극단적인 저저항이 측정되었습니다.",
                recommendation="시료 패드 또는 프로브 HI/LO 리드가 서로 단락(Short)되지 않았는지 즉시 점검하세요.",
            ))
        else:
            items.append(DiagnosisItem(
                rule_id="SHORT_CIRCUIT",
                category="2. 배선/접촉",
                title="배선 단락(Short) 상태",
                severity=DiagnosisSeverity.OK,
                difficulty="하",
                evidence=f"중앙 저항 {median_r:.3e} Ω" if math.isfinite(median_r) else "단락 징후 없음",
                cause="정상",
                recommendation="조치 불필요",
            ))

        # 2-3. 2-Wire vs 4-Wire 오설정
        wire_issue: DiagnosisItem | None = None
        if cc.sense_mode == SenseMode.WIRE_2 and valid_r and th.short_max_resistance_ohm <= median_r <= th.low_r_4wire_threshold_ohm:
            wire_issue = DiagnosisItem(
                rule_id="WIRE_MODE_MISMATCH",
                category="2. 배선/접촉",
                title="저저항 측정 시 2-Wire 배선 오차 주의",
                severity=DiagnosisSeverity.WARNING,
                difficulty="중",
                evidence=f"현재 2-Wire 모드 / 측정 저항 {median_r:.2f} Ω (≤ {th.low_r_4wire_threshold_ohm:g} Ω)",
                cause="10Ω 이하 저저항 소자를 2-Wire로 측정하면 리드선 접촉 저항(수백 mΩ~수 Ω)이 결과에 그대로 합산됩니다.",
                recommendation="정확한 저항/전압 측정을 위해 4-Wire(Kelvin Remote Sense) 배선 모드로 전환하는 것을 권장합니다.",
                fix_action_id="ENABLE_4WIRE",
                fix_label="4-Wire(Kelvin) 모드 전환",
                fix_params={"sense_mode": "4WIRE"},
            )
        elif cc.sense_mode == SenseMode.WIRE_4 and len(meas_arr) >= 6:
            # 4-Wire sense open often causes wild +/- rail swinging or huge std dev
            med_abs = float(np.median(np.abs(meas_arr)))
            if med_abs > 1e-6 and float(np.std(meas_arr)) / med_abs > 1.5 and int(np.sum(np.diff(np.sign(meas_arr)) != 0)) >= len(meas_arr) // 3:
                wire_issue = DiagnosisItem(
                    rule_id="WIRE_MODE_MISMATCH",
                    category="2. 배선/접촉",
                    title="4-Wire Sense 리드 미연결/발진 의심",
                    severity=DiagnosisSeverity.CRITICAL,
                    difficulty="중",
                    evidence=f"4-Wire 모드에서 극심한 양/음 발진 감지 (Std/Med={float(np.std(meas_arr))/med_abs:.2f})",
                    cause="4-Wire 모드 설정 상태에서 Sense HI/LO 리드가 연결되지 않아 피드백 루프가 발진하고 있습니다.",
                    recommendation="Sense HI/LO 리드 연결을 점검하거나, 2선식 케이블만 사용 중이라면 2-Wire 모드로 전환하세요.",
                    fix_action_id="ENABLE_2WIRE",
                    fix_label="2-Wire 모드로 전환",
                    fix_params={"sense_mode": "2WIRE"},
                )
        items.append(wire_issue or DiagnosisItem(
            rule_id="WIRE_MODE_MISMATCH",
            category="2. 배선/접촉",
            title="2-Wire / 4-Wire 배선 정합성",
            severity=DiagnosisSeverity.OK,
            difficulty="중",
            evidence=f"현재 배선={cc.sense_mode.value}",
            cause="정상",
            recommendation="조치 불필요",
        ))

        # 2-4. 극성 반전
        pol_rev_ratio = 0.0
        if not is_sink_mode:
            sig_pts = [p for p in pts if abs(p.voltage_v) > 0.05 and abs(p.current_a) > th.open_noise_floor_a * 5]
            if len(sig_pts) >= 3:
                opp = sum(1 for p in sig_pts if (p.voltage_v * p.current_a) < 0)
                pol_rev_ratio = opp / len(sig_pts)
        if pol_rev_ratio >= 0.5:
            items.append(DiagnosisItem(
                rule_id="POLARITY_REVERSED",
                category="2. 배선/접촉",
                title="프로브/케이블 극성 반전(음수 측정) 감지",
                severity=DiagnosisSeverity.WARNING,
                difficulty="하",
                evidence=f"인가 부호와 반대 부호 측정 비율 {pol_rev_ratio*100:.0f}% (Sink 모드 OFF)",
                cause="양(+)의 전압/전류를 인가했으나 측정값이 반대 부호(-)로 나타납니다. HI/LO 케이블이 반대로 연결되었을 수 있습니다.",
                recommendation="HI(적색) / LO(흑색) 케이블 극성 연결을 확인하세요 (배터리 방전·태양전지 발전 테스트라면 Sink 모드를 켜세요).",
            ))
        else:
            items.append(DiagnosisItem(
                rule_id="POLARITY_REVERSED",
                category="2. 배선/접촉",
                title="케이블 극성 정합성",
                severity=DiagnosisSeverity.OK,
                difficulty="하",
                evidence="극성 반전 없음",
                cause="정상",
                recommendation="조치 불필요",
            ))

        # =====================================================================
        # CATEGORY 3: 노이즈 및 측정 정밀도 진단 (4개)
        # =====================================================================

        # 3-1. 전원 노이즈(60Hz) 유입
        noise_detected = False
        noise_ratio = 0.0
        if rc.nplc < 1.0 and len(meas_arr) >= 6 and max_abs_meas > th.open_noise_floor_a:
            # High-frequency residual after removing linear trend (2nd difference)
            d2 = np.diff(meas_arr, n=2)
            noise_est = float(np.std(d2)) / math.sqrt(6.0)
            signal_span = max(float(np.ptp(meas_arr)), float(np.mean(np.abs(meas_arr))), 1e-12)
            noise_ratio = noise_est / signal_span
            if noise_ratio > th.noise_cv_threshold:
                noise_detected = True
        if noise_detected:
            items.append(DiagnosisItem(
                rule_id="POWERLINE_NOISE_60HZ",
                category="3. 노이즈/정밀도",
                title="전원 노이즈(60Hz) 유입 및 변동폭 과다",
                severity=DiagnosisSeverity.WARNING,
                difficulty="중",
                evidence=f"NPLC={rc.nplc:g} (<1.0) 상태에서 고주파 노이즈 비율 {noise_ratio*100:.1f}%",
                cause="적분 시간(NPLC)이 1 미만으로 설정되어 상용 전원(60Hz) 리플 노이즈가 제거되지 않고 측정값에 유입되었습니다.",
                recommendation="적분 시간(NPLC)을 1.0 이상으로 상향하면 60Hz 전원 노이즈가 하드웨어적으로 상쇄됩니다.",
                fix_action_id="SET_NPLC",
                fix_label="NPLC 1.0으로 상향",
                fix_params={"nplc": 1.0},
            ))
        else:
            items.append(DiagnosisItem(
                rule_id="POWERLINE_NOISE_60HZ",
                category="3. 노이즈/정밀도",
                title="전원 노이즈(60Hz) 억제 상태",
                severity=DiagnosisSeverity.OK,
                difficulty="중",
                evidence=f"NPLC={rc.nplc:g} / 노이즈 비율 {noise_ratio*100:.1f}%",
                cause="정상",
                recommendation="조치 불필요",
            ))

        # 3-2. 오토레인지 전환 스파이크
        spike_idx = -1
        if cc.measure_range is None and len(meas_arr) >= 8 and max_abs_meas > 1e-7:
            abs_m = np.abs(meas_arr)
            for k in range(1, len(abs_m) - 1):
                prev_v, cur_v, next_v = abs_m[k - 1], abs_m[k], abs_m[k + 1]
                if prev_v > 0 and next_v > 0:
                    d_prev = math.floor(math.log10(max(prev_v, 1e-15)))
                    d_next = math.floor(math.log10(max(next_v, 1e-15)))
                    interp = 0.5 * (prev_v + next_v)
                    if d_prev != d_next and interp > 0 and abs(cur_v - interp) / interp > 0.65:
                        spike_idx = k + 1
                        break
        if spike_idx > 0:
            rec_rng = _pick_next_decade_range(max_abs_meas)
            items.append(DiagnosisItem(
                rule_id="AUTORANGE_SPIKE",
                category="3. 노이즈/정밀도",
                title="오토레인지(Auto Range) 전환 스파이크 감지",
                severity=DiagnosisSeverity.WARNING,
                difficulty="중",
                evidence=f"Step #{spike_idx} 부근 레인지 10배수 경계 통과 시 급격한 스파이크 발생",
                cause="Auto Range 동작 중 전류/전압 레인지가 스위칭되는 순간 일시적인 과도 스파이크가 유입되었습니다.",
                recommendation=f"측정 최대값에 맞는 고정 레인지(Fixed Range: {rec_rng:g})를 지정하면 스파이크 없는 매끄러운 곡선을 얻을 수 있습니다.",
                fix_action_id="SET_FIXED_RANGE",
                fix_label=f"고정 레인지({rec_rng:g}) 적용",
                fix_params={"measure_range": rec_rng},
            ))
        else:
            items.append(DiagnosisItem(
                rule_id="AUTORANGE_SPIKE",
                category="3. 노이즈/정밀도",
                title="오토레인지 전환 스파이크",
                severity=DiagnosisSeverity.OK,
                difficulty="중",
                evidence="레인지 경계 스파이크 미감지",
                cause="정상",
                recommendation="조치 불필요",
            ))

        # 3-3. 레인지 부적합 (분해능 저하 — 고정 레인지가 최대 측정값보다 10배 이상 큼)
        if cc.measure_range is not None and cc.measure_range > 0 and max_abs_meas > 0 and (
            cc.measure_range >= max_abs_meas * th.range_oversize_ratio
        ):
            rec_rng = _pick_next_decade_range(max_abs_meas)
            items.append(DiagnosisItem(
                rule_id="RANGE_TOO_LARGE",
                category="3. 노이즈/정밀도",
                title="고정 레인지 과대 설정 (분해능 저하)",
                severity=DiagnosisSeverity.WARNING,
                difficulty="하",
                evidence=f"설정 레인지={cc.measure_range:g} vs 최대 측정값={max_abs_meas:.3e} ({cc.measure_range/max_abs_meas:.1f}배 과대)",
                cause="측정 신호 크기에 비해 고정 레인지가 10배 이상 크게 설정되어 유효 자릿수(분해능)와 정밀도가 떨어집니다.",
                recommendation=f"레인을 {rec_rng:g} 이하로 낮추거나 Auto Range로 전환하세요.",
                fix_action_id="SET_AUTO_RANGE",
                fix_label="Auto Range로 전환",
                fix_params={"measure_range": None},
            ))
        else:
            items.append(DiagnosisItem(
                rule_id="RANGE_TOO_LARGE",
                category="3. 노이즈/정밀도",
                title="측정 레인지 적합성",
                severity=DiagnosisSeverity.OK,
                difficulty="하",
                evidence="Auto Range" if cc.measure_range is None else f"고정 레인지 {cc.measure_range:g} 적정",
                cause="정상",
                recommendation="조치 불필요",
            ))

        # 3-4. 장시간 측정 드리프트 (AutoZero OFF)
        total_elapsed = float(t_arr[-1] - t_arr[0]) if len(t_arr) >= 2 else 0.0
        drift_detected = False
        if cc.auto_zero.upper() == "OFF":
            if total_elapsed >= th.drift_min_duration_s:
                drift_detected = True
            elif len(meas_arr) >= 12 and float(np.ptp(src_arr)) < 1e-6:
                # Constant bias DC monitoring: check monotonic baseline drift between first 25% and last 25%
                q = max(2, len(meas_arr) // 4)
                m1 = float(np.mean(meas_arr[:q]))
                m2 = float(np.mean(meas_arr[-q:]))
                if abs(m1) > 1e-12 and abs(m2 - m1) / abs(m1) > 0.05:
                    drift_detected = True
        if drift_detected:
            items.append(DiagnosisItem(
                rule_id="LONG_TERM_DRIFT",
                category="3. 노이즈/정밀도",
                title="장시간 측정 기준선 드리프트(Drift) 의심",
                severity=DiagnosisSeverity.WARNING,
                difficulty="중",
                evidence=f"AutoZero=OFF 상태에서 측정 시간 {total_elapsed:.1f}s / 기준선 이동 감지",
                cause="AutoZero가 꺼진 상태로 장시간 측정하면 내부 회로 온도 변화로 영점(Offset)이 서서히 흐릅니다.",
                recommendation="AutoZero를 ON(최고 정밀도) 또는 ONCE(시작 전 1회 보정)로 설정하세요.",
                fix_action_id="SET_AUTOZERO",
                fix_label="AutoZero=ON 적용",
                fix_params={"auto_zero": "ON"},
            ))
        else:
            items.append(DiagnosisItem(
                rule_id="LONG_TERM_DRIFT",
                category="3. 노이즈/정밀도",
                title="장시간 영점 드리프트 상태",
                severity=DiagnosisSeverity.OK,
                difficulty="중",
                evidence=f"AutoZero={cc.auto_zero} / 경과 시간 {total_elapsed:.2f}s",
                cause="정상",
                recommendation="조치 불필요",
            ))

        # =====================================================================
        # CATEGORY 4: 타이밍 및 동적 특성 진단 (3개)
        # =====================================================================

        # 4-1. 안정화(Settling) 시간 부족 (Dual Sweep 히스테리시스 벌어짐)
        hyst_pct = 0.0
        if rc.sweep_type == SweepType.DUAL and len(meas_arr) >= 8 and max_abs_meas > th.open_noise_floor_a:
            mid = len(meas_arr) // 2
            fwd = meas_arr[:mid]
            rev = meas_arr[mid:mid + len(fwd)][::-1]
            min_len = min(len(fwd), len(rev))
            if min_len >= 3:
                diff_rms = float(np.sqrt(np.mean((fwd[:min_len] - rev[:min_len]) ** 2)))
                hyst_pct = (diff_rms / max(max_abs_meas, 1e-12)) * 100.0
        if hyst_pct > th.settling_hyst_pct:
            rec_delay = min(10.0, round(max(rc.delay_s * 2.0, 0.02), 5))
            items.append(DiagnosisItem(
                rule_id="SETTLING_INSUFFICIENT",
                category="4. 타이밍/동적특성",
                title="안정화(Settling) 시간 부족 / 히스테리시스 벌어짐",
                severity=DiagnosisSeverity.WARNING,
                difficulty="상",
                evidence=f"Dual Sweep 정/역방향 차이 {hyst_pct:.1f}% (> {th.settling_hyst_pct:g}%) / Delay={rc.delay_s:g}s",
                cause="시료의 RC 충전 지연 또는 배선 기생 용량 때문에 스텝 전환 후 안정화되기 전에 측정이 이루어졌습니다.",
                recommendation=f"스텝 지연(Source Delay)을 2배 이상({rec_delay:g}s) 늘려 충분한 안정화 시간을 확보하세요.",
                fix_action_id="INCREASE_DELAY",
                fix_label=f"스텝 지연 {rec_delay:g}s로 증가",
                fix_params={"delay_s": rec_delay},
            ))
        else:
            items.append(DiagnosisItem(
                rule_id="SETTLING_INSUFFICIENT",
                category="4. 타이밍/동적특성",
                title="스텝 안정화(Settling) 응답",
                severity=DiagnosisSeverity.OK,
                difficulty="상",
                evidence=f"Delay={rc.delay_s:g}s / 히스테리시스 {hyst_pct:.1f}%",
                cause="정상",
                recommendation="조치 불필요",
            ))

        # 4-2. 용량성 부하 발진 (Oscillation)
        osc_detected = False
        flip_ratio = 0.0
        if not cc.high_capacitance and len(meas_arr) >= 10 and max_abs_meas > th.open_noise_floor_a * 10:
            diffs = np.diff(meas_arr)
            nz = diffs[np.abs(diffs) > max_abs_meas * 0.03]
            if len(nz) >= 6:
                flips = int(np.sum(nz[:-1] * nz[1:] < 0))
                flip_ratio = flips / (len(nz) - 1)
                if flip_ratio >= th.oscillation_sign_flip_ratio and float(np.std(diffs)) / max_abs_meas > 0.12:
                    osc_detected = True
        if osc_detected:
            items.append(DiagnosisItem(
                rule_id="LOAD_OSCILLATION",
                category="4. 타이밍/동적특성",
                title="용량성 부하 출력 발진(Oscillation) 의심",
                severity=DiagnosisSeverity.CRITICAL,
                difficulty="상",
                evidence=f"연속 반전 진동 비율 {flip_ratio*100:.0f}% (High-Cap 모드 OFF)",
                cause="고용량 커패시터·배터리 등 용량성 부하 연결 시 SMU 피드백 루프가 불안정해져 출력이 상하로 진동하고 있습니다.",
                recommendation="SMU의 High-Capacitance 모드를 활성화하여 출력 루프를 안정화하세요.",
                fix_action_id="ENABLE_HIGH_CAP",
                fix_label="High-Capacitance 모드 켜기",
                fix_params={"high_capacitance": True},
            ))
        else:
            items.append(DiagnosisItem(
                rule_id="LOAD_OSCILLATION",
                category="4. 타이밍/동적특성",
                title="출력 부하 안정성(발진 여부)",
                severity=DiagnosisSeverity.OK,
                difficulty="상",
                evidence=f"High-Cap={'ON' if cc.high_capacitance else 'OFF'} / 발진 징후 없음",
                cause="정상",
                recommendation="조치 불필요",
            ))

        # 4-3. 샘플링 속도 병목
        bottleneck = False
        avg_dt_s = 0.0
        if len(t_arr) >= 4 and rc.delay_s > 0:
            dts = np.diff(t_arr)
            pos_dts = dts[dts >= 0]
            if len(pos_dts) >= 3:
                avg_dt_s = float(np.mean(pos_dts))
                extra_ms = (avg_dt_s - rc.delay_s) * 1000.0
                if avg_dt_s > rc.delay_s * th.bottleneck_delay_ratio and extra_ms >= th.bottleneck_extra_ms:
                    bottleneck = True
        if bottleneck:
            causes: list[str] = []
            if cc.auto_zero.upper() == "ON":
                causes.append("AutoZero=ON")
            if cc.source_readback:
                causes.append("Source Readback=ON")
            if rc.nplc >= 1.0:
                causes.append(f"높은 NPLC({rc.nplc:g})")
            cause_str = ", ".join(causes) if causes else "AutoRange/통신 지연"
            items.append(DiagnosisItem(
                rule_id="SAMPLING_BOTTLENECK",
                category="4. 타이밍/동적특성",
                title="샘플링 속도 병목 (설정 주기 대비 과다 지연)",
                severity=DiagnosisSeverity.WARNING,
                difficulty="중",
                evidence=f"설정 지연 {rc.delay_s*1000:.1f}ms vs 실제 평균 간격 {avg_dt_s*1000:.1f}ms ({avg_dt_s/rc.delay_s:.2f}배)",
                cause=f"장비 내부 오버헤드({cause_str}) 때문에 사용자가 설정한 스텝 주기보다 실제 측정 시간이 크게 지연되었습니다.",
                recommendation="고속 측정이 목적이라면 AutoZero=ONCE, Source Readback=OFF로 전환하고 NPLC를 0.1 이하로 낮추세요.",
                fix_action_id="OPTIMIZE_SPEED",
                fix_label="고속 최적화 (AutoZero=ONCE, Readback=OFF)",
                fix_params={"auto_zero": "ONCE", "source_readback": False},
            ))
        else:
            items.append(DiagnosisItem(
                rule_id="SAMPLING_BOTTLENECK",
                category="4. 타이밍/동적특성",
                title="샘플링 타이밍 / 속도 효율",
                severity=DiagnosisSeverity.OK,
                difficulty="중",
                evidence=f"평균 간격 {avg_dt_s*1000:.1f}ms (설정 지연 {rc.delay_s*1000:.1f}ms)" if avg_dt_s > 0 else "정상",
                cause="정상",
                recommendation="조치 불필요",
            ))

        return DiagnosisReport(
            model=profile.model,
            total_rules_checked=len(items),
            items=items,
            thresholds_summary=th.to_summary_dict(),
        )
