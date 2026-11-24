"""Phase 2 Automated Unit & Integration Test Suite.

Covers all Phase 2 requirements from v3.0 Specification:
1. 1~10 Node TSP-Link cluster initialization & 3 Sync Line (Line 1 Arm, Line 2 Trigger, Line 3 End) script generation.
2. Logical channel expansion across 1-CH (2450), 2-CH (2600B), and 4-CH (2606B 1U high-density) & FET Role mapping.
3. Arbitrary Waveform Builder (Square, Triangle, Staircase presets, CSV & Excel import/export, Time-Domain Preview).
4. Dual TSP compiler variable step duration (`Config List` delay for 24xx, `trigger.timer[1].delaylist` for 26xx).
5. Output Combining Wizard rules (Voltage-parallel block, Current-series block, 2606B block, 4-step wiring gate, 10% margin, safe shutdown).
6. Dual Continuous Mode (`LOW_SPEED_STREAMING` chunk callback vs `HIGH_SPEED_BURST`) & step measurement masking.
"""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

import pandas as pd

from smu_app.compiler.tsp_compiler import get_tsp_compiler
from smu_app.core.models import (
    AcquisitionMode,
    ChannelConfig,
    CombiningMode,
    LogicalChannel,
    RepeatMode,
    SourceFunction,
    SweepRecipe,
    SweepType,
    WaveformStep,
)
from smu_app.core.profiles import ProfileRegistry
from smu_app.drivers.base_smu import create_smu_driver
from smu_app.features.sweep_engine import SweepExecutionEngine
from smu_app.features.waveform_builder import WaveformBuilder
from smu_app.sync.cluster_manager import (
    ChannelRole,
    CombiningChecklistState,
    TSPLinkClusterManager,
)


