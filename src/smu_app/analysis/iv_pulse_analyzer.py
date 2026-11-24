"""Semiconductor I-V, High-Speed Pulse Transient Parameter Extractor, Dual Cursor Calculator, and LTTB Downsampler.

Implements v3.0 Section 07 (Visualization & Data) & Phase 3 Pulse Metrics:
- Real-time statistics (Mean, Min, Max, Peak-to-Peak, StdDev, Limit Fail Count).
- Semiconductor parameters: Differential resistance (dV/dI), Threshold voltage (Vth), Breakdown voltage (Vbd), Leakage current (Ileak).
- Phase 3 Pulse transient metrics: Rise Time (10-90%), Fall Time (90-10%), Overshoot (%), Settling Time (to +/-2% band),
  Pulse Top Mean V & I.
- Dual Cursor analysis calculator (ΔX, ΔY, Slope dY/dX, Differential Resistance 1/Slope).
- LTTB (Largest-Triangle-Three-Buckets) fast downsampling for 100k~1M point plots.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import numpy as np

from smu_app.core.models import ReadingPoint, SweepRecipe


@dataclass(frozen=True)
class DualCursorSummary:
    """Computed metrics between two interactive cursor positions on an I-V or Time-Domain curve."""

    x1: float
    y1: float
    x2: float
    y2: float
    delta_x: float
    delta_y: float
    slope: float
    inverse_slope_ohm: float


def compute_dual_cursor_metrics(
    x_arr: np.ndarray,
    y_arr: np.ndarray,
    cursor1_x: float,
    cursor2_x: float,
) -> DualCursorSummary:
    """Interpolate Y values at two cursor X positions and compute ΔX, ΔY, Slope (1/R), and Resistance (R)."""
    if len(x_arr) == 0:
        return DualCursorSummary(cursor1_x, 0.0, cursor2_x, 0.0, 0.0, 0.0, 0.0, float("inf"))

    idx1 = int(np.argmin(np.abs(x_arr - cursor1_x)))
    idx2 = int(np.argmin(np.abs(x_arr - cursor2_x)))
    x1, y1 = float(x_arr[idx1]), float(y_arr[idx1])
    x2, y2 = float(x_arr[idx2]), float(y_arr[idx2])
    dx = x2 - x1
    dy = y2 - y1
    slope = (dy / dx) if abs(dx) > 1e-18 else 0.0
    inv_slope = (dx / dy) if abs(dy) > 1e-18 else float("inf")
    return DualCursorSummary(
        x1=x1,
        y1=y1,
        x2=x2,
        y2=y2,
        delta_x=dx,
        delta_y=dy,
        slope=slope,
        inverse_slope_ohm=inv_slope,
    )


def lttb_downsample(x: np.ndarray, y: np.ndarray, threshold: int = 2000) -> tuple[np.ndarray, np.ndarray]:
    """Largest-Triangle-Three-Buckets (LTTB) downsampling preserving visual peaks and valleys."""
    n = len(x)
    if threshold >= n or threshold < 3:
        return x, y

    sampled_x = np.empty(threshold, dtype=np.float64)
    sampled_y = np.empty(threshold, dtype=np.float64)
    sampled_x[0] = x[0]
    sampled_y[0] = y[0]
    sampled_x[-1] = x[-1]
    sampled_y[-1] = y[-1]

    every = (n - 2) / (threshold - 2)
    a = 0

    for i in range(threshold - 2):
        avg_range_start = int(np.floor((i + 1) * every)) + 1
        avg_range_end = min(int(np.floor((i + 2) * every)) + 1, n)
        if avg_range_end > avg_range_start:
            avg_x = float(np.mean(x[avg_range_start:avg_range_end]))
            avg_y = float(np.mean(y[avg_range_start:avg_range_end]))
        else:
            avg_x = float(x[min(avg_range_start, n - 1)])
            avg_y = float(y[min(avg_range_start, n - 1)])

        range_offs = int(np.floor(i * every)) + 1
        range_to = min(int(np.floor((i + 1) * every)) + 1, n - 1)
        if range_to <= range_offs:
            range_to = min(range_offs + 1, n - 1)

        px = x[a]
        py = y[a]
        chunk_x = x[range_offs:range_to]
        chunk_y = y[range_offs:range_to]
        areas = np.abs((px - avg_x) * (chunk_y - py) - (px - chunk_x) * (avg_y - py))
        max_idx = int(np.argmax(areas)) if len(areas) > 0 else 0
        next_a = range_offs + max_idx

        sampled_x[i + 1] = x[next_a]
        sampled_y[i + 1] = y[next_a]
        a = next_a

    return sampled_x, sampled_y


def compute_differential_resistance(voltages: np.ndarray, currents: np.ndarray) -> np.ndarray:
    """Compute dV/dI (differential resistance in Ohms) across an I-V curve."""
    if len(voltages) < 2:
        return np.full_like(voltages, np.nan, dtype=np.float64)
    dv = np.gradient(voltages)
    di = np.gradient(currents)
    with np.errstate(divide="ignore", invalid="ignore"):
        dv_di = np.where(np.abs(di) > 1e-18, dv / di, np.nan)
    return dv_di


def analyze_measurement_points(points: list[ReadingPoint], recipe: SweepRecipe | None = None) -> dict[str, Any]:
    """Extract summary statistics, semiconductor I-V parameters, and Phase 3 pulse transient metrics."""
    if not points:
        return {"count": 0}

    v_arr = np.array([p.voltage_v for p in points], dtype=np.float64)
    i_arr = np.array([p.current_a for p in points], dtype=np.float64)
    t_arr = np.array([p.timestamp_s for p in points], dtype=np.float64)
    p_arr = v_arr * i_arr
    dv_di = compute_differential_resistance(v_arr, i_arr)

    fail_count = sum(1 for p in points if not p.limit_passed)
    compliance_count = sum(1 for p in points if p.in_compliance)

    # 1. Threshold Voltage (Vth): first forward voltage where I >= 1 mA (or 5% of max current)
    i_max_pos = float(np.max(i_arr)) if len(i_arr) else 0.0
    vth_target_i = max(1e-4, i_max_pos * 0.05)
    pos_mask = (v_arr > 0) & (i_arr >= vth_target_i)
    v_th = float(v_arr[pos_mask][0]) if np.any(pos_mask) else None

    # 2. Breakdown Voltage (Vbd): voltage where |I| spikes past 10% of compliance at negative or high V
    comp_target = (abs(recipe.compliance_limit) * 0.1) if recipe else 1e-3
    bd_mask = (np.abs(v_arr) > 20.0) & (np.abs(i_arr) >= comp_target)
    v_bd = float(v_arr[bd_mask][0]) if np.any(bd_mask) else None

    # 3. Leakage Current (Ileak): mean |I| at low voltage (|V| <= 1.0V)
    leak_mask = (np.abs(v_arr) > 0.01) & (np.abs(v_arr) <= 1.0)
    i_leak = float(np.mean(np.abs(i_arr[leak_mask]))) if np.any(leak_mask) else float(np.min(np.abs(i_arr)))

    # 4. Phase 3 Pulse Transient Metrics (Rise Time 10-90%, Fall Time 90-10%, Overshoot %, Settling Time, Pulse Top V/I)
    i_min, i_max = float(np.min(i_arr)), float(np.max(i_arr))
    span = i_max - i_min
    rise_time_s = None
    fall_time_s = None
    settling_time_s = None
    overshoot_pct = 0.0
    pulse_top_v = float(np.mean(v_arr))
    pulse_top_i = float(np.mean(i_arr))

    if span > 1e-12 and len(t_arr) >= 6:
        # Estimate pulse top plateau using 60%~85% window of high region
        peak_idx = int(np.argmax(i_arr))
        high_indices = np.where(i_arr >= i_min + 0.5 * span)[0]
        if len(high_indices) >= 4:
            q_start = high_indices[len(high_indices) // 2]
            q_end = high_indices[min(len(high_indices) - 1, int(len(high_indices) * 0.85))]
            if q_end >= q_start:
                pulse_top_i = float(np.mean(i_arr[q_start : q_end + 1]))
                pulse_top_v = float(np.mean(v_arr[q_start : q_end + 1]))
        else:
            pulse_top_i = float(np.median(i_arr[len(i_arr) // 2 :]))
            pulse_top_v = float(np.median(v_arr[len(v_arr) // 2 :]))

        top_span = max(abs(pulse_top_i - i_min), span * 0.5)
        lvl_10 = i_min + 0.10 * top_span
        lvl_90 = i_min + 0.90 * top_span

        # Leading edge Rise Time (10% -> 90%)
        idx_10 = np.where(i_arr >= lvl_10)[0]
        idx_90 = np.where(i_arr >= lvl_90)[0]
        if len(idx_10) > 0 and len(idx_90) > 0 and idx_90[0] >= idx_10[0]:
            rise_time_s = float(max(0.0, t_arr[idx_90[0]] - t_arr[idx_10[0]]))

        # Trailing edge Fall Time (90% -> 10% after peak)
        if len(high_indices) >= 2:
            last_90 = high_indices[-1]
            after_90 = np.where((np.arange(len(i_arr)) > last_90) & (i_arr <= lvl_10))[0]
            if len(after_90) > 0:
                fall_time_s = float(max(0.0, t_arr[after_90[0]] - t_arr[last_90]))

        # Overshoot (%) relative to Pulse Top plateau
        if abs(pulse_top_i) > 1e-12 and i_max > pulse_top_i:
            overshoot_pct = float(((i_max - pulse_top_i) / abs(pulse_top_i)) * 100.0)

        # Settling Time (time from 10% leading edge until signal stays within +/-2% of pulse_top_i)
        if len(idx_10) > 0 and len(high_indices) >= 4:
            band = 0.03 * top_span
            plateau_slice = i_arr[idx_10[0] : high_indices[-1] + 1]
            outside = np.where(np.abs(plateau_slice - pulse_top_i) > band)[0]
            if len(outside) > 0:
                settle_idx = min(idx_10[0] + outside[-1] + 1, len(t_arr) - 1)
                settling_time_s = float(max(0.0, t_arr[settle_idx] - t_arr[idx_10[0]]))
            else:
                settling_time_s = rise_time_s

    finite_dvdi = dv_di[np.isfinite(dv_di)]

    return {
        "count": len(points),
        "v_min": float(np.min(v_arr)),
        "v_max": float(np.max(v_arr)),
        "v_mean": float(np.mean(v_arr)),
        "i_min": i_min,
        "i_max": i_max,
        "i_mean": float(np.mean(i_arr)),
        "i_std": float(np.std(i_arr)),
        "i_pk_to_pk": float(span),
        "p_max_w": float(np.max(np.abs(p_arr))),
        "dv_di_median_ohm": float(np.median(finite_dvdi)) if len(finite_dvdi) > 0 else None,
        "v_threshold_v": v_th,
        "v_breakdown_v": v_bd,
        "i_leakage_a": i_leak,
        "rise_time_10_90_s": rise_time_s,
        "fall_time_90_10_s": fall_time_s,
        "settling_time_s": settling_time_s,
        "overshoot_pct": round(overshoot_pct, 2),
        "pulse_top_v": pulse_top_v,
        "pulse_top_i": pulse_top_i,
        "compliance_hit_count": compliance_count,
        "limit_fail_count": fail_count,
    }
