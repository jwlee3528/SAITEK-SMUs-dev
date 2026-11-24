"""Main CLI & GUI Entry Point for Tektronix/Keithley SMU Advanced Control Suite (v3.0)."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

# Ensure 'src' directory is on sys.path even when main.py is executed directly (e.g. python src/smu_app/main.py)
_SRC_DIR = str(Path(__file__).resolve().parents[1])
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from smu_app.core.models import ChannelConfig, LogicalChannel, SweepRecipe
from smu_app.drivers.base_smu import create_smu_driver
from smu_app.features.sweep_engine import SweepExecutionEngine
from smu_app.storage.exporter import DataExporter


def run_cli_demo(model: str = "2450", nodes: int = 1) -> int:
    """Execute a headless CLI sweep + analysis + Excel/CSV/Parquet export using the Virtual SMU simulator."""
    driver = create_smu_driver(model)
    engine = SweepExecutionEngine(driver)
    cfg = ChannelConfig(logical_channel=LogicalChannel(node_id=1, channel_name=driver.profile.channel_names[0]))
    recipe = SweepRecipe(name=f"{model}_cli_demo", start_level=0.0, stop_level=1.2, points=51)
    result = engine.run_measurement(cfg=cfg, recipe=recipe, node_count=nodes)

    exporter = DataExporter()
    xlsx_path = exporter.export_excel_report(result)
    csv_path = exporter.export_streaming_csv(result)
    pq_path = exporter.export_parquet(result)

    print(f"[OK] Model={result.model} | Points={len(result.points)} | Metrics={result.metrics}")
    print(f"  -> Excel  : {xlsx_path}")
    print(f"  -> CSV    : {csv_path}")
    print(f"  -> Parquet: {pq_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SAITEK Tektronix/Keithley SMU Control & Analysis Suite (v3.0)")
    parser.add_argument("--headless-demo", action="store_true", help="Run headless CLI simulation and export demo")
    parser.add_argument("--model", default="2450", help="Target SMU model (2450, 2460, 2461, 2470, 2600B, 2601B-PULSE, 2606B, 2651A)")
    parser.add_argument("--nodes", type=int, default=1, help="Number of Master-Slave nodes (1~10)")
    parser.add_argument("--no-wizard", action="store_true", help="Skip the startup connection readiness wizard")
    args = parser.parse_args(argv)

    if args.headless_demo:
        return run_cli_demo(model=args.model, nodes=args.nodes)

    from smu_app.ui.main_window import launch_app

    return launch_app(show_startup_wizard=not args.no_wizard)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