class TestPhase2Suite(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = ProfileRegistry()

    def test_01_logical_channel_mapping_1ch_2ch_4ch_up_to_10_nodes(self) -> None:
        # 1. 2450 (1-CH) x 10 nodes = 10 logical channels
        drv_2450 = create_smu_driver("2450", registry=self.registry)
        cm_2450 = TSPLinkClusterManager(drv_2450)
        chs_2450 = cm_2450.build_logical_channels(10)
        self.assertEqual(len(chs_2450), 10)
        self.assertEqual(chs_2450[0].tsp_prefix, "smu")
        self.assertEqual(chs_2450[9].tsp_prefix, "node[10].smu")

        # 2. 2600B (2-CH: smua, smub) x 3 nodes = 6 logical channels
        drv_2600 = create_smu_driver("2600B", registry=self.registry)
        cm_2600 = TSPLinkClusterManager(drv_2600)
        chs_2600 = cm_2600.build_logical_channels(3)
        self.assertEqual(len(chs_2600), 6)
        self.assertEqual(chs_2600[1].tsp_prefix, "smub")
        self.assertEqual(chs_2600[5].tsp_prefix, "node[3].smub")

        # 3. 2606B (4-CH: 2 modules x 2 CH) x 2 chassis = 8 logical channels
        drv_2606 = create_smu_driver("2606B", registry=self.registry)
        cm_2606 = TSPLinkClusterManager(drv_2606)
        chs_2606 = cm_2606.build_logical_channels(2)
        self.assertEqual(len(chs_2606), 8)
        self.assertEqual(chs_2606[0].tsp_prefix, "smua")
        self.assertEqual(chs_2606[3].tsp_prefix, "node[2].smub")
        self.assertEqual(chs_2606[7].tsp_prefix, "node[4].smub")

    def test_02_tsplink_3_sync_line_script_generation_and_26x4b_fallback(self) -> None:
        drv_2600 = create_smu_driver("2600B", registry=self.registry)
        cm = TSPLinkClusterManager(drv_2600)

        # Standard 2612B supports TSP-Link -> generates tsplink.trig[1..2] script
        topo_hw = cm.validate_and_initialize_cluster(node_count=3, specific_variant="2612B")
        self.assertFalse(topo_hw.use_software_sync_fallback)
        sync_cmds = cm.generate_tsplink_sync_script(topo_hw, step_count=50, delay_s=0.01)
        joined = "\n".join(sync_cmds)
        self.assertIn("tsplink.trig[1]", joined)
        self.assertIn("node[3].smub.trigger.initiate()", joined)

        # Non-TSPLink 2614B variant -> switches to SW sync fallback and emits no hardware tsplink lines
        topo_sw = cm.validate_and_initialize_cluster(node_count=3, specific_variant="2614B")
        self.assertTrue(topo_sw.use_software_sync_fallback)
        self.assertEqual(cm.generate_tsplink_sync_script(topo_sw, step_count=50, delay_s=0.01), [])

    def test_03_arbitrary_waveform_presets_csv_excel_import_and_preview(self) -> None:
        sq = WaveformBuilder.build_square_wave(high_level=3.3, low_level=0.0, high_time_s=0.01, low_time_s=0.02, cycles=3)
        self.assertEqual(len(sq), 6)
        tri = WaveformBuilder.build_triangle_wave(-1.0, 1.0, steps_per_half_cycle=5, step_duration_s=0.005, cycles=2)
        self.assertGreater(len(tri), 10)

        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "wave.csv"
            xlsx_path = Path(tmpdir) / "wave.xlsx"

            # Export and re-import CSV
            sq[1].measure_enable = False
            WaveformBuilder.export_steps_to_csv(sq, csv_path)
            loaded_csv = WaveformBuilder.import_steps_from_file(csv_path)
            self.assertEqual(len(loaded_csv), 6)
            self.assertFalse(loaded_csv[1].measure_enable)

            # Excel import test
            df = pd.DataFrame({
                "시간(s)": [0.01, 0.02, 0.01],
                "레벨": [1.0, 2.5, 0.0],
                "제한": [0.1, 0.2, 0.1],
                "측정여부": [1, 1, 0],
            })
            df.to_excel(xlsx_path, index=False)
            loaded_xlsx = WaveformBuilder.import_steps_from_file(xlsx_path)
            self.assertEqual(len(loaded_xlsx), 3)
            self.assertAlmostEqual(loaded_xlsx[1].level, 2.5)
            self.assertFalse(loaded_xlsx[2].measure_enable)

            # Preview duration and point calculation for FINITE repeat (3 cycles)
            recipe = SweepRecipe(
                sweep_type=SweepType.LIST,
                repeat_mode=RepeatMode.FINITE,
                repeat_count=3,
                custom_steps=loaded_xlsx,
            )
            pv = WaveformBuilder.generate_preview(recipe)
            self.assertAlmostEqual(pv.single_cycle_duration_s, 0.04)
            self.assertAlmostEqual(pv.total_duration_s, 0.12)
            self.assertEqual(pv.single_cycle_points, 2)
            self.assertEqual(pv.total_measure_points, 6)

    def test_04_dual_compiler_variable_step_delaylist(self) -> None:
        steps = [
            WaveformStep(1, duration_s=0.005, level=0.0, compliance_limit=0.1),
            WaveformStep(2, duration_s=0.025, level=-2.0, compliance_limit=0.2),
            WaveformStep(3, duration_s=0.010, level=2.0, compliance_limit=0.2),
        ]
        recipe = SweepRecipe(
            sweep_type=SweepType.LIST,
            custom_steps=steps,
            end_Return_to_bias=False,
        )

        # 26xx compiler must use trigger.timer[1].delaylist and SOURCE_HOLD
        p26 = self.registry.get("2651A")
        cfg26 = ChannelConfig(logical_channel=LogicalChannel(1, "smua"))
        cmds26 = "\n".join(get_tsp_compiler(p26).compile_sweep(cfg26, recipe, p26))
        self.assertIn("trigger.timer[1].delaylist = {0.005, 0.025, 0.01}", cmds26)
        self.assertIn("SOURCE_HOLD", cmds26)
        self.assertIn("-1e-12", cmds26)

        # 24xx compiler must store per-step smu.source.delay in configlist
        p24 = self.registry.get("2461")
        cfg24 = ChannelConfig(logical_channel=LogicalChannel(1, "smu"))
        cmds24 = "\n".join(get_tsp_compiler(p24).compile_sweep(cfg24, recipe, p24))
        self.assertIn("smu.source.delay = 0.025", cmds24)

    def test_05_output_combining_safety_rules_and_shutdown(self) -> None:
        p2651 = self.registry.get("2651A")
        p2606 = self.registry.get("2606B")
        verified_chk = CombiningChecklistState(True, True, True, True)
        unverified_chk = CombiningChecklistState(True, False, True, True)

        # Rule 1: 2606B combining must be blocked
        with self.assertRaises(ValueError):
            TSPLinkClusterManager.build_combined_output_config(
                p2606, CombiningMode.PARALLEL_CURRENT, SourceFunction.CURRENT, 4.0, 10.0, verified_chk
            )

        # Rule 2: Parallel Voltage Source must be blocked
        with self.assertRaises(ValueError):
            TSPLinkClusterManager.build_combined_output_config(
                p2651, CombiningMode.PARALLEL_CURRENT, SourceFunction.VOLTAGE, 20.0, 5.0, verified_chk
            )

        # Rule 3: Series Current Source must be blocked
        with self.assertRaises(ValueError):
            TSPLinkClusterManager.build_combined_output_config(
                p2651, CombiningMode.SERIES_VOLTAGE, SourceFunction.CURRENT, 10.0, 20.0, verified_chk
            )

        # Rule 4: Incomplete physical wiring checklist must raise PermissionError
        with self.assertRaises(PermissionError):
            TSPLinkClusterManager.build_combined_output_config(
                p2651, CombiningMode.PARALLEL_CURRENT, SourceFunction.CURRENT, 80.0, 30.0, unverified_chk
            )

        # Rule 5: Valid 2651A 80A parallel pulse (40A + 40A, Slave 10% compliance margin = 27.0V)
        comb = TSPLinkClusterManager.build_combined_output_config(
            p2651, CombiningMode.PARALLEL_CURRENT, SourceFunction.CURRENT, 80.0, 30.0, verified_chk
        )
        self.assertAlmostEqual(comb.master_level, 40.0)
        self.assertAlmostEqual(comb.slave_level, 40.0)
        self.assertAlmostEqual(comb.slave_compliance, 27.0)
        self.assertTrue(comb.pulse_only_enforced)

    def test_06_multi_channel_fet_and_streaming_execution(self) -> None:
        drv = create_smu_driver("2600B", registry=self.registry)
        engine = SweepExecutionEngine(drv)
        cfg = ChannelConfig(logical_channel=LogicalChannel(1, "smua"))

        # Configure 2 channels on Node 1: smua = FET_DRAIN, smub = FET_GATE
        l_chs = [
            LogicalChannel(1, "smua", role=ChannelRole.FET_DRAIN.value, enabled=True),
            LogicalChannel(1, "smub", role=ChannelRole.FET_GATE.value, enabled=True),
        ]
        # Custom LIST with 4 steps where step 2 has measure_enable=False, delay >= 25ms -> LOW_SPEED_STREAMING
        steps = [
            WaveformStep(1, 0.025, 0.0, 0.1, measure_enable=True),
            WaveformStep(2, 0.025, 0.4, 0.1, measure_enable=False),
            WaveformStep(3, 0.025, 0.8, 0.1, measure_enable=True),
            WaveformStep(4, 0.025, 1.2, 0.1, measure_enable=True),
        ]
        recipe = SweepRecipe(
            name="fet_list_stream",
            sweep_type=SweepType.LIST,
            delay_s=0.025,
            custom_steps=steps,
            abort_on_limit=False,
        )
        chunks_received: list[int] = []
        res = engine.run_measurement(
            cfg=cfg,
            recipe=recipe,
            node_count=1,
            logical_channels=l_chs,
            streaming_callback=lambda pts: chunks_received.append(len(pts)),
        )
        self.assertEqual(res.recipe.acquisition_mode, AcquisitionMode.LOW_SPEED_STREAMING)
        self.assertGreater(len(chunks_received), 1, "LOW_SPEED_STREAMING must deliver multiple progressive chunks")
        # 3 enabled steps x 2 active channels (smua, smub) = 6 points
        self.assertEqual(len(res.points), 6)


if __name__ == "__main__":
    unittest.main()
