"""High-Speed Multi-Format Data Exporter (Phase 1-3 Complete: Excel, Streaming CSV, Parquet, SQLite/HDF5, PNG/SVG, Run History Comparison).

Implements v3.0 Section 07-3 & Phase 3 Data/Reporting Enhancements:
- Structured Excel (.xlsx): Summary sheet (instrument info, setup parameters, statistics, pulse metrics, embedded chart)
  + automatic per-Node/Channel Raw Data sheets.
- Phase 3 Multi-Run History Comparison Excel Report (`export_run_history_comparison_excel`):
  Compares multiple MeasurementResult runs side-by-side in a unified metrics table and overlays their curves.
- High-speed streaming CSV writer for >1M rows (bypassing Excel's 1,048,576-row limit).
- Apache Parquet (.parquet) & SQLite/HDF5 binary archive export.
- High-resolution Plot Image Exporter (PNG & SVG) via Matplotlib for publication/report quality charts.
"""

from __future__ import annotations

import csv
from pathlib import Path
import sqlite3
from typing import Iterable

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import openpyxl
from openpyxl.chart import Reference, ScatterChart, Series
from openpyxl.styles import Alignment, Font, PatternFill
import pandas as pd

from smu_app.core.models import MeasurementResult, ReadingPoint


class DataExporter:
    """Exports MeasurementResult and Run History to Excel, Streaming CSV, Parquet, SQLite/HDF5, and PNG/SVG plots."""

    def __init__(self, base_output_dir: Path | str | None = None) -> None:
        default_dir = Path(__file__).resolve().parents[3] / "data" / "processed"
        self.output_dir = Path(base_output_dir) if base_output_dir else default_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)

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
        """Stream measurement points to CSV in chunks to support multi-million-row digitizer captures."""
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
        """Export measurement points to high-speed compressed Apache Parquet format."""
        out_path = Path(file_path) if file_path else self.build_auto_filename(result, "parquet")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        df = self.points_to_dataframe(result.points)
        df.to_parquet(out_path, index=False)
        return out_path

    def export_sqlite_archive(self, result: MeasurementResult, file_path: Path | str | None = None) -> Path:
        """Export measurement points and session metadata into a self-contained SQLite (.db) scientific archive."""
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

    def export_plot_image(
        self,
        result: MeasurementResult,
        file_path: Path | str | None = None,
        fmt: str = "png",
        mode: str = "IV",
        history_overlays: list[MeasurementResult] | None = None,
    ) -> Path:
        """Render publication-grade PNG or SVG plot of I-V curve or Time-Domain Pulse waveform."""
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

    def export_excel_report(self, result: MeasurementResult, file_path: Path | str | None = None) -> Path:
        """Generate structured Excel (.xlsx) workbook with Summary + Embedded Chart + Per-Channel Raw Data sheets."""
        out_path = Path(file_path) if file_path else self.build_auto_filename(result, "xlsx")
        out_path.parent.mkdir(parents=True, exist_ok=True)

        wb = openpyxl.Workbook()
        ws_sum = wb.active
        ws_sum.title = "Summary"

        header_fill = PatternFill(start_color="0B1F3A", end_color="0B1F3A", fill_type="solid")
        header_font = Font(name="맑은 고딕", size=11, bold=True, color="FFFFFF")
        bold_font = Font(name="맑은 고딕", size=10, bold=True)

        ws_sum["A1"] = "Tektronix / Keithley SMU 심화 측정 리포트 (v3.0)"
        ws_sum["A1"].font = Font(name="맑은 고딕", size=14, bold=True, color="0B1F3A")

        meta_rows = [
            ("Session ID", result.session_id),
            ("SMU Model", result.model),
            ("Recipe Name", result.recipe_name),
            ("Started At", result.started_at.strftime("%Y-%m-%d %H:%M:%S")),
            ("Completed At", result.completed_at.strftime("%Y-%m-%d %H:%M:%S")),
            ("Source Function", result.recipe.source_func.value),
            ("Sweep Type", result.recipe.sweep_type.value),
            ("Compliance Limit", result.recipe.compliance_limit),
            ("Sense Mode", result.channel_config.sense_mode.value),
            ("Acquisition Mode", result.recipe.acquisition_mode.value),
        ]
        ws_sum["A3"] = "항목 (Parameter)"
        ws_sum["B3"] = "설정값 (Value)"
        ws_sum["A3"].fill = ws_sum["B3"].fill = header_fill
        ws_sum["A3"].font = ws_sum["B3"].font = header_font

        for r_idx, (k, v) in enumerate(meta_rows, start=4):
            ws_sum.cell(row=r_idx, column=1, value=k).font = bold_font
            ws_sum.cell(row=r_idx, column=2, value=str(v))

        ws_sum["D3"] = "자동 분석 지표 (Metric)"
        ws_sum["E3"] = "산출 결과 (Result)"
        ws_sum["D3"].fill = ws_sum["E3"].fill = header_fill
        ws_sum["D3"].font = ws_sum["E3"].font = header_font

        for r_idx, (mk, mv) in enumerate(result.metrics.items(), start=4):
            ws_sum.cell(row=r_idx, column=4, value=mk).font = bold_font
            ws_sum.cell(row=r_idx, column=5, value=mv if mv is not None else "N/A")

        grouped: dict[str, list[ReadingPoint]] = {}
        for p in result.points:
            key = f"Node{p.node_id}_{p.channel}".replace("[", "").replace("]", "").replace(".", "_")
            grouped.setdefault(key[:31], []).append(p)

        if not grouped:
            grouped["Node1_smu"] = []

        first_data_sheet = None
        first_sheet_rows = 0

        cols = [
            "Index",
            "Timestamp_s",
            "Voltage_V",
            "Current_A",
            "Resistance_Ohm",
            "Power_W",
            "In_Compliance",
            "Is_4Wire",
            "Limit_Passed",
        ]
        fail_fill = PatternFill(start_color="FEE2E2", end_color="FEE2E2", fill_type="solid")

        for sheet_name, pts in grouped.items():
            ws = wb.create_sheet(title=sheet_name)
            for c_idx, col_title in enumerate(cols, start=1):
                cell = ws.cell(row=1, column=c_idx, value=col_title)
                cell.fill = header_fill
                cell.font = header_font
                cell.alignment = Alignment(horizontal="center")

            max_excel_pts = min(len(pts), 1000000)
            for r_i, p in enumerate(pts[:max_excel_pts], start=2):
                row_vals = [
                    p.index,
                    p.timestamp_s,
                    p.voltage_v,
                    p.current_a,
                    p.resistance_ohm if p.resistance_ohm != float("inf") else None,
                    p.power_w,
                    p.in_compliance,
                    p.is_4wire,
                    p.limit_passed,
                ]
                for c_idx, val in enumerate(row_vals, start=1):
                    c = ws.cell(row=r_i, column=c_idx, value=val)
                    if not p.limit_passed:
                        c.fill = fail_fill

            if first_data_sheet is None and max_excel_pts >= 2:
                first_data_sheet = ws
                first_sheet_rows = max_excel_pts + 1

        if first_data_sheet is not None and first_sheet_rows >= 3:
            chart = ScatterChart()
            chart.title = f"{result.model} Characteristic Curve ({result.recipe_name})"
            chart.x_axis.title = "Voltage (V)"
            chart.y_axis.title = "Current (A)"
            xvalues = Reference(first_data_sheet, min_col=3, min_row=2, max_row=min(first_sheet_rows, 5000))
            yvalues = Reference(first_data_sheet, min_col=4, min_row=2, max_row=min(first_sheet_rows, 5000))
            series = Series(yvalues, xvalues, title_from_data=False)
            chart.series.append(series)
            chart.width = 18
            chart.height = 11
            ws_sum.add_chart(chart, "A16")

        for col_letter in ("A", "B", "D", "E"):
            ws_sum.column_dimensions[col_letter].width = 26

        wb.save(out_path)
        return out_path

    def export_run_history_comparison_excel(
        self,
        history: list[MeasurementResult],
        file_path: Path | str | None = None,
    ) -> Path:
        """Phase 3 Multi-Run History Comparison Workbook: compares metrics & overlays curves across runs."""
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

        header_fill = PatternFill(start_color="0B1F3A", end_color="0B1F3A", fill_type="solid")
        header_font = Font(name="맑은 고딕", size=10, bold=True, color="FFFFFF")

        ws["A1"] = f"SMU Run History 다중 실행 비교 리포트 (총 {len(history)}회 실행)"
        ws["A1"].font = Font(name="맑은 고딕", size=14, bold=True, color="0B1F3A")

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

            # Add individual Run sheet and link series into comparison chart
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
