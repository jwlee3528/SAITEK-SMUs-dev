"""Phase 3 Automated Unit & Integration Test Suite.

Covers all Phase 3 requirements from v3.0 Specification:
1. High-Speed Pulse & 1 MS/s Digitizer TSP compilation for 2461 (10A/150us + digitizer),
   2601B-PULSE (10A/10V 10us pulser + dual 18-bit digitizer + 3,500-pt burst plan + inductance),
   and 2651A (50A/300us + Fast ADC + Sink Mode).
2. Keithley 2461 Autocalibration (ACAL) temperature drift (+/-5°C) monitoring and remote `acal.run()` execution.
3. Thermal (>30°C) & Sink (Quadrant II/IV) maximum duty cycle derating formula & SOA boundary coordinate generation.
4. High-speed 1 MS/s pulse transient waveform simulation & automated extraction of Rise Time (10-90%),
   Fall Time (90-10%), Settling Time, Overshoot (%), and Pulse Top V/I.
5. Dual Cursor analysis calculator (ΔX, ΔY, Slope, Differential Resistance 1/Slope).
6. Multi-Run History comparison Excel (.xlsx) workbook, SQLite (.db) archive, and PNG/SVG plot image export.
"""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
import numpy as np

from smu_app.analysis.iv_pulse_analyzer import (
    analyze_measurement_points,
    compute_dual_cursor_metrics,
)
from smu_app.core.models import (
    ChannelConfig,
    LogicalChannel,
    SourceFunction,
    SweepRecipe,
    SweepType,
)
from smu_app.core.profiles import ProfileRegistry
from smu_app.drivers.base_smu import create_smu_driver
from smu_app.features.pulse_digitizer import (
    HighSpeedPulseDigitizerEngine,
    PulseDigitizerConfig,
)
from smu_app.features.sweep_engine import SweepExecutionEngine
from smu_app.safety.safety_manager import SafetyAndSOAValidator
from smu_app.storage.exporter import DataExporter


