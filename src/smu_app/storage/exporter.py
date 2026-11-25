"""High-Speed Multi-Format Data Exporter (Tek QuickStart Sample_06_SMU_SMU_2450.xlsx Compatible Multi-Sheet Format).

Upgraded Report Format matching `F:\\02.Project\\06. Tek Quickstart program\\05.data\\03.Samples`:
- Sheet 1 (`1_Summary_Checklist`):
  - Summary Metadata (측정 리포트 생성 시각, 장비 폴더 분류, 선택 장비 모델, 참조 매뉴얼 파일, VISA 리소스 주소, 동작 모드, *IDN?, 총 기록된 데이터 포인트 수)
  - Remote Readiness Checklist (`리모트 준비 사항 항목 (Remote Readiness Check)` | `사용자 확인 상태`)
- Sheet 2 (`2_Input_Configuration`):
  - `구분 (Type)` | `파라미터 / 측정 항목 이름` | `설정값 / 선택 상태`
  - Includes all `사용자 입력 파라미터 (Required Input)`, `선택된 측정 항목 (Measurable Item)`, and `자동 추출 특성 지표 (Extracted Metric)`
- Sheet 3 (`3_Measured_Data`):
  - `Step` | `Node.Channel` | `Timestamp` | `Elapsed (s)` | `측정 전압 (Measured Voltage)` | `측정 전류 (Measured Current)` | `측정 저항 (Measured Resistance)` | `측정 전력 (Measured Power)` | `컴플라이언스 제한 도달 (Compliance Tripped)`
  - Bottom green-highlighted statistics rows: `평균 (Mean)`, `최댓값 (Max)`, `최솟값 (Min)`, `표준편차 (StdDev)`
  - Embedded Excel ScatterChart right beside/below the table
- Sheet 4 (`4_Waveform_Trace`):
  - `Point Index` | `Time (s)` | `Source Level (V/A)` | `Measured Voltage (V)` | `Measured Current (A)`
- Sheet 5 (`5_Graph_Report`):
  - Embedded high-resolution I-V & Time-Domain PNG graph image + interactive Excel ScatterChart
"""

from __future__ import annotations

import csv
from datetime import timedelta
import io
from pathlib import Path
import sqlite3
from typing import Any, Iterable

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import openpyxl
from openpyxl.chart import Reference, ScatterChart, Series
from openpyxl.drawing.image import Image as OpenpyxlImage
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
import pandas as pd

from smu_app.core.models import MeasurementResult, ReadingPoint
from smu_app.storage.setup_manager import (
    SetupPersistenceManager,
    find_manual_pdf_for_model,
)


DEFAULT_SMU_CHECKLIST_ITEMS: list[dict[str, Any]] = [
    {
        "title": "[1단계: 케이블 연결 및 명령어 모드(*LANG?) 확인] USB/LAN 연결 및 SCPI vs TSP 모드 자동 진단",
        "checked": True,
    },
    {
        "title": "[2단계: 명령어 언어 감지 & 고전압 안전 인터록] *LANG? 확인 및 ±42V 초과 시 후면 Interlock 체결",
        "checked": True,
    },
    {
        "title": "[3단계: 단자 위치(Front/Rear) 및 2-Wire vs 4-Wire 배선 확인] 바나나잭(전면) 또는 트라이액셜(후면) 결선",
        "checked": True,
    },
    {
        "title": "[4단계: 컴플라이언스(제한값) 및 SOA 안전 동작 영역 점검] 소자 보호용 제한 전류/전압(Compliance) 확인",
        "checked": True,
    },
]


