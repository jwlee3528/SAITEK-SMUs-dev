"""SMU Hardware Profile & SOA Envelope Database Loader (Phase 1-3 Foundation)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import yaml


@dataclass(frozen=True)
class SOAEnvelope:
    """Safe Operating Area (SOA) boundary region for DC or Pulse operation."""

    max_v: float
    max_i: float
    mode: str = "DC"  # "DC" or "PULSE"
    max_pw_s: float | None = None
    max_duty: float = 1.0


@dataclass(frozen=True)
class SMUModelProfile:
    """Complete hardware capability and safety constraint profile for an SMU model."""

    model: str
    family: str  # "24xx" or "26xx"
    display_name: str
    channels: int
    channel_names: list[str]
    native_languages: list[str]
    default_factory_lang: str
    tsplink_supported: bool
    combining_supported: bool
    interlock_threshold_v: float
    max_dc_voltage_v: float
    max_dc_current_a: float
    max_dc_power_w: float
    max_pulse_voltage_v: float
    max_pulse_current_a: float
    max_pulse_power_w: float
    min_pulse_width_s: float
    max_duty_cycle: float
    min_nplc: float
    max_nplc: float
    max_buffer_points: int
    has_digitizer: bool = False
    max_digitizer_rate_sps: int = 0
    has_dedicated_pulser: bool = False
    has_contact_check: bool = False
    has_acal: bool = False
    has_breakdown_protection: bool = False
    has_high_cap_mode: bool = True
    has_offset_comp_ohms: bool = True
    has_sink_mode: bool = False
    negative_zero_bias_fix: bool = False
    max_pulser_burst_points: int | None = None
    max_pulser_rate_metric: int | None = None
    non_tsplink_variants: list[str] = field(default_factory=list)
    output_off_modes: list[str] = field(default_factory=list)
    soa_envelopes: list[SOAEnvelope] = field(default_factory=list)

    @property
    def is_24xx(self) -> bool:
        return self.family == "24xx"

    @property
    def is_26xx(self) -> bool:
        return self.family == "26xx"

    def supports_tsplink_for_variant(self, specific_variant: str | None = None) -> bool:
        """Check TSP-Link availability, accounting for 2604B/2614B/2634B non-TSPLink variants."""
        if specific_variant and specific_variant.upper() in self.non_tsplink_variants:
            return False
        return self.tsplink_supported


class ProfileRegistry:
    """Loads and provides lookup for the 8 supported Keithley SMU model profiles."""

    DEFAULT_YAML_PATH = (
        Path(__file__).resolve().parents[3] / "configs" / "profiles" / "smu_profiles.yaml"
    )

    def __init__(self, yaml_path: Path | str | None = None) -> None:
        self.yaml_path = Path(yaml_path) if yaml_path else self.DEFAULT_YAML_PATH
        self._profiles: dict[str, SMUModelProfile] = {}
        self.reload()

    def reload(self) -> None:
        if not self.yaml_path.exists():
            raise FileNotFoundError(f"SMU profile database not found: {self.yaml_path}")
        with open(self.yaml_path, "r", encoding="utf-8") as fh:
            raw: dict[str, Any] = yaml.safe_load(fh) or {}

        models_data = raw.get("models", {})
        loaded: dict[str, SMUModelProfile] = {}
        for model_key, cfg in models_data.items():
            envelopes = [
                SOAEnvelope(
                    max_v=float(e["max_v"]),
                    max_i=float(e["max_i"]),
                    mode=str(e.get("mode", "DC")).upper(),
                    max_pw_s=float(e["max_pw_s"]) if e.get("max_pw_s") is not None else None,
                    max_duty=float(e.get("max_duty", 1.0)),
                )
                for e in cfg.get("soa_envelopes", [])
            ]
            loaded[model_key.upper()] = SMUModelProfile(
                model=model_key.upper(),
                family=cfg["family"],
                display_name=cfg["display_name"],
                channels=int(cfg["channels"]),
                channel_names=list(cfg["channel_names"]),
                native_languages=list(cfg["native_languages"]),
                default_factory_lang=cfg["default_factory_lang"],
                tsplink_supported=bool(cfg["tsplink_supported"]),
                combining_supported=bool(cfg.get("combining_supported", True)),
                interlock_threshold_v=float(cfg.get("interlock_threshold_v", 42.0)),
                max_dc_voltage_v=float(cfg["max_dc_voltage_v"]),
                max_dc_current_a=float(cfg["max_dc_current_a"]),
                max_dc_power_w=float(cfg["max_dc_power_w"]),
                max_pulse_voltage_v=float(cfg["max_pulse_voltage_v"]),
                max_pulse_current_a=float(cfg["max_pulse_current_a"]),
                max_pulse_power_w=float(cfg["max_pulse_power_w"]),
                min_pulse_width_s=float(cfg["min_pulse_width_s"]),
                max_duty_cycle=float(cfg["max_duty_cycle"]),
                min_nplc=float(cfg["min_nplc"]),
                max_nplc=float(cfg["max_nplc"]),
                max_buffer_points=int(cfg["max_buffer_points"]),
                has_digitizer=bool(cfg.get("has_digitizer", False)),
                max_digitizer_rate_sps=int(cfg.get("max_digitizer_rate_sps", 0)),
                has_dedicated_pulser=bool(cfg.get("has_dedicated_pulser", False)),
                has_contact_check=bool(cfg.get("has_contact_check", False)),
                has_acal=bool(cfg.get("has_acal", False)),
                has_breakdown_protection=bool(cfg.get("has_breakdown_protection", False)),
                has_high_cap_mode=bool(cfg.get("has_high_cap_mode", True)),
                has_offset_comp_ohms=bool(cfg.get("has_offset_comp_ohms", True)),
                has_sink_mode=bool(cfg.get("has_sink_mode", False)),
                negative_zero_bias_fix=bool(cfg.get("negative_zero_bias_fix", False)),
                max_pulser_burst_points=cfg.get("max_pulser_burst_points"),
                max_pulser_rate_metric=cfg.get("max_pulser_rate_metric"),
                non_tsplink_variants=list(cfg.get("non_tsplink_variants", [])),
                output_off_modes=list(cfg.get("output_off_modes", [])),
                soa_envelopes=envelopes,
            )
        self._profiles = loaded

    def list_models(self) -> list[str]:
        return list(self._profiles.keys())

    def get(self, model_name: str) -> SMUModelProfile:
        key = self.resolve_model_key(model_name)
        return self._profiles[key]

    def resolve_model_key(self, raw_model: str) -> str:
        """Resolve *IDN? model strings (e.g. 'MODEL 2450', '2602B', '2614B') to profile keys."""
        cleaned = raw_model.upper().replace("MODEL", "").strip()
        if cleaned in self._profiles:
            return cleaned
        # Map 2601B/2602B/2604B/2611B/2612B/2614B/2634B/2635B/2636B to 2600B profile
        if cleaned.startswith("26") and cleaned.endswith("B") and "PULSE" not in cleaned and cleaned != "2606B":
            return "2600B"
        for k in self._profiles:
            if k in cleaned:
                return k
        raise KeyError(f"Unsupported SMU model '{raw_model}'. Supported: {self.list_models()}")
