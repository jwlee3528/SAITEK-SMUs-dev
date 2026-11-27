"""Unit Test Suite for Rule-Based 15-Item Auto-Diagnosis Coach Engine,
Model-Aware Adaptive Thresholds, Source Readback Default OFF, 1-Click Auto-Fix, and Excel 6th Sheet.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import sys
import tempfile
import unittest

import openpyxl
from PyQt6.QtWidgets import QApplication

from smu_app.analysis.auto_diagnosis import AutoDiagnosisEngine, get_thresholds_for_profile
from smu_app.core.models import (
    ChannelConfig,
    DiagnosisSeverity,
    MeasurementResult,
    ReadingPoint,
    SenseMode,
    SourceFunction,
    SweepRecipe,
    SweepType,
    TerminalLocation,
)
from smu_app.core.profiles import ProfileRegistry
from smu_app.storage.exporter import DataExporter
from smu_app.ui.main_window import SMUMainWindow

_APP = QApplication.instance() or QApplication(sys.argv)


def _make_result(
    model: str,
    pts: list[ReadingPoint],
    cfg: ChannelConfig | None = None,
    recipe: SweepRecipe | None = None,
) -> MeasurementResult:
    now = datetime.now()
    return MeasurementResult(
        session_id="diag_test",
        model=model,
        recipe_name="diag_recipe",
        started_at=now,
        completed_at=now,
        channel_config=cfg or ChannelConfig(),
        recipe=recipe or SweepRecipe(start_level=0.0, stop_level=5.0, points=len(pts), delay_s=0.01, nplc=1.0),
        points=pts,
    )


class TestAutoDiagnosisSuite(unittest.TestCase):
    def setUp(self) -> None:
        self.reg = ProfileRegistry()
        self.p2450 = self.reg.get("2450")

    def test_01_model_aware_adaptive_thresholds_across_models(self) -> None:
        th_2450 = get_thresholds_for_profile(self.reg.get("2450"))
        th_2461 = get_thresholds_for_profile(self.reg.get("2461"))
        th_2470 = get_thresholds_for_profile(self.reg.get("2470"))
        th_2651a = get_thresholds_for_profile(self.reg.get("2651A"))

        # High-power 50A SMU (2651A) has higher open noise floor & lower short-R threshold than 2450
        self.assertLess(th_2450.open_noise_floor_a, th_2461.open_noise_floor_a)
        self.assertLess(th_2461.open_noise_floor_a, th_2651a.open_noise_floor_a)
        self.assertGreater(th_2470.short_max_resistance_ohm, th_2651a.short_max_resistance_ohm)
        self.assertEqual(th_2450.readback_error_pct, 1.0)

    def test_02_source_readback_default_off_in_model_and_gui(self) -> None:
        self.assertFalse(ChannelConfig().source_readback)
        win = SMUMainWindow(show_startup_wizard=False, prefer_simulator=True)
        try:
            win._reset_input_defaults()
            self.assertFalse(win.chk_source_readback.isChecked())
        finally:
            win.close()
            _APP.processEvents()

    def test_03_cat1_output_and_limit_rules(self) -> None:
        # Compliance trip + Readback error > 1% + Terminal REAR mismatch + Interlock
        pts = [
            ReadingPoint(i + 1, i * 0.01, 0.90, 0.10, 9.0, 0.09, in_compliance=True, commanded_level=1.0)
            for i in range(6)
        ]
        cfg = ChannelConfig(source_readback=True, compliance_limit=0.1)
        res = _make_result("2450", pts, cfg=cfg, recipe=SweepRecipe(compliance_limit=0.1, delay_s=0.01))
        rep = AutoDiagnosisEngine.diagnose(res, self.p2450, interlock_engaged=False, recent_error_messages=["Error 5074 Interlock"])
        by_id = {it.rule_id: it for it in rep.items}
        self.assertEqual(len(rep.items), 15)
        self.assertEqual(by_id["COMPLIANCE_TRIP"].severity, DiagnosisSeverity.CRITICAL)
        self.assertEqual(by_id["READBACK_ERROR"].severity, DiagnosisSeverity.WARNING)
        self.assertEqual(by_id["INTERLOCK_TRIP"].severity, DiagnosisSeverity.CRITICAL)

        # Terminal REAR mismatch with Open current
        pts_open = [ReadingPoint(i + 1, i * 0.01, 2.0, 1e-11, 1e11, 2e-11) for i in range(6)]
        res_term = _make_result("2450", pts_open, cfg=ChannelConfig(terminals=TerminalLocation.REAR))
        rep_term = AutoDiagnosisEngine.diagnose(res_term, self.p2450)
        by_t = {it.rule_id: it for it in rep_term.items}
        self.assertEqual(by_t["TERMINAL_MISMATCH"].severity, DiagnosisSeverity.CRITICAL)
        self.assertEqual(by_t["OPEN_CIRCUIT"].severity, DiagnosisSeverity.WARNING)

    def test_04_cat2_wiring_and_contact_rules(self) -> None:
        # Short circuit (R = 0.1 ohm at 0.01V)
        pts_short = [ReadingPoint(i + 1, i * 0.01, 0.01, 0.1, 0.1, 0.001, in_compliance=True) for i in range(6)]
        rep_s = AutoDiagnosisEngine.diagnose(_make_result("2450", pts_short), self.p2450)
        self.assertEqual({it.rule_id: it for it in rep_s.items}["SHORT_CIRCUIT"].severity, DiagnosisSeverity.CRITICAL)

        # Low-R (5 ohm) with 2-Wire -> recommend 4-Wire
        pts_lowr = [ReadingPoint(i + 1, i * 0.01, 0.5 + i * 0.1, (0.5 + i * 0.1) / 5.0, 5.0, 0.1) for i in range(6)]
        rep_w = AutoDiagnosisEngine.diagnose(_make_result("2450", pts_lowr, cfg=ChannelConfig(sense_mode=SenseMode.WIRE_2)), self.p2450)
        self.assertEqual({it.rule_id: it for it in rep_w.items}["WIRE_MODE_MISMATCH"].severity, DiagnosisSeverity.WARNING)

        # Polarity reversed (+V with -I)
        pts_pol = [ReadingPoint(i + 1, i * 0.01, 1.0 + i * 0.2, -0.002, -500.0, -0.002) for i in range(6)]
        rep_p = AutoDiagnosisEngine.diagnose(_make_result("2450", pts_pol), self.p2450, is_sink_mode=False)
        self.assertEqual({it.rule_id: it for it in rep_p.items}["POLARITY_REVERSED"].severity, DiagnosisSeverity.WARNING)

    def test_05_cat3_noise_precision_and_cat4_timing_rules(self) -> None:
        # 60Hz noise (NPLC < 1 + heavy ripple) + Oversized fixed range + AutoZero OFF long-term drift
        pts_noisy = [
            ReadingPoint(i + 1, i * 100.0, 1.0, 0.001 + (0.0006 if i % 2 == 0 else -0.0006), 1000.0, 0.001)
            for i in range(10)
        ]
        cfg_n = ChannelConfig(nplc=0.05, measure_range=1.0, auto_zero="OFF", high_capacitance=False)
        rc_n = SweepRecipe(nplc=0.05, delay_s=0.01)
        rep_n = AutoDiagnosisEngine.diagnose(_make_result("2450", pts_noisy, cfg=cfg_n, recipe=rc_n), self.p2450)
        by_n = {it.rule_id: it for it in rep_n.items}
        self.assertEqual(by_n["POWERLINE_NOISE_60HZ"].severity, DiagnosisSeverity.WARNING)
        self.assertEqual(by_n["RANGE_TOO_LARGE"].severity, DiagnosisSeverity.WARNING)
        self.assertEqual(by_n["LONG_TERM_DRIFT"].severity, DiagnosisSeverity.WARNING)
        self.assertEqual(by_n["LOAD_OSCILLATION"].severity, DiagnosisSeverity.CRITICAL)
        self.assertEqual(by_n["SAMPLING_BOTTLENECK"].severity, DiagnosisSeverity.WARNING)

        # Dual sweep settling hysteresis
        fwd_i = [0.0001, 0.0005, 0.001, 0.002, 0.005]
        rev_i = [0.005, 0.0045, 0.004, 0.003, 0.0025]
        pts_dual = [ReadingPoint(i + 1, i * 0.005, float(i), val, 1000.0, 0.001) for i, val in enumerate(fwd_i + rev_i)]
        rep_d = AutoDiagnosisEngine.diagnose(
            _make_result("2450", pts_dual, recipe=SweepRecipe(sweep_type=SweepType.DUAL, delay_s=0.005)),
            self.p2450,
        )
        self.assertEqual({it.rule_id: it for it in rep_d.items}["SETTLING_INSUFFICIENT"].severity, DiagnosisSeverity.WARNING)

    def test_06_gui_banner_autofix_and_excel_6th_sheet(self) -> None:
        from smu_app.core.models import MultiUnitSyncMode

        win = SMUMainWindow(show_startup_wizard=False, prefer_simulator=True)
        try:
            win.suppress_range_popup = True
            win.multi_sync_mode = MultiUnitSyncMode.TSPLINK_HW
            win.spn_nodes.setValue(1)
            win.cmb_sweep_type.setCurrentText("LINEAR")
            win.spn_points.setValue(15)
            win.execute_measurement()
            assert win.last_result is not None
            assert win.last_result.diagnosis_report is not None
            self.assertEqual(win.last_result.diagnosis_report.total_rules_checked, 15)

            # Verify 1-click Auto-Fix updates GUI controls
            dummy_fix = next(it for it in win.last_result.diagnosis_report.items if it.rule_id == "LOAD_OSCILLATION")
            dummy_fix.fix_action_id = "ENABLE_HIGH_CAP"
            win.apply_diagnosis_autofix(dummy_fix)
            self.assertTrue(win.chk_high_cap.isChecked())

            # Verify Excel 6_Auto_Diagnosis sheet
            with tempfile.TemporaryDirectory() as tmpdir:
                exporter = DataExporter(tmpdir)
                xlsx = exporter.export_excel_report(win.last_result)
                wb = openpyxl.load_workbook(xlsx, data_only=True)
                self.assertIn("6_Auto_Diagnosis", wb.sheetnames)
                ws6 = wb["6_Auto_Diagnosis"]
                col_a = [str(ws6.cell(r, 1).value or "") for r in range(1, ws6.max_row + 1)]
                self.assertTrue(any("자동 측정 코치" in v for v in col_a))
                self.assertTrue(any("1. 출력/리미트" in v for v in col_a))
        finally:
            win.close()
            _APP.processEvents()


if __name__ == "__main__":
    unittest.main()
