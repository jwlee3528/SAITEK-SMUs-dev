"""Phase 2 & UX Requirement 2/3 Upgraded Arbitrary Waveform Builder, Easy Parametric Generator, CSV/Excel Importer, and Previewer.

New in UX Upgrade (Requirements 2 & 3):
- `build_easy_waveform()`: User simply selects shape (SQUARE, PULSE, TRIANGLE, STAIRCASE, SINE),
  High/Peak level, Low/Base level, Duration(s), and Repeat Count (`count = 100` -> 100 cycles; `count = -1` -> Infinite Continuous mode).
- `delete_steps()` & `reindex_steps()`: Delete selected step rows or clear all steps in the arbitrary waveform editor.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from enum import Enum
import math
from pathlib import Path
import numpy as np
import pandas as pd

from smu_app.core.models import RepeatMode, SweepRecipe, SweepType, WaveformStep


class EasyWaveformShape(str, Enum):
    SQUARE = "SQUARE"          # 사각파 (High 레벨 유지 -> Low 레벨 유지)
    PULSE = "PULSE"            # 단극성 펄스파 (Pulse 폭 동안 High -> 나머지 Off 시간 동안 0V/Base)
    TRIANGLE = "TRIANGLE"      # 삼각파 (선형 상승 -> 선형 하강)
    STAIRCASE = "STAIRCASE"    # 계단파 (Start -> Stop 단계별 상승)
    SINE = "SINE"              # 사인파 (이산 스텝 근사 정현파)


@dataclass
class EasyWaveformResult:
    """Output of the Easy Parametric Waveform Generator (unit pattern steps + repeat mode/count)."""

    steps: list[WaveformStep]
    repeat_mode: RepeatMode
    repeat_count: int
    summary_ko: str


@dataclass
class WaveformPreviewData:
    """Computed time-domain preview trace and summary metrics for a custom waveform or sweep."""

    time_s: np.ndarray
    level_va: np.ndarray
    measure_times_s: np.ndarray
    measure_levels_va: np.ndarray
    single_cycle_duration_s: float
    total_duration_s: float
    single_cycle_points: int
    total_measure_points: int
    is_infinite_continuous: bool = False


class WaveformBuilder:
    """Builds, edits, deletes, imports, exports, and previews standard and easy parametric SMU waveforms."""

    @classmethod
    def build_easy_waveform(
        cls,
        shape: EasyWaveformShape | str = EasyWaveformShape.SQUARE,
        high_level: float = 5.0,
        low_level: float = 0.0,
        duration_s: float = 0.01,
        off_duration_s: float | None = None,
        sub_steps: int = 10,
        repeat_count: int = 1,
        compliance: float = 0.1,
        measure_enable: bool = True,
    ) -> EasyWaveformResult:
        """Requirement 3: Easy Parametric Waveform Generator.

        - User specifies shape, voltage/current level, duration, and repeat count (`repeat_count`).
        - If `repeat_count == -1` (or <= 0), sets `RepeatMode.CONTINUOUS` (infinite output until stop).
        - If `repeat_count == 1`, sets `RepeatMode.SINGLE` (single-shot output).
        - If `repeat_count > 1` (e.g. 100), sets `RepeatMode.FINITE` with `repeat_count = 100`.
        """
        shape_enum = EasyWaveformShape(shape) if isinstance(shape, str) else shape
        dur = max(1e-6, float(duration_s))
        off_dur = max(1e-6, float(off_duration_s)) if off_duration_s is not None else dur
        unit_steps: list[WaveformStep] = []

        if shape_enum == EasyWaveformShape.SQUARE:
            unit_steps = [
                WaveformStep(1, duration_s=dur, level=high_level, compliance_limit=compliance, measure_enable=measure_enable),
                WaveformStep(2, duration_s=off_dur, level=low_level, compliance_limit=compliance, measure_enable=measure_enable),
            ]
        elif shape_enum == EasyWaveformShape.PULSE:
            unit_steps = [
                WaveformStep(
                    1,
                    duration_s=dur,
                    level=high_level,
                    compliance_limit=compliance,
                    measure_enable=measure_enable,
                    is_pulse=True,
                    pulse_width_s=dur,
                    pulse_off_time_s=off_dur,
                ),
                WaveformStep(
                    2,
                    duration_s=off_dur,
                    level=low_level,
                    compliance_limit=compliance,
                    measure_enable=measure_enable,
                ),
            ]
        elif shape_enum == EasyWaveformShape.TRIANGLE:
            n_half = max(2, int(sub_steps) // 2)
            up = list(np.linspace(low_level, high_level, n_half))
            down = list(np.linspace(high_level, low_level, n_half))[1:]
            pts = up + down
            step_dt = max(1e-6, dur / max(1, len(pts)))
            unit_steps = [
                WaveformStep(i + 1, duration_s=step_dt, level=float(lv), compliance_limit=compliance, measure_enable=measure_enable)
                for i, lv in enumerate(pts)
            ]
        elif shape_enum == EasyWaveformShape.STAIRCASE:
            n_pts = max(2, int(sub_steps))
            pts = list(np.linspace(low_level, high_level, n_pts))
            step_dt = max(1e-6, dur / n_pts)
            unit_steps = [
                WaveformStep(i + 1, duration_s=step_dt, level=float(lv), compliance_limit=compliance, measure_enable=measure_enable)
                for i, lv in enumerate(pts)
            ]
        elif shape_enum == EasyWaveformShape.SINE:
            n_pts = max(4, int(sub_steps))
            mid = 0.5 * (high_level + low_level)
            amp = 0.5 * (high_level - low_level)
            step_dt = max(1e-6, dur / n_pts)
            unit_steps = [
                WaveformStep(
                    i + 1,
                    duration_s=step_dt,
                    level=float(mid + amp * math.sin(2.0 * math.pi * i / n_pts)),
                    compliance_limit=compliance,
                    measure_enable=measure_enable,
                )
                for i in range(n_pts)
            ]

        if repeat_count <= 0:
            rmode = RepeatMode.CONTINUOUS
            rcount = 1
            rep_desc = "무한 연속 출력 (-1: 정지 버튼 클릭 시까지 반복)"
        elif repeat_count == 1:
            rmode = RepeatMode.SINGLE
            rcount = 1
            rep_desc = "단발성 1회 출력 (Single-Shot)"
        else:
            rmode = RepeatMode.FINITE
            rcount = int(repeat_count)
            rep_desc = f"{rcount:,}회 연속 반복 출력 (Finite ×{rcount})"

        unit_dur = sum(s.duration_s for s in unit_steps)
        summary = (
            f"[{shape_enum.value}] 레벨: {low_level:g} ↔ {high_level:g} | "
            f"1주기: {unit_dur:.4g}s ({len(unit_steps)} 스텝) | 반복: {rep_desc}"
        )
        return EasyWaveformResult(
            steps=unit_steps,
            repeat_mode=rmode,
            repeat_count=rcount,
            summary_ko=summary,
        )

    @staticmethod
    def reindex_steps(steps: list[WaveformStep]) -> list[WaveformStep]:
        """Ensure step_index is sequential 1..N after insertion or deletion."""
        for idx, st in enumerate(steps, start=1):
            st.step_index = idx
        return steps

    @classmethod
    def delete_steps(cls, steps: list[WaveformStep], indices_zero_based: list[int]) -> list[WaveformStep]:
        """Requirement 2: Delete one or more step rows by 0-based table row index and re-index remaining steps."""
        remove_set = set(indices_zero_based)
        remaining = [st for i, st in enumerate(steps) if i not in remove_set]
        return cls.reindex_steps(remaining)

    @staticmethod
    def build_square_wave(
        high_level: float = 5.0,
        low_level: float = 0.0,
        high_time_s: float = 0.01,
        low_time_s: float = 0.01,
        cycles: int = 5,
        compliance: float = 0.1,
    ) -> list[WaveformStep]:
        steps: list[WaveformStep] = []
        idx = 1
        for _ in range(max(1, cycles)):
            steps.append(
                WaveformStep(
                    step_index=idx,
                    duration_s=high_time_s,
                    level=high_level,
                    compliance_limit=compliance,
                    measure_enable=True,
                )
            )
            idx += 1
            steps.append(
                WaveformStep(
                    step_index=idx,
                    duration_s=low_time_s,
                    level=low_level,
                    compliance_limit=compliance,
                    measure_enable=True,
                )
            )
            idx += 1
        return steps

    @staticmethod
    def build_triangle_wave(
        min_level: float = -2.0,
        max_level: float = 2.0,
        steps_per_half_cycle: int = 10,
        step_duration_s: float = 0.005,
        cycles: int = 2,
        compliance: float = 0.1,
    ) -> list[WaveformStep]:
        up = np.linspace(min_level, max_level, max(2, steps_per_half_cycle))
        down = np.linspace(max_level, min_level, max(2, steps_per_half_cycle))[1:]
        one_cycle = list(up) + list(down)

        steps: list[WaveformStep] = []
        idx = 1
        for c in range(max(1, cycles)):
            seq = one_cycle if c == 0 else one_cycle[1:]
            for lv in seq:
                steps.append(
                    WaveformStep(
                        step_index=idx,
                        duration_s=step_duration_s,
                        level=float(lv),
                        compliance_limit=compliance,
                        measure_enable=True,
                    )
                )
                idx += 1
        return steps

    @staticmethod
    def build_staircase_wave(
        start_level: float = 0.0,
        stop_level: float = 5.0,
        num_steps: int = 11,
        step_duration_s: float = 0.01,
        dual_return: bool = False,
        compliance: float = 0.1,
    ) -> list[WaveformStep]:
        levels = list(np.linspace(start_level, stop_level, max(2, num_steps)))
        if dual_return:
            levels = levels + list(reversed(levels[:-1]))
        return [
            WaveformStep(
                step_index=i + 1,
                duration_s=step_duration_s,
                level=float(lv),
                compliance_limit=compliance,
                measure_enable=True,
            )
            for i, lv in enumerate(levels)
        ]

    @staticmethod
    def import_steps_from_file(file_path: Path | str, default_compliance: float = 0.1) -> list[WaveformStep]:
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"파형 파일을 찾을 수 없습니다: {path}")

        if path.suffix.lower() in (".xlsx", ".xls"):
            df = pd.read_excel(path)
        else:
            df = pd.read_csv(path)

        col_map = {str(c).strip().lower(): c for c in df.columns}

        def find_col(candidates: list[str]) -> str | None:
            for cand in candidates:
                if cand in col_map:
                    return col_map[cand]
            return None

        dur_col = find_col(["duration_s", "time_s", "duration", "time", "시간", "시간(s)"])
        lvl_col = find_col(["level", "voltage", "current", "value", "level_va", "전압", "전류", "레벨"])
        lim_col = find_col(["compliance", "limit", "compliance_limit", "제한"])
        meas_col = find_col(["measure", "measure_enable", "측정", "측정여부"])

        if dur_col is None or lvl_col is None:
            raise ValueError(
                f"파형 파일에 필수 컬럼(duration_s, level)이 없습니다. 현재 컬럼: {list(df.columns)}"
            )

        steps: list[WaveformStep] = []
        for _, row in df.iterrows():
            dur = float(row[dur_col])
            lvl = float(row[lvl_col])
            lim = float(row[lim_col]) if (lim_col and pd.notna(row[lim_col])) else default_compliance
            meas = True
            if meas_col and pd.notna(row[meas_col]):
                raw_m = row[meas_col]
                if isinstance(raw_m, (int, float, bool)):
                    meas = bool(int(raw_m) != 0)
                else:
                    val = str(raw_m).strip().lower()
                    meas = val not in ("false", "0", "0.0", "no", "n", "off", "x")
            steps.append(
                WaveformStep(
                    step_index=len(steps) + 1,
                    duration_s=max(1e-6, dur),
                    level=lvl,
                    compliance_limit=lim,
                    measure_enable=meas,
                )
            )
        return steps

    @staticmethod
    def export_steps_to_csv(steps: list[WaveformStep], file_path: Path | str) -> Path:
        path = Path(file_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.writer(fh)
            writer.writerow(["Step", "Duration_s", "Level", "Compliance_Limit", "Measure_Enable"])
            for s in steps:
                writer.writerow([s.step_index, s.duration_s, s.level, s.compliance_limit, s.measure_enable])
        return path

    @staticmethod
    def generate_preview(recipe: SweepRecipe, max_preview_cycles: int = 10) -> WaveformPreviewData:
        """Compute piecewise-constant time-domain waveform trace and measurement sample markers."""
        if recipe.sweep_type == SweepType.LIST and recipe.custom_steps:
            base_steps = recipe.custom_steps
        else:
            levels = recipe.generate_levels()
            base_steps = [
                WaveformStep(
                    step_index=i + 1,
                    duration_s=max(1e-5, recipe.delay_s),
                    level=lv,
                    compliance_limit=recipe.compliance_limit,
                    measure_enable=True,
                )
                for i, lv in enumerate(levels)
            ]

        single_dur = sum(max(1e-6, s.duration_s) for s in base_steps)
        single_meas_pts = sum(1 for s in base_steps if s.measure_enable)
        is_inf = recipe.repeat_mode == RepeatMode.CONTINUOUS

        if recipe.repeat_mode == RepeatMode.SINGLE:
            total_cycles = 1
        elif recipe.repeat_mode == RepeatMode.FINITE:
            total_cycles = max(1, recipe.repeat_count)
        else:
            total_cycles = max_preview_cycles

        preview_cycles = min(total_cycles, max_preview_cycles)

        t_list: list[float] = []
        v_list: list[float] = []
        mt_list: list[float] = []
        mv_list: list[float] = []

        t_cursor = 0.0
        for _ in range(preview_cycles):
            for s in base_steps:
                dt = max(1e-6, s.duration_s)
                t_list.append(t_cursor)
                v_list.append(s.level)
                t_end = t_cursor + dt
                t_list.append(t_end)
                v_list.append(s.level)
                if s.measure_enable:
                    mt_list.append(t_cursor + dt * 0.85)
                    mv_list.append(s.level)
                t_cursor = t_end

        if recipe.end_Return_to_bias:
            t_list.append(t_cursor)
            v_list.append(recipe.bias_level)

        return WaveformPreviewData(
            time_s=np.array(t_list, dtype=np.float64),
            level_va=np.array(v_list, dtype=np.float64),
            measure_times_s=np.array(mt_list, dtype=np.float64),
            measure_levels_va=np.array(mv_list, dtype=np.float64),
            single_cycle_duration_s=single_dur,
            total_duration_s=single_dur * total_cycles,
            single_cycle_points=single_meas_pts,
            total_measure_points=single_meas_pts * total_cycles,
            is_infinite_continuous=is_inf,
        )