class DataExporter:
    """Exports MeasurementResult and Run History to Tek QuickStart formatted Excel, Streaming CSV, Parquet, SQLite, and PNG/SVG."""

    def __init__(self, base_output_dir: Path | str | None = None) -> None:
        if base_output_dir is not None:
            self.output_dir = Path(base_output_dir)
        else:
            self.output_dir = SetupPersistenceManager.get_saved_export_dir()
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def set_output_dir(self, new_dir: Path | str, persist: bool = True) -> Path:
        """Update and optionally persist the export directory across restarts."""
        self.output_dir = Path(new_dir).resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        if persist:
            SetupPersistenceManager.set_saved_export_dir(self.output_dir)
        return self.output_dir

    def build_auto_filename(self, result: MeasurementResult, ext: str) -> Path:
        ts = result.started_at.strftime("%Y%m%d_%H%M%S")
        safe_recipe = "".join(c if c.isalnum() or c in ("_", "-") else "_" for c in result.recipe_name)
        return self.output_dir / f"{ts}_{result.model}_{safe_recipe}.{ext.lstrip('.')}"

    @staticmethod
    def points_to_dataframe(points: Iterable[ReadingPoint]) -> pd.DataFrame:
        rows = [
            {
                "Index": p.index,
                "Node": p.node_id,
                "Channel": p.channel,
                "Timestamp_s": p.timestamp_s,
                "Voltage_V": p.voltage_v,
                "Current_A": p.current_a,
                "Resistance_Ohm": p.resistance_ohm,
                "Power_W": p.power_w,
                "In_Compliance": p.in_compliance,
                "Is_4Wire": p.is_4wire,
                "Limit_Passed": p.limit_passed,
                "Status_Bits": p.status_bits,
            }
            for p in points
        ]
        return pd.DataFrame(rows)

    def export_streaming_csv(
        self,
        result: MeasurementResult,
        file_path: Path | str | None = None,
        chunk_size: int = 50000,
    ) -> Path:
        out_path = Path(file_path) if file_path else self.build_auto_filename(result, "csv")
        out_path.parent.mkdir(parents=True, exist_ok=True)

        headers = [
            "Index",
            "Node",
            "Channel",
            "Timestamp_s",
            "Voltage_V",
            "Current_A",
            "Resistance_Ohm",
            "Power_W",
            "In_Compliance",
            "Is_4Wire",
            "Limit_Passed",
            "Status_Bits",
        ]
        with open(out_path, "w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.writer(fh)
            writer.writerow([f"# Model={result.model}", f"Recipe={result.recipe_name}", f"Session={result.session_id}"])
            writer.writerow(headers)
            batch: list[list[object]] = []
            for p in result.points:
                batch.append([
                    p.index,
                    p.node_id,
                    p.channel,
                    p.timestamp_s,
                    p.voltage_v,
                    p.current_a,
                    p.resistance_ohm,
                    p.power_w,
                    p.in_compliance,
                    p.is_4wire,
                    p.limit_passed,
                    p.status_bits,
                ])
                if len(batch) >= chunk_size:
                    writer.writerows(batch)
                    batch.clear()
            if batch:
                writer.writerows(batch)
        return out_path

    def export_parquet(self, result: MeasurementResult, file_path: Path | str | None = None) -> Path:
        out_path = Path(file_path) if file_path else self.build_auto_filename(result, "parquet")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        df = self.points_to_dataframe(result.points)
        df.to_parquet(out_path, index=False)
        return out_path

    def export_sqlite_archive(self, result: MeasurementResult, file_path: Path | str | None = None) -> Path:
        out_path = Path(file_path) if file_path else self.build_auto_filename(result, "db")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        df = self.points_to_dataframe(result.points)
        conn = sqlite3.connect(out_path)
        try:
            df.to_sql("readings", conn, if_exists="replace", index=False)
            meta_df = pd.DataFrame([{"session_id": result.session_id, "model": result.model, "recipe": result.recipe_name, **result.metrics}])
            meta_df.to_sql("session_summary", conn, if_exists="replace", index=False)
            conn.commit()
        finally:
            conn.close()
        return out_path

    def _render_dual_chart_png_bytes(self, result: MeasurementResult) -> bytes:
        """Render a side-by-side I-V Curve & Time-Domain Waveform PNG image in memory for Excel embedding."""
        fig, (ax_iv, ax_time) = plt.subplots(1, 2, figsize=(12.5, 4.6), dpi=130)
        pts = result.points
        if pts:
            v = [p.voltage_v for p in pts]
            i = [p.current_a for p in pts]
            t = [p.timestamp_s for p in pts]
            ax_iv.plot(v, i, "-o", color="#0070C0", markersize=3.5, linewidth=1.8, label=f"{result.model} I-V")
            ax_time.plot(t, v, "-s", color="#0E7490", markersize=3, linewidth=1.5, label="Voltage (V)")
            ax_time_i = ax_time.twinx()
            ax_time_i.plot(t, i, "-^", color="#DC2626", markersize=3, linewidth=1.5, label="Current (A)")
            ax_time_i.set_ylabel("Current (A)", color="#DC2626")

        ax_iv.set_title(f"[{result.model}] I-V Characteristic Curve")
        ax_iv.set_xlabel("Voltage (V)")
        ax_iv.set_ylabel("Current (A)")
        ax_iv.grid(True, linestyle=":", alpha=0.6)
        ax_iv.legend(loc="best")

        ax_time.set_title(f"[{result.model}] Time-Domain Source & Measure Trace")
        ax_time.set_xlabel("Elapsed Time (s)")
        ax_time.set_ylabel("Voltage (V)", color="#0E7490")
        ax_time.grid(True, linestyle=":", alpha=0.6)

        fig.tight_layout()
        buf = io.BytesIO()
        fig.savefig(buf, format="png")
        plt.close(fig)
        buf.seek(0)
        return buf.getvalue()

    def export_plot_image(
        self,
        result: MeasurementResult,
        file_path: Path | str | None = None,
        fmt: str = "png",
        mode: str = "IV",
        history_overlays: list[MeasurementResult] | None = None,
    ) -> Path:
        ext = fmt.lower().lstrip(".")
        out_path = Path(file_path) if file_path else self.build_auto_filename(result, ext)
        out_path.parent.mkdir(parents=True, exist_ok=True)

        fig, ax = plt.subplots(figsize=(8.5, 5.2), dpi=150)

        if history_overlays:
            for idx, h_res in enumerate(history_overlays[-4:], start=1):
                if not h_res.points:
                    continue
                hx = [p.timestamp_s if mode == "TIME" else p.voltage_v for p in h_res.points]
                hy = [p.current_a for p in h_res.points]
                ax.plot(hx, hy, "--", alpha=0.55, linewidth=1.2, label=f"Hist #{idx}: {h_res.model} ({h_res.recipe_name})")

        pts = result.points
        if pts:
            x = [p.timestamp_s if mode == "TIME" else p.voltage_v for p in pts]
            y = [p.current_a for p in pts]
            ax.plot(x, y, "-o", color="#0070C0", markersize=3, linewidth=1.8, label=f"Current: {result.model} ({len(pts)} pts)")

        ax.set_title(f"Keithley {result.model} — {result.recipe_name} ({mode} View)")
        ax.set_xlabel("Time (s)" if mode == "TIME" else "Voltage (V)")
        ax.set_ylabel("Current (A)")
        ax.grid(True, linestyle=":", alpha=0.6)
        ax.legend(loc="best")
        fig.tight_layout()
        fig.savefig(out_path, format=ext)
        plt.close(fig)
        return out_path

    def export_excel_report(
        self,
        result: MeasurementResult,
        file_path: Path | str | None = None,
        resource_addr: str = "USB0::0x05E6::0x2450::04584348::INSTR",
        is_simulation: bool = False,
        idn_string: str = "",
        checklist_status: list[dict[str, Any]] | None = None,
        extra_input_params: dict[str, Any] | None = None,
    ) -> Path:
        """Generate Tek QuickStart compatible multi-sheet Excel report:
        1_Summary_Checklist // 2_Input_Configuration // 3_Measured_Data (+Mean/Max/Min/StdDev) // 4_Waveform_Trace // 5_Graph_Report
        """
        out_path = Path(file_path) if file_path else self.build_auto_filename(result, "xlsx")
        out_path.parent.mkdir(parents=True, exist_ok=True)

        wb = openpyxl.Workbook()

        # Tek QuickStart exact styling palette (#003366 Navy Header, #D9E1F2 Sub-fill, #E2EFDA Green Statistics fill)
        header_fill = PatternFill(start_color="003366", end_color="003366", fill_type="solid")
        sub_fill = PatternFill(start_color="D9E1F2", end_color="D9E1F2", fill_type="solid")
        stat_fill = PatternFill(start_color="E2EFDA", end_color="E2EFDA", fill_type="solid")
        fail_fill = PatternFill(start_color="FEE2E2", end_color="FEE2E2", fill_type="solid")
        header_font = Font(name="맑은 고딕", size=11, bold=True, color="FFFFFF")
        bold_font = Font(name="맑은 고딕", size=10, bold=True)
        reg_font = Font(name="맑은 고딕", size=10)
        thin_border = Border(
            left=Side(style="thin", color="CCCCCC"),
            right=Side(style="thin", color="CCCCCC"),
            top=Side(style="thin", color="CCCCCC"),
            bottom=Side(style="thin", color="CCCCCC"),
        )

        pdf_manual = find_manual_pdf_for_model(result.model)
        manual_rel = f"docs/manuals/{pdf_manual.parent.name}/{pdf_manual.name}" if pdf_manual else f"docs/manuals/{result.model}"
        idn_val = idn_string or f"KEITHLEY INSTRUMENTS,MODEL {result.model},SESSION-{result.session_id},v3.0"

        # =====================================================================
        # SHEET 1: 1_Summary_Checklist
        # =====================================================================
        ws_sum = wb.active
        ws_sum.title = "1_Summary_Checklist"
        summary_rows = [
            ("항목 (Field)", "내용 (Value)"),
            ("측정 리포트 생성 시각", result.completed_at.strftime("%Y-%m-%d %H:%M:%S")),
            ("장비 폴더 분류 (Category)", "06. SMUs (Keithley SourceMeter® 2400 / 2600B 시리즈)"),
            ("선택 장비 모델 (Model)", f"Model {result.model} SourceMeter"),
            ("참조 매뉴얼 파일 (Manual)", manual_rel),
            ("VISA 리소스 주소", resource_addr),
            ("동작 모드 (Real / Simulation)", "가상 시뮬레이션 (Simulation)" if is_simulation else "실제 하드웨어 (Real VISA)"),
            ("장비 식별 정보 (*IDN?)", idn_val),
            ("총 기록된 데이터 포인트 수", len(result.points)),
        ]
        for r_idx, (k, v) in enumerate(summary_rows, start=1):
            c1 = ws_sum.cell(row=r_idx, column=1, value=k)
            c2 = ws_sum.cell(row=r_idx, column=2, value=str(v))
            c1.border = thin_border
            c2.border = thin_border
            if r_idx == 1:
                c1.fill = header_fill
                c2.fill = header_fill
                c1.font = header_font
                c2.font = header_font
            else:
                c1.fill = sub_fill
                c1.font = bold_font
                c2.font = reg_font

        start_chk = len(summary_rows) + 2
        ch_h1 = ws_sum.cell(row=start_chk, column=1, value="리모트 준비 사항 항목 (Remote Readiness Check)")
        ch_h2 = ws_sum.cell(row=start_chk, column=2, value="사용자 확인 상태")
        for ch_cell in (ch_h1, ch_h2):
            ch_cell.fill = header_fill
            ch_cell.font = header_font
            ch_cell.border = thin_border

        chk_list = checklist_status if checklist_status else DEFAULT_SMU_CHECKLIST_ITEMS
        for idx, item in enumerate(chk_list, start=start_chk + 1):
            c1 = ws_sum.cell(row=idx, column=1, value=item.get("title", ""))
            c2 = ws_sum.cell(
                row=idx,
                column=2,
                value="확인 완료 (Checked)" if item.get("checked", True) else "미확인 (Unchecked)",
            )
            c1.font = reg_font
            c2.font = bold_font
            c1.border = thin_border
            c2.border = thin_border

        ws_sum.column_dimensions["A"].width = 52
        ws_sum.column_dimensions["B"].width = 68

        # =====================================================================
        # SHEET 2: 2_Input_Configuration
        # =====================================================================
        ws_cfg = wb.create_sheet(title="2_Input_Configuration")
        for col_i, h_text in enumerate(["구분 (Type)", "파라미터 / 측정 항목 이름", "설정값 / 선택 상태"], start=1):
            c = ws_cfg.cell(row=1, column=col_i, value=h_text)
            c.fill = header_fill
            c.font = header_font
            c.border = thin_border

        rc = result.recipe
        cc = result.channel_config
        input_params: dict[str, Any] = {
            "smu_channel (논리 채널)": cc.logical_channel.label,
            "cmd_lang (명령어 엔진)": "TSP (Test Script Processor / 24xx Auto-Adapt)",
            "terminals (출력 단자)": f"{cc.terminals.value} (전면/후면 단자)",
            "sense_wire (센싱 배선)": f"{cc.sense_mode.value} ({'Remote Kelvin Sense' if cc.sense_mode.value == '4WIRE' else 'Local Sense'})",
            "output_off_mode (출력 OFF 상태)": cc.output_off_mode.value,
            "op_mode / sweep_type (파형 모드)": rc.sweep_type.value,
            "source_func (소스 인가 기능)": f"{rc.source_func.value} Source",
            "measure_func (측정 기능)": f"{rc.measure_func.value} Measure",
            "start_level (시작/바이어스 레벨)": rc.start_level,
            "stop_level (종료/펄스 레벨)": rc.stop_level,
            "sweep_points (측정 포인트 수)": rc.points,
            "step_delay_s (스텝 지연 시간 s)": rc.delay_s,
            "repeat_mode (반복 출력 모드)": f"{rc.repeat_mode.value} (Count={rc.repeat_count})",
            "compliance_limit (제한값 A/V)": rc.compliance_limit,
            "nplc (적분 시간 PLC)": rc.nplc,
            "auto_zero (오토제로)": cc.auto_zero,
            "source_readback (소스 리드백)": cc.source_readback,
            "high_cap (고용량 부하 모드)": cc.high_capacitance,
            "acquisition_mode (버퍼 수집 방식)": rc.acquisition_mode.value,
        }
        if extra_input_params:
            input_params.update(extra_input_params)

        cfg_row = 2
        for k, v in input_params.items():
            c1 = ws_cfg.cell(row=cfg_row, column=1, value="사용자 입력 파라미터 (Required Input)")
            c2 = ws_cfg.cell(row=cfg_row, column=2, value=str(k))
            c3 = ws_cfg.cell(row=cfg_row, column=3, value=str(v))
            c1.font = bold_font
            c2.font = reg_font
            c3.font = reg_font
            for col in range(1, 4):
                ws_cfg.cell(row=cfg_row, column=col).border = thin_border
            cfg_row += 1

        meas_items = [
            ("측정 전압 (Measured Voltage) (meas_v)", "Active (측정 수행)"),
            ("측정 전류 (Measured Current) (meas_i)", "Active (측정 수행)"),
            ("측정 저항 (Measured Resistance) (meas_r)", "Active (측정 수행)"),
            ("측정 전력 (Measured Power) (meas_p)", "Active (측정 수행)"),
            ("컴플라이언스 제한 도달 (Compliance Tripped) (compliance_state)", "Active (측정 수행)"),
        ]
        for mlbl, mstate in meas_items:
            c1 = ws_cfg.cell(row=cfg_row, column=1, value="선택된 측정 항목 (Measurable Item)")
            c2 = ws_cfg.cell(row=cfg_row, column=2, value=mlbl)
            c3 = ws_cfg.cell(row=cfg_row, column=3, value=mstate)
            c1.font = bold_font
            c2.font = reg_font
            c3.font = reg_font
            for col in range(1, 4):
                ws_cfg.cell(row=cfg_row, column=col).border = thin_border
            cfg_row += 1

        for mk, mv in result.metrics.items():
            c1 = ws_cfg.cell(row=cfg_row, column=1, value="자동 추출 특성 지표 (Extracted Metric)")
            c2 = ws_cfg.cell(row=cfg_row, column=2, value=str(mk))
            c3 = ws_cfg.cell(row=cfg_row, column=3, value=str(mv if mv is not None else "N/A"))
            c1.fill = stat_fill
            c1.font = bold_font
            c2.font = reg_font
            c3.font = bold_font
            for col in range(1, 4):
                ws_cfg.cell(row=cfg_row, column=col).border = thin_border
            cfg_row += 1

        ws_cfg.column_dimensions["A"].width = 36
        ws_cfg.column_dimensions["B"].width = 48
        ws_cfg.column_dimensions["C"].width = 38

        # =====================================================================
        # SHEET 3: 3_Measured_Data (+ Mean / Max / Min / StdDev Statistics & Chart)
        # =====================================================================
        ws_data = wb.create_sheet(title="3_Measured_Data")
        data_cols = [
            "Step",
            "Node.Channel",
            "Timestamp",
            "Elapsed (s)",
            "측정 전압 (Measured Voltage)",
            "측정 전류 (Measured Current)",
            "측정 저항 (Measured Resistance)",
            "측정 전력 (Measured Power)",
            "컴플라이언스 제한 도달 (Compliance Tripped)",
        ]
        for c_idx, col_name in enumerate(data_cols, start=1):
            cell = ws_data.cell(row=1, column=c_idx, value=col_name)
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center")
            cell.border = thin_border

        max_excel_pts = min(len(result.points), 1000000)
        for r_i, p in enumerate(result.points[:max_excel_pts], start=2):
            abs_ts = (result.started_at + timedelta(seconds=float(p.timestamp_s))).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            r_ohm = p.resistance_ohm if (p.resistance_ohm != float("inf") and not np.isnan(p.resistance_ohm)) else None
            comp_str = "TRIPPED" if p.in_compliance else "NORMAL"
            row_vals = [
                p.index,
                f"Node{p.node_id}.{p.channel}",
                abs_ts,
                float(p.timestamp_s),
                float(p.voltage_v),
                float(p.current_a),
                r_ohm,
                float(p.power_w),
                comp_str,
            ]
            for c_idx, val in enumerate(row_vals, start=1):
                cell = ws_data.cell(row=r_i, column=c_idx, value=val)
                cell.font = reg_font
                cell.border = thin_border
                if p.in_compliance or not p.limit_passed:
                    cell.fill = fail_fill

        # Append Tek QuickStart Summary Statistics (Mean, Max, Min, StdDev) at bottom of 3_Measured_Data
        if max_excel_pts > 0:
            stat_start = max_excel_pts + 3
            stat_defs = [
                ("평균 (Mean)", "mean"),
                ("최댓값 (Max)", "max"),
                ("최솟값 (Min)", "min"),
                ("표준편차 (StdDev)", "std"),
            ]
            # Numeric column indices (1-based): 4=Elapsed, 5=Voltage, 6=Current, 7=Resistance, 8=Power
            num_arrays: dict[int, np.ndarray] = {
                4: np.array([p.timestamp_s for p in result.points[:max_excel_pts]], dtype=float),
                5: np.array([p.voltage_v for p in result.points[:max_excel_pts]], dtype=float),
                6: np.array([p.current_a for p in result.points[:max_excel_pts]], dtype=float),
                7: np.array(
                    [p.resistance_ohm for p in result.points[:max_excel_pts] if np.isfinite(p.resistance_ohm)],
                    dtype=float,
                ),
                8: np.array([p.power_w for p in result.points[:max_excel_pts]], dtype=float),
            }
            for s_offset, (s_label, s_func) in enumerate(stat_defs):
                r_cur = stat_start + s_offset
                c_lbl = ws_data.cell(row=r_cur, column=1, value=s_label)
                c_lbl.fill = stat_fill
                c_lbl.font = bold_font
                c_lbl.border = thin_border

                for c_idx in range(2, len(data_cols) + 1):
                    arr = num_arrays.get(c_idx)
                    if arr is not None and len(arr) > 0:
                        if s_func == "mean":
                            val_stat: Any = round(float(np.mean(arr)), 8)
                        elif s_func == "max":
                            val_stat = round(float(np.max(arr)), 8)
                        elif s_func == "min":
                            val_stat = round(float(np.min(arr)), 8)
                        else:
                            val_stat = round(float(np.std(arr, ddof=1)) if len(arr) > 1 else 0.0, 8)
                    else:
                        val_stat = "-"
                    c_st = ws_data.cell(row=r_cur, column=c_idx, value=val_stat)
                    c_st.fill = stat_fill
                    c_st.font = bold_font
                    c_st.border = thin_border

        for c_idx in range(1, len(data_cols) + 1):
            ws_data.column_dimensions[get_column_letter(c_idx)].width = 24

        # =====================================================================
        # SHEET 4: 4_Waveform_Trace
        # =====================================================================
        ws_wfm = wb.create_sheet(title="4_Waveform_Trace")
        wfm_cols = ["Point Index", "Elapsed Time (s)", "Measured Voltage (V)", "Measured Current (A)", "Power (W)"]
        for c_idx, h_title in enumerate(wfm_cols, start=1):
            c = ws_wfm.cell(row=1, column=c_idx, value=h_title)
            c.fill = header_fill
            c.font = header_font
            c.border = thin_border
            ws_wfm.column_dimensions[get_column_letter(c_idx)].width = 22

        for i, p in enumerate(result.points[:max_excel_pts], start=2):
            ws_wfm.cell(row=i, column=1, value=p.index).font = reg_font
            ws_wfm.cell(row=i, column=2, value=float(p.timestamp_s)).font = reg_font
            ws_wfm.cell(row=i, column=3, value=float(p.voltage_v)).font = reg_font
            ws_wfm.cell(row=i, column=4, value=float(p.current_a)).font = reg_font
            ws_wfm.cell(row=i, column=5, value=float(p.power_w)).font = reg_font

        # =====================================================================
        # SHEET 5: 5_Graph_Report (High-Res Dual Graph Image + Native Excel Charts)
        # =====================================================================
        ws_graph = wb.create_sheet(title="5_Graph_Report")
        ws_graph["A1"] = f"Tektronix / Keithley {result.model} — 측정 그래프 리포트 ({result.recipe_name})"
        ws_graph["A1"].font = Font(name="맑은 고딕", size=14, bold=True, color="003366")

        if max_excel_pts >= 2:
            chart_rows = min(max_excel_pts + 1, 5000)

            # 1. Native Excel I-V ScatterChart on 3_Measured_Data AND 5_Graph_Report
            iv_chart = ScatterChart()
            iv_chart.title = f"{result.model} I-V Characteristic Curve (Voltage vs Current)"
            iv_chart.x_axis.title = "Voltage (V)"
            iv_chart.y_axis.title = "Current (A)"
            xref_iv = Reference(ws_data, min_col=5, min_row=2, max_row=chart_rows)
            yref_iv = Reference(ws_data, min_col=6, min_row=2, max_row=chart_rows)
            iv_chart.series.append(Series(yref_iv, xref_iv, title_from_data=False))
            iv_chart.width = 18
            iv_chart.height = 11
            ws_data.add_chart(iv_chart, "K2")

            # 2. High-Resolution Matplotlib Dual Plot PNG embedded in 5_Graph_Report
            try:
                png_bytes = self._render_dual_chart_png_bytes(result)
                img_stream = io.BytesIO(png_bytes)
                xl_img = OpenpyxlImage(img_stream)
                ws_graph.add_image(xl_img, "A3")
            except Exception:
                pass

        wb.save(out_path)
        return out_path

    def export_run_history_comparison_excel(
        self,
        history: list[MeasurementResult],
        file_path: Path | str | None = None,
    ) -> Path:
        if not history:
            raise ValueError("비교할 Run History 측정 이력이 없습니다.")

        latest = history[-1]
        out_path = (
            Path(file_path)
            if file_path
            else (self.output_dir / f"{latest.started_at.strftime('%Y%m%d_%H%M%S')}_RunHistory_Comparison.xlsx")
        )
        out_path.parent.mkdir(parents=True, exist_ok=True)

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Run_History_Comparison"

        header_fill = PatternFill(start_color="003366", end_color="003366", fill_type="solid")
        header_font = Font(name="맑은 고딕", size=10, bold=True, color="FFFFFF")

        ws["A1"] = f"SMU Run History 다중 실행 비교 리포트 (총 {len(history)}회 실행)"
        ws["A1"].font = Font(name="맑은 고딕", size=14, bold=True, color="003366")

        headers = [
            "Run #",
            "Session ID",
            "Model",
            "Recipe",
            "Points",
            "I_Max (A)",
            "I_Mean (A)",
            "Vth (V)",
            "dV/dI (Ohm)",
            "RiseTime (s)",
            "Overshoot (%)",
            "Compliance Hits",
        ]
        for c_idx, h in enumerate(headers, start=1):
            cell = ws.cell(row=3, column=c_idx, value=h)
            cell.fill = header_fill
            cell.font = header_font

        chart = ScatterChart()
        chart.title = "Run History Multi-Curve Overlay Comparison"
        chart.x_axis.title = "Voltage (V) / Time (s)"
        chart.y_axis.title = "Current (A)"
        chart.width = 20
        chart.height = 12

        for idx, res in enumerate(history, start=1):
            r_row = idx + 3
            m = res.metrics
            row_vals = [
                f"Run #{idx}",
                res.session_id,
                res.model,
                res.recipe_name,
                len(res.points),
                m.get("i_max"),
                m.get("i_mean"),
                m.get("v_threshold_v"),
                m.get("dv_di_median_ohm"),
                m.get("rise_time_10_90_s"),
                m.get("overshoot_pct"),
                m.get("compliance_hit_count"),
            ]
            for c_idx, val in enumerate(row_vals, start=1):
                ws.cell(row=r_row, column=c_idx, value=val if val is not None else "N/A")

            sheet_title = f"Run{idx}_{res.model}"[:31]
            ws_run = wb.create_sheet(title=sheet_title)
            ws_run.append(["Index", "Timestamp_s", "Voltage_V", "Current_A"])
            max_pts = min(len(res.points), 5000)
            for p in res.points[:max_pts]:
                ws_run.append([p.index, p.timestamp_s, p.voltage_v, p.current_a])
            if max_pts >= 2:
                xref = Reference(ws_run, min_col=3, min_row=2, max_row=max_pts + 1)
                yref = Reference(ws_run, min_col=4, min_row=2, max_row=max_pts + 1)
                ser = Series(yref, xref, title=f"Run #{idx} ({res.model})")
                chart.series.append(ser)

        ws.add_chart(chart, f"A{len(history) + 6}")
        wb.save(out_path)
        return out_path
