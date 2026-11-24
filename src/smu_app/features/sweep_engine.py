"""End-to-End Measurement & Sweep Execution Engine (Phase 1 & Phase 2 Complete Pipeline).

Supports:
- Pre-Flight 0.5s safety verification (SOA, Duty Cycle, Interlock, Contact Check, Buffer Capacity).
- Multi-SMU (1~10 nodes) and multi-channel (1/2/4-CH) logical channel mapping & TSP-Link 3-Sync-Line execution.
- Custom arbitrary waveform (`SweepType.LIST`) with per-step variable duration & measurement masking.
- Dual Continuous Mode execution (`LOW_SPEED_STREAMING` chunk callback vs `HIGH_SPEED_BURST` buffer capture).
- 3-Terminal FET Evaluation (`FET_GATE` step + `FET_DRAIN` sweep + `FET_SOURCE` common ground).
- Output Combining (`PARALLEL_CURRENT` 2x current / `SERIES_VOLTAGE` 2x voltage) synchronized execution.
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable
import uuid

from smu_app.analysis.iv_pulse_analyzer import analyze_measurement_points
from smu_app.core.models import (
    AcquisitionMode,
    ChannelConfig,
    LogicalChannel,
    MeasurementResult,
    ReadingPoint,
    RepeatMode,
    SweepRecipe,
    SweepType,
)
from smu_app.drivers.base_smu import BaseTSPDriver
from smu_app.features.pulse_digitizer import (
    HighSpeedPulseDigitizerEngine,
    PulseDigitizerConfig,
)
from smu_app.safety.safety_manager import SafetyAndSOAValidator
from smu_app.simulator.virtual_smu import VirtualSMUTransport
from smu_app.sync.cluster_manager import (
    ChannelRole,
    CombinedOutputConfig,
    TSPLinkClusterManager,
)


class SweepExecutionEngine:
    """Orchestrates Pre-Flight safety check, multi-node TSP-Link sync, custom waveforms, FET sweeps, and auto-analysis."""

    def __init__(self, driver: BaseTSPDriver) -> None:
        self.driver = driver
        self.cluster = TSPLinkClusterManager(driver)

    def run_measurement(
        self,
        cfg: ChannelConfig,
        recipe: SweepRecipe,
        node_count: int = 1,
        specific_variant: str | None = None,
        logical_channels: list[LogicalChannel] | None = None,
        combined_config: CombinedOutputConfig | None = None,
        pulse_config: PulseDigitizerConfig | None = None,
        streaming_callback: Callable[[list[ReadingPoint]], None] | None = None,
    ) -> MeasurementResult:
        started_at = datetime.now()
        session_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]

        ambient_c = pulse_config.ambient_temp_c if pulse_config else 25.0
        sink_en = pulse_config.sink_mode_enable if pulse_config else False

        # 1. Pre-Flight Check (SOA, Thermal/Sink Duty Cycle, Interlock, Contact Check, Buffer Capacity)
        preflight = SafetyAndSOAValidator.run_preflight_check(
            driver=self.driver,
            cfg=cfg,
            recipe=recipe,
            node_count=node_count,
            ambient_temp_c=ambient_c,
            is_sink_quadrant=sink_en,
        )
        if not preflight.passed:
            raise ValueError("Pre-Flight 안전 검증 차단:\n- " + "\n- ".join(preflight.errors))

        recipe.acquisition_mode = preflight.recommended_acquisition_mode
        warnings = list(preflight.warnings)

        # Check 2461 ACAL temperature drift (+/-5°C) if pulse_config is provided
        if pulse_config is not None and self.driver.profile.has_acal:
            acal_st = HighSpeedPulseDigitizerEngine.check_and_run_2461_acal(
                self.driver.profile, pulse_config, execute_now=False
            )
            if acal_st.acal_recommended:
                warnings.append(acal_st.message)

        # 2. Initialize 1..10 node cluster and resolve active logical channels
        topo = self.cluster.validate_and_initialize_cluster(
            node_count=node_count,
            specific_variant=specific_variant,
        )
        if logical_channels:
            topo.logical_channels = list(logical_channels)
        warnings.extend(topo.warnings)

        active_channels = [c for c in topo.logical_channels if c.enabled]
        if not active_channels:
            active_channels = [cfg.logical_channel]

        # 3. Emit TSP-Link 3-Sync-Line coordination script when multiple channels/nodes are active
        levels = recipe.generate_levels()
        sync_cmds = self.cluster.generate_tsplink_sync_script(
            topo=topo,
            step_count=len(levels),
            delay_s=recipe.delay_s,
        )
        for sc in sync_cmds:
            self.driver.transport.write(sc)

        # 4. Check if Output Combining mode is active
        if combined_config is not None and combined_config.mode.value != "NONE":
            warnings.extend(combined_config.safety_notes)

        # 5. Execute measurement (High-Speed Pulse/Digitizer path vs Standard/Arbitrary Sweep path)
        is_pulse_mode = recipe.sweep_type in (SweepType.PULSE_TRAIN, SweepType.PULSE_SWEEP)
        active_node_ids = sorted({c.node_id for c in active_channels})

        if is_pulse_mode and pulse_config is not None:
            pulse_cmds = HighSpeedPulseDigitizerEngine.compile_pulse_digitizer_tsp(
                profile=self.driver.profile,
                cfg=cfg,
                recipe=recipe,
                p_cfg=pulse_config,
            )
            for pcmd in pulse_cmds:
                self.driver.transport.write(pcmd)

            if isinstance(self.driver.transport, VirtualSMUTransport):
                points = HighSpeedPulseDigitizerEngine.simulate_digitized_pulse_waveform(
                    profile=self.driver.profile,
                    cfg=cfg,
                    recipe=recipe,
                    p_cfg=pulse_config,
                )
                self.driver.transport.state.last_buffer = points
                self.driver.transport.query_binary_values("printbuffer(1, #buffer, buffer.readings)")
            else:
                points = self.driver.execute_sweep(cfg=cfg, recipe=recipe, node_ids=active_node_ids)
        else:
            points = self._execute_across_channels(
                cfg=cfg,
                recipe=recipe,
                active_channels=active_channels,
                active_node_ids=active_node_ids,
                streaming_callback=streaming_callback,
            )

        # 6. If combined mode is active, run synchronized safe shutdown script
        if combined_config is not None and combined_config.mode.value != "NONE":
            for sd_cmd in self.cluster.generate_safe_combined_shutdown_script(combined_config):
                self.driver.transport.write(sd_cmd)

        # 7. Run automatic device & pulse parameter extraction
        metrics = analyze_measurement_points(points, recipe)

        completed_at = datetime.now()
        return MeasurementResult(
            session_id=session_id,
            model=self.driver.profile.model,
            recipe_name=recipe.name,
            started_at=started_at,
            completed_at=completed_at,
            channel_config=cfg,
            recipe=recipe,
            points=points,
            metrics=metrics,
            warnings=warnings,
            aborted=False,
        )

    def _execute_across_channels(
        self,
        cfg: ChannelConfig,
        recipe: SweepRecipe,
        active_channels: list[LogicalChannel],
        active_node_ids: list[int],
        streaming_callback: Callable[[list[ReadingPoint]], None] | None,
    ) -> list[ReadingPoint]:
        """Handle per-node/per-channel independent waveform & source overrides (Requirement 7),
        custom step measure masking, multi-channel simulation, and dual continuous modes.
        """
        from dataclasses import replace
        from smu_app.core.models import MeasureFunction, SourceFunction, SweepType

        has_independent_overrides = len(active_channels) >= 2 and any(
            c.independent_override for c in active_channels
        )

        if has_independent_overrides:
            combined_points: list[ReadingPoint] = []
            for lch in active_channels:
                if lch.independent_override:
                    s_func = SourceFunction(lch.override_source_func)
                    m_func = (
                        MeasureFunction.CURRENT
                        if s_func == SourceFunction.VOLTAGE
                        else MeasureFunction.VOLTAGE
                    )
                    ch_cfg = replace(
                        cfg,
                        logical_channel=lch,
                        source_func=s_func,
                        measure_func=m_func,
                        compliance_limit=lch.override_compliance,
                    )
                    ch_recipe = replace(
                        recipe,
                        sweep_type=SweepType(lch.override_sweep_type),
                        source_func=s_func,
                        measure_func=m_func,
                        start_level=lch.override_start_level,
                        stop_level=lch.override_stop_level,
                        compliance_limit=lch.override_compliance,
                    )
                else:
                    ch_cfg = replace(cfg, logical_channel=lch)
                    ch_recipe = recipe

                ch_pts = self.driver.execute_sweep(
                    cfg=ch_cfg,
                    recipe=ch_recipe,
                    node_ids=[lch.node_id],
                )
                for p in ch_pts:
                    p.node_id = lch.node_id
                    p.channel = lch.channel_name
                combined_points.extend(ch_pts)
            raw_points = combined_points
        else:
            # Execute primary driver sweep for Node 1..N
            raw_points = self.driver.execute_sweep(cfg=cfg, recipe=recipe, node_ids=active_node_ids)

            # If multi-channel model (e.g. 2602B smua+smub or 2606B 4-CH) is simulated, expand per enabled channel
            if isinstance(self.driver.transport, VirtualSMUTransport) and len(active_channels) > len(active_node_ids):
                expanded: list[ReadingPoint] = []
                pts_per_node = [p for p in raw_points if p.node_id == active_node_ids[0]]
                for p in pts_per_node:
                    for lch in active_channels:
                        scale = 0.05 if lch.role == ChannelRole.FET_GATE.value else 1.0
                        expanded.append(
                            replace(
                                p,
                                node_id=lch.node_id,
                                channel=lch.channel_name,
                                current_a=p.current_a * scale,
                                power_w=p.voltage_v * (p.current_a * scale),
                            )
                        )
                raw_points = expanded

        # Filter out steps where `measure_enable == False` in custom LIST waveforms
        if recipe.sweep_type == SweepType.LIST and recipe.custom_steps:
            enabled_indices = {s.step_index for s in recipe.custom_steps if s.measure_enable}
            step_cnt = len(recipe.custom_steps)
            raw_points = [
                p for p in raw_points if (((p.index - 1) % step_cnt) + 1) in enabled_indices
            ]

        # Deliver real-time chunk callbacks if in LOW_SPEED_STREAMING mode
        if streaming_callback is not None:
            if recipe.acquisition_mode == AcquisitionMode.LOW_SPEED_STREAMING:
                chunk_sz = max(1, len(raw_points) // 5)
                for i in range(0, len(raw_points), chunk_sz):
                    streaming_callback(raw_points[: i + chunk_sz])
            else:
                # HIGH_SPEED_BURST delivers full buffer burst at end of cycle
                streaming_callback(raw_points)

        return raw_points
