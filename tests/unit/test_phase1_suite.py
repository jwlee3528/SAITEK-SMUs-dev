"""Comprehensive Unit & Integration Tests for Phase 1 Foundation (with Phase 2-3 Hooks)."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
import numpy as np

from smu_app.analysis.iv_pulse_analyzer import analyze_measurement_points, lttb_downsample
from smu_app.compiler.tsp_compiler import TSP24xxGenerator, TSP26xxGenerator, get_tsp_compiler
from smu_app.core.command_spy import CommandSpy
from smu_app.core.models import (
    AcquisitionMode,
    ChannelConfig,
    CombiningMode,
    LogicalChannel,
    RepeatMode,
    SenseMode,
    SourceFunction,
    SweepRecipe,
    SweepType,
)
from smu_app.core.profiles import ProfileRegistry
from smu_app.core.visa_manager import (
    VISAConnectionManager,
    build_ieee4882_binary_block,
    parse_ieee4882_binary_block,
)
from smu_app.drivers.base_smu import create_smu_driver
from smu_app.features.sweep_engine import SweepExecutionEngine
from smu_app.safety.safety_manager import SafetyAndSOAValidator
from smu_app.simulator.virtual_smu import VirtualDUTType, VirtualSMUTransport
from smu_app.storage.exporter import DataExporter
from smu_app.sync.cluster_manager import TSPLinkClusterManager


class TestPhase1Foundation(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = ProfileRegistry()
        self.spy = CommandSpy()

    def test_01_all_8_model_profiles_loaded(self) -> None:
        expected = {"2450", "2460", "2461", "2470", "2600B", "2601B-PULSE", "2606B", "2651A"}
        self.assertEqual(set(self.registry.list_models()), expected)
        self.assertEqual(self.registry.resolve_model_key("MODEL 2602B"), "2600B")
        self.assertEqual(self.registry.get("2606B").channels, 4)
        self.assertFalse(self.registry.get("2606B").combining_supported)
        self.assertFalse(self.registry.get("2600B").supports_tsplink_for_variant("2614B"))
        self.assertTrue(self.registry.get("2600B").supports_tsplink_for_variant("2612B"))

    def test_02_dual_tsp_compilers_and_negative_zero_fix(self) -> None:
        p2450 = self.registry.get("2450")
        p2651 = self.registry.get("2651A")
        self.assertIsInstance(get_tsp_compiler(p2450), TSP24xxGenerator)
        self.assertIsInstance(get_tsp_compiler(p2651), TSP26xxGenerator)

        cfg26 = ChannelConfig(logical_channel=LogicalChannel(node_id=1, channel_name="smua"))
        recipe = SweepRecipe(
            sweep_type=SweepType.LINEAR,
            start_level=0.0,
            stop_level=-5.0,
            points=6,
            apply_negative_zero_fix=True,
        )
        cmds = get_tsp_compiler(p2651).compile_sweep(cfg26, recipe, p2651)
        joined = "\n".join(cmds)
        self.assertIn("-1e-12", joined, "26xx 0V -> negative pulse -1e-12 polarity fix must be applied")

    def test_03_24xx_auto_switch_scpi_to_tsp(self) -> None:
        driver = create_smu_driver("2450", spy=self.spy, registry=self.registry)
        assert isinstance(driver.transport, VirtualSMUTransport)
        driver.transport.state.language = "SCPI"
        ok, msg = driver.ensure_tsp_mode(auto_switch=True)
        self.assertTrue(ok)
        self.assertEqual(driver.transport.state.language, "TSP")
        self.assertIn("*LANG TSP", msg)

    def test_04_safety_soa_interlock_and_preflight_validation(self) -> None:
        # 1. Voltage exceeding 2450 200V limit should fail SOA validation
        p2450 = self.registry.get("2450")
        cfg = ChannelConfig()
        over_v_recipe = SweepRecipe(start_level=0.0, stop_level=250.0, compliance_limit=0.1)
        rep = SafetyAndSOAValidator.validate_soa_and_recipe(p2450, cfg, over_v_recipe)
        self.assertFalse(rep.passed)

        # 2. 2470 High Voltage (>42V) with Interlock disengaged must be blocked by Pre-Flight Check
        drv2470 = create_smu_driver("2470", spy=self.spy, registry=self.registry)
        assert isinstance(drv2470.transport, VirtualSMUTransport)
        drv2470.transport.state.interlock_engaged = False
        hv_recipe = SweepRecipe(start_level=0.0, stop_level=500.0, compliance_limit=0.01)
        preflight = SafetyAndSOAValidator.run_preflight_check(drv2470, cfg, hv_recipe)
        self.assertFalse(preflight.passed)
        self.assertTrue(any("인터락" in e for e in preflight.errors))

        # 3. 2651A 50A Pulse with excessive duty cycle (10% > 1% max) must be blocked
        p2651 = self.registry.get("2651A")
        bad_pulse = SweepRecipe(
            sweep_type=SweepType.PULSE_TRAIN,
            source_func=SourceFunction.CURRENT,
            start_level=0.0,
            stop_level=50.0,
            compliance_limit=30.0,
            pulse_width_s=0.0003,
            pulse_period_s=0.001,  # 30% duty > 1% limit
        )
        rep_pulse = SafetyAndSOAValidator.validate_soa_and_recipe(p2651, cfg, bad_pulse)
        self.assertFalse(rep_pulse.passed)

    def test_05_multi_node_cluster_and_combining_gate(self) -> None:
        drv2600 = create_smu_driver("2600B", spy=self.spy, registry=self.registry)
        cluster = TSPLinkClusterManager(drv2600)
        # 2614B non-TSPLink variant warning check
        topo = cluster.validate_and_initialize_cluster(node_count=4, specific_variant="2614B")
        self.assertEqual(len(topo.warnings), 1)
        self.assertIn("SW 동기화 경고", topo.warnings[0])

        # Combining SMU without wiring checklist verification must raise PermissionError
        with self.assertRaises(PermissionError):
            cluster.compute_combined_slave_compliance(
                master_compliance=10.0,
                mode=CombiningMode.PARALLEL_CURRENT,
                profile=drv2600.profile,
                wiring_verified=False,
            )
        slave_lim, _ = cluster.compute_combined_slave_compliance(
            master_compliance=10.0,
            mode=CombiningMode.PARALLEL_CURRENT,
            profile=drv2600.profile,
            wiring_verified=True,
        )
        self.assertAlmostEqual(slave_lim, 9.0)

    def test_06_ieee4882_binary_codec_and_lttb(self) -> None:
        original = [0.1 * i for i in range(100)]
        raw = build_ieee4882_binary_block(original, datatype="d")
        decoded = parse_ieee4882_binary_block(raw, datatype="d")
        np.testing.assert_allclose(original, decoded)

        x = np.linspace(0, 10, 10000)
        y = np.sin(x)
        dx, dy = lttb_downsample(x, y, threshold=500)
        self.assertEqual(len(dx), 500)
        self.assertEqual(len(dy), 500)

    def test_07_end_to_end_sweep_analysis_and_exports(self) -> None:
        driver = create_smu_driver("2461", spy=self.spy, registry=self.registry)
        assert isinstance(driver.transport, VirtualSMUTransport)
        driver.transport.state.dut_type = VirtualDUTType.SILICON_DIODE

        engine = SweepExecutionEngine(driver)
        cfg = ChannelConfig(
            logical_channel=LogicalChannel(node_id=1, channel_name="smu"),
            sense_mode=SenseMode.WIRE_4,
            contact_check_enabled=True,
        )
        recipe = SweepRecipe(
            name="diode_iv_test",
            sweep_type=SweepType.LINEAR,
            start_level=0.0,
            stop_level=1.2,
            points=61,
            delay_s=0.005,
            compliance_limit=0.2,
            abort_on_limit=False,
        )
        result = engine.run_measurement(cfg=cfg, recipe=recipe, node_count=2)
        self.assertEqual(len(result.points), 61 * 2)
        self.assertGreater(result.metrics["compliance_hit_count"], 0)
        self.assertEqual(result.recipe.acquisition_mode, AcquisitionMode.HIGH_SPEED_BURST)
        self.assertIsNotNone(result.metrics.get("v_threshold_v"))
        self.assertIsNotNone(result.metrics.get("dv_di_median_ohm"))

        with tempfile.TemporaryDirectory() as tmpdir:
            exporter = DataExporter(base_output_dir=tmpdir)
            xlsx_file = exporter.export_excel_report(result)
            csv_file = exporter.export_streaming_csv(result)
            pq_file = exporter.export_parquet(result)
            self.assertTrue(xlsx_file.exists() and xlsx_file.stat().st_size > 0)
            self.assertTrue(csv_file.exists() and csv_file.stat().st_size > 0)
            self.assertTrue(pq_file.exists() and pq_file.stat().st_size > 0)


if __name__ == "__main__":
    unittest.main()
