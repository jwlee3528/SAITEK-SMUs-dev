"""Requirement 6, 8 & 9 Utilities:
- Manual PDF Finder & OS Viewer Launcher for all 8 SMU models (`find_manual_pdf_for_model`).
- Range Clamping with User Notification (`clamp_value_with_notice`).
- Complete Setup & Recipe Persistence Manager (`SetupPersistenceManager`) for saving/loading all
  instrument selections, multi-node configurations, waveform steps, and pulse parameters at once.
"""

from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
from typing import Any

from smu_app.core.models import (
    ChannelConfig,
    LogicalChannel,
    MeasureFunction,
    OutputOffMode,
    RepeatMode,
    SenseMode,
    SourceFunction,
    SweepRecipe,
    SweepType,
    WaveformStep,
)
from smu_app.features.pulse_digitizer import PulseDigitizerConfig


MANUAL_FOLDER_MAP: dict[str, str] = {
    "2450": "2450",
    "2460": "2460",
    "2461": "2461",
    "2470": "2470",
    "2600B": "2600B",
    "2601B-PULSE": "2601B_PULSE",
    "2606B": "2606B",
    "2651A": "2651A",
}


def find_manual_pdf_for_model(model: str, base_manuals_dir: Path | str | None = None) -> Path | None:
    """Requirement 6: Locate the reference manual PDF file for the selected SMU model."""
    root = (
        Path(base_manuals_dir)
        if base_manuals_dir
        else (Path(__file__).resolve().parents[3] / "docs" / "manuals")
    )
    folder_name = MANUAL_FOLDER_MAP.get(model.upper(), model.upper())
    model_dir = root / folder_name
    if model_dir.exists():
        pdfs = sorted(model_dir.glob("*.pdf"))
        if pdfs:
            return pdfs[0]
    # Fallback search in root manuals folder
    for p in root.rglob("*.pdf"):
        if model.upper().replace("-PULSE", "") in p.name.upper():
            return p
    return None


def clamp_value_with_notice(
    value: float,
    min_val: float,
    max_val: float,
    param_label: str,
    model_name: str,
    unit: str = "",
) -> tuple[float, str | None]:
    """Requirement 8: If user input exceeds model limits, clamp to boundary and return a Korean notification message."""
    if value > max_val:
        msg = (
            f"[{model_name} 입력 범위 초과 자동 조정]\n"
            f"• 항목: {param_label}\n"
            f"• 입력하신 값({value:g}{unit})이 {model_name} 최대 허용 한계({max_val:g}{unit})를 초과하여 "
            f"최댓값인 {max_val:g}{unit}(으)로 자동 변경되었습니다."
        )
        return max_val, msg
    if value < min_val:
        msg = (
            f"[{model_name} 입력 범위 미달 자동 조정]\n"
            f"• 항목: {param_label}\n"
            f"• 입력하신 값({value:g}{unit})이 {model_name} 최소 허용 한계({min_val:g}{unit})보다 작아 "
            f"최솟값인 {min_val:g}{unit}(으)로 자동 변경되었습니다."
        )
        return min_val, msg
    return value, None