class TestPhase3Suite(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = ProfileRegistry()

    def test_01_high_speed_pulse_and_digitizer_tsp_compilation_2461_2601b_2651a(self) -> None:
        p_cfg = PulseDigitizerConfig(
            bias_level=0.0,
            pulse_level=-8.0,
            pulse_width_s=0.0002,   # 200 us
            pulse_period_s=0.010,   # 10 ms (2% duty)
            pulse_count=10,
            digitize_enable=True,
            sample_rate_sps=1000000,
            load_inductance_uh=1.5,
            fast_adc_mode=True,
            sink_mode_enable=True,
        )
        recipe = SweepRecipe(
            sweep_type=SweepType.PULSE_TRAIN,
            source_func=SourceFunction.CURRENT,
            start_level=0.0,
            stop_level=-8.0,
            compliance_limit=10.0,
            pulse_width_s=0.0002,
            pulse_period_s=0.010,
        )

        # 1. 2461 TSP script check
        p2461 = self.registry.get("2461")
        cfg24 = ChannelConfig(logical_channel=LogicalChannel(1, "smu"))
        cmds2461 = "\n".join(HighSpeedPulseDigitizerEngine.compile_pulse_digitizer_tsp(p2461, cfg24, recipe, p_cfg))
        self.assertIn("smu.FUNC_DIGITIZE_VOLTAGE", cmds2461)
        self.assertIn("smu.digitize.samplerate = 1000000", cmds2461)
        self.assertIn("smu.source.pulsetraini", cmds2461)

        # 2. 2601B-PULSE TSP script & 3,500-pt burst plan check
        p2601p = self.registry.get("2601B-PULSE")
        cfg26 = ChannelConfig(logical_channel=LogicalChannel(1, "smua"))
        samples_per_pulse, pulses_per_burst, num_bursts = HighSpeedPulseDigitizerEngine.compute_burst_plan(p2601p, p_cfg)
        self.assertEqual(samples_per_pulse, 200 + 20 + 40)  # 260 pts/pulse
        self.assertLessEqual(samples_per_pulse * pulses_per_burst, 3500)
        cmds2601 = "\n".join(HighSpeedPulseDigitizerEngine.compile_pulse_digitizer_tsp(p2601p, cfg26, recipe, p_cfg))
        self.assertIn("smua.pulser.enable = smua.ENABLE", cmds2601)
        self.assertIn("smua.pulser.measure.digiv(smua.nvbuffer1, smua.nvbuffer2)", cmds2601)
        self.assertIn("-1e-12", cmds2601)

        # 3. 2651A Fast ADC & Sink Mode check
        p2651 = self.registry.get("2651A")
        cmds2651 = "\n".join(HighSpeedPulseDigitizerEngine.compile_pulse_digitizer_tsp(p2651, cfg26, recipe, p_cfg))
        self.assertIn("smua.ADC_FAST", cmds2651)
        self.assertIn("smua.source.sink = smua.ENABLE", cmds2651)

    def test_02_2461_acal_temperature_drift_monitoring_and_execution(self) -> None:
        p2461 = self.registry.get("2461")
        p2450 = self.registry.get("2450")
        p_cfg = PulseDigitizerConfig(ambient_temp_c=29.5, last_acal_temp_c=23.0)  # ΔT = 6.5°C >= 5.0°C

        # Non-2461 model does not support ACAL
        st_2450 = HighSpeedPulseDigitizerEngine.check_and_run_2461_acal(p2450, p_cfg)
        self.assertFalse(st_2450.supported)

        # 2461 recommends ACAL when ΔT >= 5°C
        st_warn = HighSpeedPulseDigitizerEngine.check_and_run_2461_acal(p2461, p_cfg, execute_now=False)
        self.assertTrue(st_warn.supported)
        self.assertTrue(st_warn.acal_recommended)

        # Executing remote ACAL emits acal.run() and resets drift
        st_exec = HighSpeedPulseDigitizerEngine.check_and_run_2461_acal(p2461, p_cfg, execute_now=True)
        self.assertFalse(st_exec.acal_recommended)
        self.assertIn("acal.run()", st_exec.executed_commands)

    def test_03_thermal_and_sink_duty_cycle_derating_and_soa_boundaries(self) -> None:
        p2461 = self.registry.get("2461")  # Base max_duty_cycle = 5% (0.05)
        duty_nominal = SafetyAndSOAValidator.compute_thermal_and_sink_max_duty(
            p2461, peak_v=50.0, peak_i=10.0, ambient_temp_c=25.0, is_sink_quadrant=False
        )
        self.assertAlmostEqual(duty_nominal, 0.05)

        # At 45°C (+15°C above 30°C -> 30% thermal derating) + Sink mode (40% sink derating) -> 0.05 * 0.70 * 0.60 = 0.021
        duty_derated = SafetyAndSOAValidator.compute_thermal_and_sink_max_duty(
            p2461, peak_v=50.0, peak_i=10.0, ambient_temp_c=45.0, is_sink_quadrant=True
        )
        self.assertAlmostEqual(duty_derated, 0.021)

        # A 3.5% duty pulse passes at 25°C but fails at 45°C + Sink mode
        cfg = ChannelConfig()
        recipe = SweepRecipe(
            sweep_type=SweepType.PULSE_TRAIN,
            source_func=SourceFunction.CURRENT,
            start_level=0.0,
            stop_level=10.0,
            compliance_limit=50.0,
            pulse_width_s=0.00035,
            pulse_period_s=0.010,  # 3.5% duty
        )
        rep_cool = SafetyAndSOAValidator.validate_soa_and_recipe(
            p2461, cfg, recipe, ambient_temp_c=25.0, is_sink_quadrant=False
        )
        self.assertTrue(rep_cool.passed)

        rep_hot_sink = SafetyAndSOAValidator.validate_soa_and_recipe(
            p2461, cfg, recipe, ambient_temp_c=45.0, is_sink_quadrant=True
        )
        self.assertFalse(rep_hot_sink.passed)

        # SOA boundary curve generation check
        curves = SafetyAndSOAValidator.get_soa_boundary_curves(p2461)
        self.assertIn("DC", curves)
        self.assertIn("PULSE", curves)

    def test_04_digitized_pulse_transient_metrics_and_dual_cursors(self) -> None:
        drv = create_smu_driver("2601B-PULSE", registry=self.registry)
        engine = SweepExecutionEngine(drv)
        cfg = ChannelConfig(logical_channel=LogicalChannel(1, "smua"))
        p_cfg = PulseDigitizerConfig(
            bias_level=0.0,
            pulse_level=5.0,
            pulse_width_s=0.0001,   # 100 us
            pulse_period_s=0.0020,  # 2 ms (5% duty)
            pulse_count=1,
            digitize_enable=True,
            sample_rate_sps=1000000,
            load_inductance_uh=1.2,
        )
        recipe = SweepRecipe(
            name="pulser_1msps_capture",
            sweep_type=SweepType.PULSE_TRAIN,
            source_func=SourceFunction.CURRENT,
            start_level=0.0,
            stop_level=5.0,
            compliance_limit=10.0,
            pulse_width_s=0.0001,
            pulse_period_s=0.0020,
            use_digitizer=True,
        )
        res = engine.run_measurement(cfg=cfg, recipe=recipe, pulse_config=p_cfg)
        self.assertEqual(len(res.points), 100 + 20 + 40)  # 160 digitized samples at 1us intervals
        m = res.metrics
        self.assertIsNotNone(m.get("rise_time_10_90_s"))
        self.assertIsNotNone(m.get("fall_time_90_10_s"))
        self.assertIsNotNone(m.get("settling_time_s"))
        self.assertGreater(m.get("overshoot_pct", 0.0), 1.0)
        self.assertAlmostEqual(m.get("pulse_top_i", 0.0), 5.0, delta=0.25)

        # Dual cursor calculation test on linear resistor I = V / 100
        vx = np.linspace(0.0, 10.0, 101)
        iy = vx / 100.0
        cur = compute_dual_cursor_metrics(vx, iy, 2.0, 7.0)
        self.assertAlmostEqual(cur.delta_x, 5.0)
        self.assertAlmostEqual(cur.delta_y, 0.05)
        self.assertAlmostEqual(cur.inverse_slope_ohm, 100.0)

    def test_05_phase3_exports_history_comparison_sqlite_and_images(self) -> None:
        drv = create_smu_driver("2461", registry=self.registry)
        engine = SweepExecutionEngine(drv)
        cfg = ChannelConfig(logical_channel=LogicalChannel(1, "smu"))

        r1 = engine.run_measurement(cfg, SweepRecipe(name="run1", start_level=0.0, stop_level=1.0, points=21, abort_on_limit=False))
        r2 = engine.run_measurement(cfg, SweepRecipe(name="run2", start_level=0.0, stop_level=1.2, points=21, abort_on_limit=False))

        with tempfile.TemporaryDirectory() as tmpdir:
            exporter = DataExporter(base_output_dir=tmpdir)
            hist_xlsx = exporter.export_run_history_comparison_excel([r1, r2])
            db_path = exporter.export_sqlite_archive(r2)
            png_path = exporter.export_plot_image(r2, fmt="png", mode="IV", history_overlays=[r1])
            svg_path = exporter.export_plot_image(r2, fmt="svg", mode="TIME", history_overlays=[r1])

            self.assertTrue(hist_xlsx.exists() and hist_xlsx.stat().st_size > 0)
            self.assertTrue(db_path.exists() and db_path.stat().st_size > 0)
            self.assertTrue(png_path.exists() and png_path.stat().st_size > 0)
            self.assertTrue(svg_path.exists() and svg_path.stat().st_size > 0)


if __name__ == "__main__":
    unittest.main()
