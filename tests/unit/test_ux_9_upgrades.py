"""Automated Verification Suite for the 9 User Experience & Functional Upgrades.

Tests:
1. TEK_quickstart-style Beginner Connection Wizard (`SMUConnectionWizardDialog`): multi-node (1~10) per-unit ping test & beginner checklist verification.
2. Arbitrary Waveform Editor step deletion (`WaveformBuilder.delete_steps` & GUI `_delete_selected_waveform_steps`).
3. Easy Parametric Waveform Generator (`WaveformBuilder.build_easy_waveform`): `repeat_count=100` -> FINITE x100; `repeat_count=-1` -> CONTINUOUS infinite mode.
4. Detachable / Pop-out Graph Window (`DetachedGraphWindow`) creation and live data sync.
5. Automatic Screen Resolution Adaptation (`get_available_screen_geometry`, `fit_and_center_window`).
6. Model Reference Manual PDF lookup (`find_manual_pdf_for_model` across all 8 models).
7. Multi-instrument (2+ nodes) independent per-node waveform mode & source function (`VOLTAGE` vs `CURRENT`) execution.
8. Out-of-range input value notification & automatic clamping to model maximum/minimum (`clamp_value_with_notice` & GUI spinbox handler).
9. Full setup save & load persistence (`SetupPersistenceManager.save_full_setup` / `load_full_setup`).
"""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtWidgets import QApplication

from smu_app.core.models import (
    ChannelConfig,
    LogicalChannel,
    RepeatMode,
    SourceFunction,
    SweepRecipe,
    SweepType,
    WaveformStep,
)
from smu_app.core.profiles import ProfileRegistry
from smu_app.core.visa_manager import VISAConnectionManager
from smu_app.drivers.base_smu import create_smu_driver
from smu_app.features.pulse_digitizer import PulseDigitizerConfig
from smu_app.features.sweep_engine import SweepExecutionEngine
from smu_app.features.waveform_builder import (
    EasyWaveformShape,
    WaveformBuilder,
)
from smu_app.storage.setup_manager import (
    SetupPersistenceManager,
    clamp_value_with_notice,
    find_manual_pdf_for_model,
)
from smu_app.ui.connection_wizard import (
    SMUConnectionWizardDialog,
    fit_and_center_window,
)
from smu_app.ui.main_window import SMUMainWindow


_APP = QApplication.instance() or QApplication([])


