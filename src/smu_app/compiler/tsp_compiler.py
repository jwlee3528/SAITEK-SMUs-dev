"""Dual-Generation TSP Waveform & Sweep Script Compiler (Strategy Pattern for 24xx & 26xx).

Resolves v3.0 Challenge 2:
- TSP24xxGenerator compiles SweepRecipe into 2450/2460/2461/2470 `smu.*` + `Config List` + `TriggerFlow` blocks.
- TSP26xxGenerator compiles SweepRecipe into 2600B/2601B-PULSE/2606B/2651A `smua.*`/`smub.*` + `Trigger Timer` / `List Sweep`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from smu_app.core.models import (
    ChannelConfig,
    RepeatMode,
    SourceFunction,
    SweepRecipe,
    SweepType,
)
from smu_app.core.profiles import SMUModelProfile


class BaseTSPCompiler(ABC):
    """Strategy interface for compiling high-level SweepRecipe into native Keithley TSP scripts."""

    @abstractmethod
    def compile_channel_setup(self, cfg: ChannelConfig, profile: SMUModelProfile) -> list[str]:
        """Generate TSP commands to configure terminals, 2/4-wire sense, NPLC, compliance, and output-off state."""

    @abstractmethod
    def compile_sweep(self, cfg: ChannelConfig, recipe: SweepRecipe, profile: SMUModelProfile) -> list[str]:
        """Generate hardware-triggered TSP sweep or custom waveform script lines."""


class TSP24xxGenerator(BaseTSPCompiler):
    """TSP Script Generator for Keithley 2450, 2460, 2461, and 2470 (TriggerFlow & Config List)."""

    def compile_channel_setup(self, cfg: ChannelConfig, profile: SMUModelProfile) -> list[str]:
        pfx = cfg.logical_channel.tsp_prefix  # "smu" or "node[N].smu"
        s_func = "smu.FUNC_DC_VOLTAGE" if cfg.source_func == SourceFunction.VOLTAGE else "smu.FUNC_DC_CURRENT"
        m_func = "smu.FUNC_DC_CURRENT" if cfg.source_func == SourceFunction.VOLTAGE else "smu.FUNC_DC_VOLTAGE"
        lim_attr = "ilimit.level" if cfg.source_func == SourceFunction.VOLTAGE else "vlimit.level"
        sense_const = "smu.SENSE_4WIRE" if cfg.sense_mode.value == "4WIRE" else "smu.SENSE_2WIRE"
        term_const = "smu.TERMINALS_REAR" if cfg.terminals.value == "REAR" else "smu.TERMINALS_FRONT"
        off_map = {
            "NORMAL": "smu.OFFMODE_NORMAL",
            "HIGH_Z": "smu.OFFMODE_HIGHZ",
            "ZERO": "smu.OFFMODE_ZERO",
            "GUARD": "smu.OFFMODE_GUARD",
        }
        off_const = off_map.get(cfg.output_off_mode.value, "smu.OFFMODE_NORMAL")
        az_const = "smu.ON" if cfg.auto_zero == "ON" else "smu.OFF"
        rb_const = "smu.ON" if cfg.source_readback else "smu.OFF"
        hc_const = "smu.ON" if cfg.high_capacitance else "smu.OFF"

        # Sequence ordered specifically for Keithley 2450/2460/2461/2470 firmware to prevent Warning 5079 & Warning 5076:
        # 1) Set source & measure functions first
        # 2) When using FRONT banana terminals for current measurement, set autorangelow = 10uA (1e-5) before setting terminals
        #    so the instrument never pops up Warning 5079 ("Consider using the rear triaxial connectors for better low-current measurements").
        # 3) Enable autorange BEFORE setting source compliance limit so Warning 5076 is never triggered.
        cmds = [
            f"{pfx}.reset()",
            "eventlog.clear()",
            f"{pfx}.source.func = {s_func}",
            f"{pfx}.measure.func = {m_func}",
        ]
        if cfg.source_func == SourceFunction.VOLTAGE and cfg.terminals.value == "FRONT":
            cmds.append(f"{pfx}.measure.autorangelow = 1e-5")
        cmds.extend([
            f"{pfx}.measure.autorange = {pfx}.ON",
            f"{pfx}.source.{lim_attr} = {cfg.compliance_limit:.6g}",
            f"{pfx}.terminals = {term_const}",
            f"{pfx}.source.offmode = {off_const}",
            f"{pfx}.source.readback = {rb_const}",
            f"{pfx}.source.highc = {hc_const}",
            f"{pfx}.measure.sense = {sense_const}",
            f"{pfx}.measure.nplc = {max(profile.min_nplc, min(profile.max_nplc, cfg.nplc)):.4g}",
            f"{pfx}.measure.autozero.enable = {az_const}",
        ])
        if profile.has_breakdown_protection and cfg.breakdown_protection_enabled:
            cmds.append(f"{pfx}.source.protect.breakdown = smu.ON")
        return cmds

    def compile_sweep(self, cfg: ChannelConfig, recipe: SweepRecipe, profile: SMUModelProfile) -> list[str]:
        pfx = cfg.logical_channel.tsp_prefix
        cmds = self.compile_channel_setup(recipe_override_cfg(cfg, recipe), profile)
        levels = recipe.generate_levels()
        count = len(levels)
        repeat_n = max(1, recipe.repeat_count if recipe.repeat_mode == RepeatMode.FINITE else 1)

        if recipe.sweep_type == SweepType.LINEAR:
            dual_const = f"{pfx}.ON" if False else f"{pfx}.OFF"
            cmds.extend([
                "defbuffer1.clear()",
                f"{pfx}.source.sweeplinear('SAITEK_SWP', {recipe.start_level:.6g}, {recipe.stop_level:.6g}, {count}, {max(0.0, recipe.delay_s):.6g}, {repeat_n}, {pfx}.RANGE_AUTO, {pfx}.ON, {dual_const}, defbuffer1)",
                "trigger.model.initiate()",
            ])
            return cmds

        # General Config List + smu.source.sweeplist() compilation (supports Arbitrary List, per-step variable delay, Dual, Log, Finite Repeat)
        list_name = "SAITEK_SRC_LIST"
        lim_attr = "ilimit.level" if recipe.source_func == SourceFunction.VOLTAGE else "vlimit.level"
        cmds.extend([
            "defbuffer1.clear()",
            f"{pfx}.source.configlist.create('{list_name}')",
        ])
        if recipe.sweep_type == SweepType.LIST and recipe.custom_steps:
            for st in recipe.custom_steps:
                lim_val = st.compliance_limit if st.compliance_limit is not None else recipe.compliance_limit
                cmds.append(
                    f"{pfx}.source.level = {st.level:.6g}; {pfx}.source.{lim_attr} = {lim_val:.6g}; "
                    f"{pfx}.source.delay = {max(0.0, st.duration_s):.6g}; "
                    f"{pfx}.source.configlist.store('{list_name}')"
                )
        else:
            for lv in levels:
                cmds.append(
                    f"{pfx}.source.level = {lv:.6g}; {pfx}.source.{lim_attr} = {recipe.compliance_limit:.6g}; "
                    f"{pfx}.source.configlist.store('{list_name}')"
                )

        cmds.extend([
            f"{pfx}.source.sweeplist('{list_name}', 1, {max(0.0, recipe.delay_s):.6g}, {repeat_n}, {pfx}.ON, defbuffer1)",
            "trigger.model.initiate()",
        ])
        return cmds


class TSP26xxGenerator(BaseTSPCompiler):
    """TSP Script Generator for Keithley 2600B, 2601B-PULSE, 2606B, and 2651A."""

    def compile_channel_setup(self, cfg: ChannelConfig, profile: SMUModelProfile) -> list[str]:
        ch = cfg.logical_channel.tsp_prefix  # e.g. "smua", "smub", "node[2].smua"
        s_func = f"{ch}.OUTPUT_DCVOLTS" if cfg.source_func == SourceFunction.VOLTAGE else f"{ch}.OUTPUT_DCAMPS"
        lim_attr = "limiti" if cfg.source_func == SourceFunction.VOLTAGE else "limitv"
        sense_const = f"{ch}.SENSE_REMOTE" if cfg.sense_mode.value == "4WIRE" else f"{ch}.SENSE_LOCAL"
        off_map = {
            "NORMAL": f"{ch}.OUTPUT_NORMAL",
            "HIGH_Z": f"{ch}.OUTPUT_HIGH_Z",
            "ZERO": f"{ch}.OUTPUT_ZERO",
        }
        off_const = off_map.get(cfg.output_off_mode.value, f"{ch}.OUTPUT_NORMAL")
        az_map = {"ON": f"{ch}.AUTOZERO_AUTO", "OFF": f"{ch}.AUTOZERO_OFF", "ONCE": f"{ch}.AUTOZERO_ONCE"}
        az_const = az_map.get(cfg.auto_zero, f"{ch}.AUTOZERO_AUTO")
        hc_const = f"{ch}.ENABLE" if cfg.high_capacitance else f"{ch}.DISABLE"

        return [
            f"{ch}.reset()",
            f"{ch}.source.func = {s_func}",
            f"{ch}.source.offmode = {off_const}",
            f"{ch}.source.highc = {hc_const}",
            f"{ch}.source.{lim_attr} = {cfg.compliance_limit:.6g}",
            f"{ch}.sense = {sense_const}",
            f"{ch}.measure.nplc = {max(profile.min_nplc, min(profile.max_nplc, cfg.nplc)):.4g}",
            f"{ch}.measure.autozero = {az_const}",
        ]

    def compile_sweep(self, cfg: ChannelConfig, recipe: SweepRecipe, profile: SMUModelProfile) -> list[str]:
        ch = cfg.logical_channel.tsp_prefix
        cmds = self.compile_channel_setup(recipe_override_cfg(cfg, recipe), profile)
        levels = recipe.generate_levels()

        # Apply v3.0 -1e-12 Negative Zero bias polarity delay fix when transitioning 0 -> negative on 26xx
        if profile.negative_zero_bias_fix and recipe.apply_negative_zero_fix:
            has_neg = any(v < 0 for v in levels)
            if has_neg:
                levels = [-1e-12 if v == 0.0 else v for v in levels]

        level_table = "{" + ", ".join(f"{v:.6g}" for v in levels) + "}"
        s_fn = "listv" if recipe.source_func == SourceFunction.VOLTAGE else "listi"
        lim_attr = "limiti" if recipe.source_func == SourceFunction.VOLTAGE else "limitv"
        total_pts = len(levels) * (recipe.repeat_count if recipe.repeat_mode == RepeatMode.FINITE else 1)
        end_action = f"{ch}.SOURCE_IDLE" if recipe.end_Return_to_bias else f"{ch}.SOURCE_HOLD"

        if recipe.sweep_type == SweepType.LIST and recipe.custom_steps:
            delay_table = "{" + ", ".join(f"{max(1e-5, s.duration_s):.6g}" for s in recipe.custom_steps) + "}"
            timer_setup = f"trigger.timer[1].delaylist = {delay_table}"
        else:
            timer_setup = f"trigger.timer[1].delay = {max(1e-5, recipe.delay_s):.6g}"

        cmds.extend([
            f"{ch}.nvbuffer1.clear()",
            f"{ch}.nvbuffer1.collecttimestamps = 1",
            f"{ch}.nvbuffer1.collectsourcevalues = 1",
            timer_setup,
            f"trigger.timer[1].count = {total_pts}",
            f"trigger.timer[1].passthrough = true",
            f"trigger.timer[1].stimulus = {ch}.trigger.ARMED_EVENT_ID",
            f"{ch}.trigger.source.{s_fn}({level_table})",
            f"{ch}.trigger.source.{lim_attr} = {recipe.compliance_limit:.6g}",
            f"{ch}.trigger.source.action = {ch}.ENABLE",
            f"{ch}.trigger.source.stimulus = 0",
            f"{ch}.trigger.measure.action = {ch}.ENABLE",
            f"{ch}.trigger.measure.iv({ch}.nvbuffer1)",
            f"{ch}.trigger.measure.stimulus = trigger.timer[1].EVENT_ID",
            f"{ch}.trigger.endpulse.action = {ch}.SOURCE_HOLD",
            f"{ch}.trigger.endsweep.action = {end_action}",
            f"{ch}.trigger.count = {total_pts}",
            f"{ch}.trigger.arm.count = 1",
            f"{ch}.source.output = {ch}.OUTPUT_ON",
            f"{ch}.trigger.initiate()",
        ])
        return cmds


def recipe_override_cfg(cfg: ChannelConfig, recipe: SweepRecipe) -> ChannelConfig:
    """Create a shallow copy of ChannelConfig updated with recipe source/measure/NPLC/compliance."""
    from dataclasses import replace

    return replace(
        cfg,
        source_func=recipe.source_func,
        measure_func=recipe.measure_func,
        compliance_limit=recipe.compliance_limit,
        nplc=recipe.nplc,
    )


def get_tsp_compiler(profile: SMUModelProfile) -> BaseTSPCompiler:
    return TSP24xxGenerator() if profile.is_24xx else TSP26xxGenerator()
