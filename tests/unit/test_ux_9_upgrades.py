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

        # 2. Execute Full Linear Sweep Measurement
        win.cmb_sweep_type.setCurrentText("LINEAR")
        win.spn_points.setValue(21)
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

    def test_req11_persistent_export_folder_and_tek_quickstart_excel_report_format(self) -> None:
        import openpyxl

        orig_dir = SetupPersistenceManager.get_saved_export_dir()
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                custom_export_dir = Path(tmpdir) / "user_chosen_reports"
                win = SMUMainWindow(show_startup_wizard=False, prefer_simulator=True)
                win.suppress_range_popup = True

                # 1. User sets custom export folder -> must persist across new window/session instances
                win.set_export_directory(custom_export_dir, persist=True)
                self.assertEqual(SetupPersistenceManager.get_saved_export_dir(), custom_export_dir.resolve())
                self.assertEqual(win.txt_export_dir.text(), str(custom_export_dir.resolve()))

                win2 = SMUMainWindow(show_startup_wizard=False, prefer_simulator=True)
                self.assertEqual(win2.exporter.output_dir, custom_export_dir.resolve())

                # 2. Execute measurement & export Tek QuickStart formatted Excel report
                win.execute_measurement()
                assert win.last_result is not None
                xlsx_path = win.exporter.export_excel_report(
                    win.last_result,
                    resource_addr="USB0::0x05E6::0x2450::04584348::INSTR",
                    is_simulation=False,
                    idn_string="KEITHLEY INSTRUMENTS,MODEL 2450,04584348,1.7.12",
                )
                self.assertTrue(xlsx_path.exists())
                self.assertEqual(xlsx_path.parent, custom_export_dir.resolve())

                wb = openpyxl.load_workbook(xlsx_path, data_only=True)
                expected_sheets = [
                    "1_Summary_Checklist",
                    "2_Input_Configuration",
                    "3_Measured_Data",
                    "4_Waveform_Trace",
                    "5_Graph_Report",
                    "6_Auto_Diagnosis",
                ]
                self.assertEqual(wb.sheetnames, expected_sheets)

                # Verify Checklist rows in 1_Summary_Checklist
                ws1 = wb["1_Summary_Checklist"]
                col_a_vals = [str(ws1.cell(r, 1).value or "") for r in range(1, ws1.max_row + 1)]
                self.assertTrue(any("리모트 준비 사항 항목" in v for v in col_a_vals))
                self.assertTrue(any("1단계:" in v for v in col_a_vals))

                # Verify Statistics rows (평균 Mean, 최댓값 Max, 최솟값 Min, 표준편차 StdDev) in 3_Measured_Data
                ws3 = wb["3_Measured_Data"]
                col_a_data = [str(ws3.cell(r, 1).value or "") for r in range(1, ws3.max_row + 1)]
                self.assertIn("평균 (Mean)", col_a_data)
                self.assertIn("최댓값 (Max)", col_a_data)
                self.assertIn("최솟값 (Min)", col_a_data)
                self.assertIn("표준편차 (StdDev)", col_a_data)
        finally:
            SetupPersistenceManager.set_saved_export_dir(orig_dir)

    def test_req12_autozero_and_source_readback_compiler_ui_and_persistence(self) -> None:
        from smu_app.compiler.tsp_compiler import TSP24xxGenerator, TSP26xxGenerator
        from smu_app.core.models import ChannelConfig, LogicalChannel
        from smu_app.core.profiles import ProfileRegistry

        reg = ProfileRegistry()
        p2450 = reg.get("2450")
        p2600 = reg.get("2600B")

        # 1. Verify TSP24xxGenerator emits autozero.once() and readback=OFF properly
        cfg_once = ChannelConfig(
            logical_channel=LogicalChannel(1, "smu"),
            auto_zero="ONCE",
            source_readback=False,
        )
        cmds_24 = TSP24xxGenerator().compile_channel_setup(cfg_once, p2450)
        self.assertIn("smu.measure.autozero.enable = smu.OFF", cmds_24)
        self.assertIn("smu.measure.autozero.once()", cmds_24)
        self.assertIn("smu.source.readback = smu.OFF", cmds_24)

        # 2. Verify TSP26xxGenerator emits AUTOZERO_ONCE
        cfg_26 = ChannelConfig(
            logical_channel=LogicalChannel(1, "smua"),
            auto_zero="ONCE",
            source_readback=False,
        )
        cmds_26 = TSP26xxGenerator().compile_channel_setup(cfg_26, p2600)
        self.assertIn("smua.measure.autozero = smua.AUTOZERO_ONCE", cmds_26)

        # 3. Verify GUI controls, ChannelConfig propagation, and Setup save/load roundtrip
        with tempfile.TemporaryDirectory() as tmpdir:
            setup_file = Path(tmpdir) / "az_rb_setup.json"
            win = SMUMainWindow(show_startup_wizard=False, prefer_simulator=True)
            win.suppress_range_popup = True
            win.cmb_auto_zero.setCurrentText("ONCE")
            win.chk_source_readback.setChecked(False)
            win.execute_measurement()
            assert win.last_result is not None
            self.assertEqual(win.last_result.channel_config.auto_zero, "ONCE")
            self.assertFalse(win.last_result.channel_config.source_readback)

            win.save_setup_to_path(setup_file, silent=True)
            win.cmb_auto_zero.setCurrentText("ON")
            win.chk_source_readback.setChecked(True)
            win.load_setup_from_path(setup_file, silent=True)
            self.assertEqual(win.cmb_auto_zero.currentText(), "ONCE")
            self.assertFalse(win.chk_source_readback.isChecked())

            # 4. Verify measurement time estimator formula & HTML guide reflects current UI inputs
            win.cmb_sweep_type.setCurrentText("LINEAR")
            win.spn_points.setValue(50)
            win.spn_delay.setValue(0.1)
            win.spn_nplc.setValue(0.1)
            win.cmb_auto_zero.setCurrentText("ON")
            win.chk_source_readback.setChecked(True)
            html_on, stats_on = win.build_measurement_time_guide_html()
            self.assertIn("49", html_on)
            self.assertAlmostEqual(stats_on["ideal_elapsed_s"], 4.9, places=3)
            self.assertGreater(stats_on["est_mid_s"], 6.0)

            win.cmb_auto_zero.setCurrentText("ONCE")
            win.chk_source_readback.setChecked(False)
            _, stats_fast = win.build_measurement_time_guide_html()
            self.assertLess(stats_fast["est_mid_s"], stats_on["est_mid_s"])
            self.assertLess(stats_fast["est_mid_s"], 5.5)

    def test_req13_mixed_model_mode_a_tsplink_and_mode_b_multi_visa_sw(self) -> None:
        from smu_app.core.models import LogicalChannel, MultiUnitSyncMode
        from smu_app.sync.cluster_manager import TSPLinkClusterManager
        from smu_app.ui.multi_channel_dialog import MultiChannelConfigDialog

        # 1. Mode A (TSP-Link < 1us): 2450 + 2461 (same 24xx family) is VALID
        ch_2450_2461 = [
            LogicalChannel(node_id=1, channel_name="smu", model_name="2450", enabled=True, visa_resource="SIM::2450::INSTR"),
            LogicalChannel(node_id=2, channel_name="smu", model_name="2461", enabled=True, visa_resource="SIM::2461::INSTR"),
        ]
        ok_a, errs_a, notes_a = TSPLinkClusterManager.validate_mixed_model_compatibility(
            ch_2450_2461, MultiUnitSyncMode.TSPLINK_HW, master_model="2450"
        )
        self.assertTrue(ok_a)
        self.assertEqual(errs_a, [])
        self.assertTrue(any("TSP-Link" in n and "2450" in n and "2461" in n for n in notes_a))

        # 2. Mode A (TSP-Link): 2450 + 2600B (cross-generation) is BLOCKED with clear guidance
        ch_cross = [
            LogicalChannel(node_id=1, channel_name="smu", model_name="2450", enabled=True, visa_resource="SIM::2450::INSTR"),
            LogicalChannel(node_id=2, channel_name="smua", model_name="2600B", enabled=True, visa_resource="SIM::2600B::INSTR"),
        ]
        ok_cross_a, errs_cross_a, _ = TSPLinkClusterManager.validate_mixed_model_compatibility(
            ch_cross, MultiUnitSyncMode.TSPLINK_HW, master_model="2450"
        )
        self.assertFalse(ok_cross_a)
        self.assertTrue(any("방식 B" in e for e in errs_cross_a))

        # 3. Mode B (Multi-VISA USB/LAN SW): 2450 + 2600B is ALLOWED & executes both units cleanly
        ok_cross_b, _, _ = TSPLinkClusterManager.validate_mixed_model_compatibility(
            ch_cross, MultiUnitSyncMode.MULTI_VISA_SW, master_model="2450"
        )
        self.assertTrue(ok_cross_b)

        win = SMUMainWindow(show_startup_wizard=False, prefer_simulator=True)
        win.suppress_range_popup = True
        win.cmb_sweep_type.setCurrentText("LINEAR")
        win.spn_points.setValue(11)
        win.spn_nodes.setValue(2)
        win.logical_channels = ch_cross
        win.multi_sync_mode = MultiUnitSyncMode.MULTI_VISA_SW
        win.execute_measurement()
        assert win.last_result is not None
        node_ids = {p.node_id for p in win.last_result.points}
        self.assertEqual(node_ids, {1, 2})

        # 4. Verify MultiChannelConfigDialog preset, Mode A/B toggle, per-node *IDN? test & Setup persistence roundtrip
        dlg = MultiChannelConfigDialog(
            profile=win.driver.profile,
            logical_channels=ch_2450_2461,
            sync_mode=MultiUnitSyncMode.TSPLINK_HW,
            registry=win.registry,
            available_visa_resources=["SIM::2450::INSTR", "SIM::2461::INSTR"],
            visa_mgr=win.visa_mgr,
        )
        dlg._preset_2450_plus_2461()
        self.assertEqual([c.model_name for c in dlg.result_channels], ["2450", "2461"])
        dlg.rad_mode_b.setChecked(True)
        self.assertEqual(dlg.result_sync_mode, MultiUnitSyncMode.MULTI_VISA_SW)
        conn_results = dlg.test_all_connections()
        self.assertEqual(len(conn_results), 2)
        self.assertTrue(all(ok for _, ok, _ in conn_results))

        # 5. Verify SMUConnectionWizardDialog Mode B per-node USB/LAN VISA selection & connection test
        wiz = SMUConnectionWizardDialog(
            visa_mgr=win.visa_mgr,
            registry=win.registry,
            initial_model="2450",
            initial_nodes=2,
            initial_sync_mode=MultiUnitSyncMode.MULTI_VISA_SW,
            initial_node_resources=["SIM::2450::INSTR", "SIM::2461::INSTR"],
        )
        self.assertEqual(wiz.selected_sync_mode, MultiUnitSyncMode.MULTI_VISA_SW)
        self.assertTrue(wiz.test_single_node_connection(1))
        self.assertTrue(wiz.test_single_node_connection(2))

        with tempfile.TemporaryDirectory() as tmpdir:
            sf = Path(tmpdir) / "mixed_setup.json"
            win.logical_channels = ch_2450_2461
            win.multi_sync_mode = MultiUnitSyncMode.MULTI_VISA_SW
            win.save_setup_to_path(sf, silent=True)
            win.multi_sync_mode = MultiUnitSyncMode.TSPLINK_HW
            win.load_setup_from_path(sf, silent=True)
            self.assertEqual(win.multi_sync_mode, MultiUnitSyncMode.MULTI_VISA_SW)
            self.assertEqual([c.model_name for c in win.logical_channels], ["2450", "2461"])


if __name__ == "__main__":
    unittest.main()