class SetupPersistenceManager:
    """Requirement 9: Save and load complete application state (instrument, nodes, per-node configs, waveform, pulse)."""

    DEFAULT_LAST_SESSION_PATH = (
        Path(__file__).resolve().parents[3] / "configs" / "last_session_setup.json"
    )

    @classmethod
    def save_full_setup(
        cls,
        file_path: Path | str,
        model: str,
        variant: str,
        node_count: int,
        dut_type: str,
        interlock_engaged: bool,
        channel_config: ChannelConfig,
        recipe: SweepRecipe,
        pulse_config: PulseDigitizerConfig,
        logical_channels: list[LogicalChannel],
    ) -> Path:
        path = Path(file_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "version": "3.0",
            "model": model,
            "variant": variant,
            "node_count": int(node_count),
            "dut_type": dut_type,
            "interlock_engaged": bool(interlock_engaged),
            "channel_config": {
                "source_func": channel_config.source_func.value,
                "measure_func": channel_config.measure_func.value,
                "compliance_limit": channel_config.compliance_limit,
                "nplc": channel_config.nplc,
                "sense_mode": channel_config.sense_mode.value,
                "output_off_mode": channel_config.output_off_mode.value,
                "contact_check_enabled": channel_config.contact_check_enabled,
            },
            "recipe": {
                "name": recipe.name,
                "sweep_type": recipe.sweep_type.value,
                "source_func": recipe.source_func.value,
                "measure_func": recipe.measure_func.value,
                "start_level": recipe.start_level,
                "stop_level": recipe.stop_level,
                "points": recipe.points,
                "delay_s": recipe.delay_s,
                "compliance_limit": recipe.compliance_limit,
                "nplc": recipe.nplc,
                "repeat_mode": recipe.repeat_mode.value,
                "repeat_count": recipe.repeat_count,
                "end_Return_to_bias": recipe.end_Return_to_bias,
                "apply_negative_zero_fix": recipe.apply_negative_zero_fix,
                "custom_steps": [asdict(st) for st in recipe.custom_steps],
            },
            "pulse_config": asdict(pulse_config),
            "logical_channels": [asdict(lch) for lch in logical_channels],
        }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        return path

    @classmethod
    def load_full_setup(cls, file_path: Path | str) -> dict[str, Any]:
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"설정 파일을 찾을 수 없습니다: {path}")
        with open(path, "r", encoding="utf-8") as fh:
            raw: dict[str, Any] = json.load(fh)

        rc_raw = raw.get("recipe", {})
        steps = [
            WaveformStep(
                step_index=int(s["step_index"]),
                duration_s=float(s["duration_s"]),
                level=float(s["level"]),
                compliance_limit=float(s["compliance_limit"]) if s.get("compliance_limit") is not None else None,
                measure_enable=bool(s.get("measure_enable", True)),
                is_pulse=bool(s.get("is_pulse", False)),
                pulse_width_s=s.get("pulse_width_s"),
                pulse_off_time_s=s.get("pulse_off_time_s"),
            )
            for s in rc_raw.get("custom_steps", [])
        ]
        recipe = SweepRecipe(
            name=rc_raw.get("name", "loaded_recipe"),
            sweep_type=SweepType(rc_raw.get("sweep_type", "LINEAR")),
            source_func=SourceFunction(rc_raw.get("source_func", "VOLTAGE")),
            measure_func=MeasureFunction(rc_raw.get("measure_func", "CURRENT")),
            start_level=float(rc_raw.get("start_level", 0.0)),
            stop_level=float(rc_raw.get("stop_level", 1.2)),
            points=int(rc_raw.get("points", 51)),
            delay_s=float(rc_raw.get("delay_s", 0.005)),
            compliance_limit=float(rc_raw.get("compliance_limit", 0.1)),
            nplc=float(rc_raw.get("nplc", 1.0)),
            repeat_mode=RepeatMode(rc_raw.get("repeat_mode", "SINGLE")),
            repeat_count=int(rc_raw.get("repeat_count", 1)),
            end_Return_to_bias=bool(rc_raw.get("end_Return_to_bias", True)),
            apply_negative_zero_fix=bool(rc_raw.get("apply_negative_zero_fix", True)),
            custom_steps=steps,
        )

        cc_raw = raw.get("channel_config", {})
        ch_cfg = ChannelConfig(
            source_func=SourceFunction(cc_raw.get("source_func", "VOLTAGE")),
            measure_func=MeasureFunction(cc_raw.get("measure_func", "CURRENT")),
            compliance_limit=float(cc_raw.get("compliance_limit", 0.1)),
            nplc=float(cc_raw.get("nplc", 1.0)),
            sense_mode=SenseMode(cc_raw.get("sense_mode", "2WIRE")),
            output_off_mode=OutputOffMode(cc_raw.get("output_off_mode", "NORMAL")),
            contact_check_enabled=bool(cc_raw.get("contact_check_enabled", False)),
        )

        pc_raw = raw.get("pulse_config", {})
        p_cfg = PulseDigitizerConfig(
            bias_level=float(pc_raw.get("bias_level", 0.0)),
            pulse_level=float(pc_raw.get("pulse_level", 5.0)),
            pulse_width_s=float(pc_raw.get("pulse_width_s", 0.0002)),
            pulse_period_s=float(pc_raw.get("pulse_period_s", 0.004)),
            pulse_count=int(pc_raw.get("pulse_count", 1)),
            digitize_enable=bool(pc_raw.get("digitize_enable", True)),
            sample_rate_sps=int(pc_raw.get("sample_rate_sps", 1000000)),
            aperture_s=float(pc_raw.get("aperture_s", 1e-6)),
            load_inductance_uh=float(pc_raw.get("load_inductance_uh", 0.5)),
            fast_adc_mode=bool(pc_raw.get("fast_adc_mode", True)),
            sink_mode_enable=bool(pc_raw.get("sink_mode_enable", False)),
            ambient_temp_c=float(pc_raw.get("ambient_temp_c", 25.0)),
        )

        l_chs = [
            LogicalChannel(
                node_id=int(c["node_id"]),
                channel_name=str(c["channel_name"]),
                role=str(c.get("role", "PARALLEL_DUT")),
                enabled=bool(c.get("enabled", True)),
                independent_override=bool(c.get("independent_override", False)),
                override_sweep_type=str(c.get("override_sweep_type", "LINEAR")),
                override_source_func=str(c.get("override_source_func", "VOLTAGE")),
                override_start_level=float(c.get("override_start_level", 0.0)),
                override_stop_level=float(c.get("override_stop_level", 1.2)),
                override_compliance=float(c.get("override_compliance", 0.1)),
                visa_resource=str(c.get("visa_resource", "")),
            )
            for c in raw.get("logical_channels", [])
        ]

        return {
            "model": str(raw.get("model", "2450")),
            "variant": str(raw.get("variant", "2450")),
            "node_count": int(raw.get("node_count", 1)),
            "dut_type": str(raw.get("dut_type", "SILICON_DIODE")),
            "interlock_engaged": bool(raw.get("interlock_engaged", True)),
            "channel_config": ch_cfg,
            "recipe": recipe,
            "pulse_config": p_cfg,
            "logical_channels": l_chs,
        }