class TestUX9Upgrades(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = ProfileRegistry()

    def test_req1_connection_wizard_multi_node_test_and_checklist(self) -> None:
        visa_mgr = VISAConnectionManager()
        dlg = SMUConnectionWizardDialog(
            visa_mgr=visa_mgr,
            registry=self.registry,
            initial_model="2461",
            initial_nodes=3,
        )
        # Select simulator resource for deterministic 3-node unit test without touching single physical 2450 USB
        for i in range(dlg.cmb_resource.count()):
            if "SIM::2461::INSTR" in dlg.cmb_resource.itemText(i):
                dlg.cmb_resource.setCurrentIndex(i)
                break
        self.assertEqual(dlg.tbl_nodes.rowCount(), 3)
        self.assertGreaterEqual(len(dlg.step_cards), 5)  # 4 standard + 1 TSP-Link multi-node step
        all_ok = dlg.run_all_verifications()
        self.assertTrue(all_ok)
        self.assertTrue(all(c.checkbox.isChecked() for c in dlg.step_cards))

    def test_req2_and_req3_easy_waveform_generator_and_step_deletion(self) -> None:
        # 1. Square wave x 100 cycles
        res_100 = WaveformBuilder.build_easy_waveform(
            shape=EasyWaveformShape.SQUARE,
            high_level=3.3,
            low_level=0.0,
            duration_s=0.01,
            repeat_count=100,
        )
        self.assertEqual(len(res_100.steps), 2)
        self.assertEqual(res_100.repeat_mode, RepeatMode.FINITE)
        self.assertEqual(res_100.repeat_count, 100)

        # 2. Square wave x -1 -> Infinite Continuous mode
        res_inf = WaveformBuilder.build_easy_waveform(
            shape=EasyWaveformShape.SQUARE,
            high_level=5.0,
            low_level=-5.0,
            duration_s=0.005,
            repeat_count=-1,
        )
        self.assertEqual(res_inf.repeat_mode, RepeatMode.CONTINUOUS)

        # 3. Step deletion and automatic re-indexing
        steps = [
            WaveformStep(1, 0.01, 1.0),
            WaveformStep(2, 0.01, 2.0),
            WaveformStep(3, 0.01, 3.0),
            WaveformStep(4, 0.01, 4.0),
        ]
        after_del = WaveformBuilder.delete_steps(steps, [1, 2])
        self.assertEqual(len(after_del), 2)
        self.assertEqual([s.step_index for s in after_del], [1, 2])
        self.assertEqual([s.level for s in after_del], [1.0, 4.0])

    def test_req4_and_req5_detached_graph_window_and_screen_autofit(self) -> None:
        win = SMUMainWindow(show_startup_wizard=False, prefer_simulator=True)
        win.suppress_range_popup = True
        w, h, _ = fit_and_center_window(win)
        self.assertGreaterEqual(w, 640)
        self.assertGreaterEqual(h, 480)

        win.execute_measurement()
        detached = win.open_detached_graph_window()
        self.assertIsNotNone(detached.result)
        self.assertEqual(len(detached.result.points), len(win.last_result.points))
        detached.close()

    def test_req6_all_8_model_manuals_found(self) -> None:
        for model in ["2450", "2460", "2461", "2470", "2600B", "2601B-PULSE", "2606B", "2651A"]:
            pdf = find_manual_pdf_for_model(model)
            self.assertIsNotNone(pdf, f"PDF manual must exist for {model}")
            assert pdf is not None
            self.assertTrue(pdf.exists())

    def test_req7_multi_node_independent_waveform_and_source_function_separation(self) -> None:
        drv = create_smu_driver("2450", registry=self.registry)
        engine = SweepExecutionEngine(drv)
        cfg = ChannelConfig(logical_channel=LogicalChannel(1, "smu"))

        # Node 1 sources VOLTAGE (0 -> 2V, LINEAR), Node 2 independently sources CURRENT (0 -> 0.05A, DUAL)
        ch1 = LogicalChannel(
            node_id=1,
            channel_name="smu",
            enabled=True,
            independent_override=True,
            override_sweep_type=SweepType.LINEAR.value,
            override_source_func=SourceFunction.VOLTAGE.value,
            override_start_level=0.0,
            override_stop_level=2.0,
            override_compliance=0.1,
        )
        ch2 = LogicalChannel(
            node_id=2,
            channel_name="smu",
            enabled=True,
            independent_override=True,
            override_sweep_type=SweepType.DUAL.value,
            override_source_func=SourceFunction.CURRENT.value,
            override_start_level=0.0,
            override_stop_level=0.05,
            override_compliance=5.0,
        )
        recipe = SweepRecipe(name="multi_ind_test", points=11, abort_on_limit=False)
        res = engine.run_measurement(cfg=cfg, recipe=recipe, node_count=2, logical_channels=[ch1, ch2])

        n1_pts = [p for p in res.points if p.node_id == 1]
        n2_pts = [p for p in res.points if p.node_id == 2]
        self.assertEqual(len(n1_pts), 11)      # LINEAR 11 pts
        self.assertEqual(len(n2_pts), 21)      # DUAL (11 + 10) = 21 pts

    def test_req8_out_of_range_clamping_and_notification(self) -> None:
        # Direct helper test: 2450 max voltage is 200V; entering 550V must clamp to 200V and return notice
        clamped, notice = clamp_value_with_notice(550.0, -200.0, 200.0, "종료 레벨", "2450", "V")
        self.assertAlmostEqual(clamped, 200.0)
        self.assertIsNotNone(notice)
        assert notice is not None
        self.assertIn("200V", notice)

        # GUI spinbox auto-clamping test
        win = SMUMainWindow(show_startup_wizard=False, prefer_simulator=True)
        win.suppress_range_popup = True
        win.cmb_model.setCurrentText("2450")
        win.cmb_source_func.setCurrentText("VOLTAGE")
        win.spn_stop.setValue(999.0)
        win._validate_and_clamp_spinbox(win.spn_stop, "source_level", "종료 레벨")
        self.assertAlmostEqual(win.spn_stop.value(), 200.0)
        self.assertIn("200V", win.last_range_notice)

    def test_req9_full_setup_save_and_load_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            setup_file = Path(tmpdir) / "saved_smu_setup.json"
            win = SMUMainWindow(show_startup_wizard=False, prefer_simulator=True)
            win.suppress_range_popup = True

            win.cmb_model.setCurrentText("2461")
            win.spn_nodes.setValue(3)
            win.spn_easy_high.setValue(4.5)
            win.spn_easy_repeat.setValue(100)
            win.generate_easy_parametric_waveform()
            win.logical_channels[1].independent_override = True
            win.logical_channels[1].override_source_func = "CURRENT"

            win.save_setup_to_path(setup_file, silent=True)
            self.assertTrue(setup_file.exists())

            # Modify window state and then restore from saved file
            win.cmb_model.setCurrentText("2450")
            win.spn_nodes.setValue(1)
            win.load_setup_from_path(setup_file, silent=True)

            self.assertEqual(win.cmb_model.currentText(), "2461")
            self.assertEqual(win.spn_nodes.value(), 3)
            self.assertEqual(win.spn_repeat_count.value(), 100)
            self.assertTrue(win.logical_channels[1].independent_override)
            self.assertEqual(win.logical_channels[1].override_source_func, "CURRENT")

    def test_req10_window_size_never_changes_on_measurement_and_live_table_updates(self) -> None:
        win = SMUMainWindow(show_startup_wizard=False, prefer_simulator=True)
        win.suppress_range_popup = True
        win.resize(1360, 820)
        size_before = (win.width(), win.height())

        # 1. Execute Quick Single DC Source & Measure (2450)
        win.spn_quick_dc_level.setValue(1.5)
        win.execute_quick_single_dc_measurement()
        size_after_dc = (win.width(), win.height())
        self.assertEqual(size_before, size_after_dc, "Window size/resolution must NEVER change after DC measurement")
        self.assertEqual(win.tbl_live_data.rowCount(), 1)
        self.assertIn("전압(V):", win.lbl_live_dmm.text())

        # 2. Execute Full Sweep Measurement
        win.execute_measurement()
        size_after_sweep = (win.width(), win.height())
        self.assertEqual(size_before, size_after_sweep, "Window size/resolution must NEVER change after Sweep measurement")
        self.assertGreater(win.tbl_live_data.rowCount(), 10)

        # 3. Verify Clear Graph & Table button resets everything to empty state
        win.clear_graph_and_table_data()
        self.assertIsNone(win.last_result)
        self.assertEqual(len(win.run_history), 0)
        self.assertEqual(len(win.single_dc_points), 0)
        self.assertEqual(win.tbl_live_data.rowCount(), 0)
        self.assertEqual(win.table.rowCount(), 0)
        self.assertIn("---", win.lbl_live_dmm.text())


if __name__ == "__main__":
    unittest.main()
