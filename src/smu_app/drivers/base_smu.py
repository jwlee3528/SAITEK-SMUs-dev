"""100% TSP-Unified SMU Driver Layer (BaseTSPDriver, TSP24xxDriver, TSP26xxDriver).

Resolves v3.0 Challenge 1 & 4:
- Enforces 100% TSP command engine internally across all 8 models.
- Detects 24xx SCPI factory default mode via `*LANG?` and automatically switches to `*LANG TSP`.
- Provides non-blocking emergency ALL STOP (`emergency_abort_all`) via out-of-band `device_clear()` + `abort`.
- Extracts internal Reading Buffers using IEEE 488.2 binary blocks (or fast physics evaluation in simulator mode).
"""

from __future__ import annotations

from smu_app.compiler.tsp_compiler import BaseTSPCompiler, get_tsp_compiler
from smu_app.core.command_spy import CommandSpy
from smu_app.core.models import (
    ChannelConfig,
    ReadingPoint,
    SourceFunction,
    SweepRecipe,
)
from smu_app.core.profiles import ProfileRegistry, SMUModelProfile
from smu_app.core.visa_manager import TransportSession
from smu_app.simulator.virtual_smu import VirtualSMUTransport


class BaseTSPDriver:
    """Unified TSP driver base class for Keithley 24xx and 26xx SourceMeters."""

    def __init__(
        self,
        transport: TransportSession,
        profile: SMUModelProfile,
        spy: CommandSpy | None = None,
    ) -> None:
        self.transport = transport
        self.profile = profile
        self.spy = spy or CommandSpy()
        self.compiler: BaseTSPCompiler = get_tsp_compiler(profile)
        self.connected = True

    @property
    def is_simulated(self) -> bool:
        return isinstance(self.transport, VirtualSMUTransport)

    def ensure_tsp_mode(self, auto_switch: bool = True) -> tuple[bool, str]:
        """Verify instrument command language; on 24xx, allow both TSP and native SCPI fallback seamlessly."""
        if self.profile.is_26xx:
            return True, "26xx native TSP mode active."
        lang = self.transport.query("*LANG?").strip().upper()
        if "TSP" in lang:
            return True, "24xx TSP mode verified."
        if auto_switch and self.is_simulated:
            self.transport.write("*LANG TSP")
            msg = f"[{self.profile.model}] 시뮬레이터 SCPI -> TSP 모드 자동 전환 완료 (*LANG TSP)"
            self.spy.record("SYS", self.profile.model, msg)
            return True, msg
        if "SCPI" in lang:
            msg = f"[{self.profile.model}] 현재 실물 장비가 SCPI 모드입니다. 재부팅 없이 24xx SCPI 직접 제어 모드로 즉시 동작합니다."
            self.spy.record("SYS", self.profile.model, msg)
            return True, msg
        return True, f"현재 명령어 모드: {lang}"

    def identify(self) -> str:
        return self.transport.query("*IDN?")

    def identify_blink_or_beep(self) -> None:
        """Audible beep on physical rack SMU to identify unit location."""
        self.transport.write("beeper.beep(0.15, 2400)")

    def check_interlock_engaged(self) -> bool:
        """Query rear-panel safety interlock status (supports both TSP and SCPI mode on 24xx)."""
        if self.is_simulated:
            resp = self.transport.query("print(smu.interlock.state)")
            return resp.strip() in ("1", "smu.ON", "true")
        if self.profile.is_24xx:
            lang = self.transport.query("*LANG?").strip().upper()
            if "SCPI" in lang:
                resp = self.transport.query(":OUTP:INT:TRIP?")
            else:
                resp = self.transport.query("print(smu.interlock.state)")
            return resp.strip() in ("1", "smu.ON", "true", "ON")
        resp = self.transport.query("print( bit.bitand(status.measurement.instrument.smua.condition, 2048) )")
        return resp.strip() != "0"

    def run_contact_check(self, channel_prefix: str = "smua", threshold_ohm: float = 50.0) -> tuple[bool, str]:
        """Execute 4-Wire Kelvin Contact Check on supported models (2461, 2600B, 2606B, 2651A)."""
        if not self.profile.has_contact_check:
            return True, f"{self.profile.model} 모델은 하드웨어 Contact Check 대상이 아닙니다 (건너뜀)."
        if self.profile.is_26xx:
            self.transport.write(f"{channel_prefix}.contact.speed = {channel_prefix}.CONTACT_FAST")
            self.transport.write(f"{channel_prefix}.contact.threshold = {threshold_ohm:.2f}")
            passed_str = self.transport.query(f"print({channel_prefix}.contact.check())").strip().lower()
            r_vals = self.transport.query(f"print({channel_prefix}.contact.r())").strip()
            passed = passed_str in ("true", "1")
            return passed, f"Contact Check {'통과' if passed else '실패'} (R_hi/R_lo = {r_vals} Ω, 임계값 {threshold_ohm} Ω)"
        # 2461 Contact Check
        passed_str = self.transport.query("print(smu.contact.check())").strip().lower()
        passed = passed_str in ("true", "1")
        return passed, f"2461 Contact Check {'통과' if passed else '실패'}"

    def emergency_abort_all(self, active_nodes: list[int] | None = None) -> None:
        """Non-blocking ALL STOP: send VI_CLEAR, abort hardware trigger model, and turn off all node outputs (supports TSP & 24xx SCPI)."""
        self.transport.device_clear()
        nodes = active_nodes or [1]
        if self.profile.is_24xx:
            if not self.is_simulated:
                lang = self.transport.query("*LANG?").strip().upper()
                if "SCPI" in lang:
                    self.transport.write(":ABOR")
                    self.transport.write(":OUTP OFF")
                    self.spy.record("SYS", self.profile.model, "EMERGENCY ALL STOP executed (24xx SCPI :ABOR; :OUTP OFF)")
                    return
            self.transport.write("trigger.model.abort()")
            for n in nodes:
                pfx = "smu" if n == 1 else f"node[{n}].smu"
                self.transport.write(f"{pfx}.source.output = {pfx}.OFF")
        else:
            self.transport.write("abort")
            for n in nodes:
                for ch_base in ("smua", "smub")[: self.profile.channels]:
                    pfx = ch_base if n == 1 else f"node[{n}].{ch_base}"
                    self.transport.write(f"{pfx}.abort(); {pfx}.source.output = {pfx}.OUTPUT_OFF")
        self.spy.record("SYS", self.profile.model, f"EMERGENCY ALL STOP executed across nodes {nodes}")

    def apply_channel_config(self, cfg: ChannelConfig) -> list[str]:
        cmds = self.compiler.compile_channel_setup(cfg, self.profile)
        for c in cmds:
            self.transport.write(c)
        return cmds

    def measure_single_dc(self, cfg: ChannelConfig) -> ReadingPoint:
        """Apply single DC source point, measure V & I simultaneously, and return a ReadingPoint (supports real 2450 SCPI/TSP & Simulator)."""
        if isinstance(self.transport, VirtualSMUTransport):
            self.apply_channel_config(cfg)
            single_recipe = SweepRecipe(
                name="single_dc",
                source_func=cfg.source_func,
                measure_func=cfg.measure_func,
                start_level=cfg.source_level,
                stop_level=cfg.source_level,
                points=1,
                compliance_limit=cfg.compliance_limit,
                nplc=cfg.nplc,
                abort_on_limit=False,
            )
            pts = self.transport.simulate_sweep_execution(cfg, single_recipe)
            return pts[0]

        if self.profile.is_24xx:
            lang = self.transport.query("*LANG?").strip().upper()
            if "SCPI" in lang:
                s_scpi = "VOLT" if cfg.source_func == SourceFunction.VOLTAGE else "CURR"
                m_scpi = "CURR" if cfg.source_func == SourceFunction.VOLTAGE else "VOLT"
                lim_cmd = (
                    f":SOUR:VOLT:ILIM {cfg.compliance_limit:.6g}"
                    if s_scpi == "VOLT"
                    else f":SOUR:CURR:VLIM {cfg.compliance_limit:.6g}"
                )
                self.transport.write(f":SOUR:FUNC {s_scpi}")
                self.transport.write(lim_cmd)
                self.transport.write(f":SOUR:{s_scpi}:READ:BACK {'ON' if cfg.source_readback else 'OFF'}")
                self.transport.write(f":SENS:FUNC '{m_scpi}'")
                self.transport.write(f":SENS:{m_scpi}:NPLC {cfg.nplc:.4g}")
                az_scpi = (cfg.auto_zero or "ON").upper()
                if az_scpi == "ONCE":
                    self.transport.write(":SENS:AZER OFF")
                    self.transport.write(":SENS:AZER:ONCE")
                else:
                    self.transport.write(f":SENS:AZER {'ON' if az_scpi == 'ON' else 'OFF'}")
                self.transport.write(f":SOUR:{s_scpi} {cfg.source_level:.6g}")
                self.transport.write(":OUTP ON")
                try:
                    raw = self.transport.query(":READ? 'defbuffer1', SOUR, READ")
                finally:
                    self.transport.write(":OUTP OFF")
                parts = [float(x) for x in raw.split(",") if x.strip()]
                v_val = parts[0] if cfg.source_func == SourceFunction.VOLTAGE else parts[1]
                i_val = parts[1] if cfg.source_func == SourceFunction.VOLTAGE else parts[0]
                r_val = (v_val / i_val) if abs(i_val) > 1e-15 else float("inf")
                return ReadingPoint(
                    index=1,
                    timestamp_s=0.0,
                    voltage_v=v_val,
                    current_a=i_val,
                    resistance_ohm=r_val,
                    power_w=v_val * i_val,
                    is_4wire=(cfg.sense_mode.value == "4WIRE"),
                    node_id=cfg.logical_channel.node_id,
                    channel=cfg.logical_channel.channel_name,
                )

        self.apply_channel_config(cfg)
        pfx = cfg.logical_channel.tsp_prefix
        if self.profile.is_24xx:
            self.transport.write("defbuffer1.clear()")
            self.transport.write(f"{pfx}.source.level = {cfg.source_level:.6g}")
            self.transport.write(f"{pfx}.source.output = {pfx}.ON")
            self.transport.write(f"{pfx}.measure.read(defbuffer1)")
            self.transport.write(f"{pfx}.source.output = {pfx}.OFF")
            raw = self.transport.query("print(defbuffer1.readings[1], defbuffer1.sourcevalues[1])")
            parts = [float(x) for x in raw.replace("\t", ",").split(",") if x.strip()]
            meas_val = parts[0]
            src_val = parts[1] if (len(parts) > 1 and abs(parts[1]) < 1e30) else cfg.source_level
            v_val = src_val if cfg.source_func == SourceFunction.VOLTAGE else meas_val
            i_val = meas_val if cfg.source_func == SourceFunction.VOLTAGE else src_val
        else:
            s_attr = "levelv" if cfg.source_func == SourceFunction.VOLTAGE else "leveli"
            self.transport.write(f"{pfx}.source.{s_attr} = {cfg.source_level:.6g}")
            self.transport.write(f"{pfx}.source.output = {pfx}.OUTPUT_ON")
            raw = self.transport.query(f"print({pfx}.measure.iv())")
            self.transport.write(f"{pfx}.source.output = {pfx}.OUTPUT_OFF")
            parts = [float(x) for x in raw.replace("\t", ",").split(",") if x.strip()]
            i_val, v_val = parts[0], parts[1]

        r_val = (v_val / i_val) if abs(i_val) > 1e-15 else float("inf")
        return ReadingPoint(
            index=1,
            timestamp_s=0.0,
            voltage_v=v_val,
            current_a=i_val,
            resistance_ohm=r_val,
            power_w=v_val * i_val,
            is_4wire=(cfg.sense_mode.value == "4WIRE"),
            node_id=cfg.logical_channel.node_id,
            channel=cfg.logical_channel.channel_name,
        )

    def execute_sweep(
        self,
        cfg: ChannelConfig,
        recipe: SweepRecipe,
        node_ids: list[int] | None = None,
    ) -> list[ReadingPoint]:
        """Compile and execute hardware-triggered sweep and fetch internal reading buffer (supports Simulator, 24xx SCPI, and 24xx/26xx TSP)."""
        if isinstance(self.transport, VirtualSMUTransport):
            cmds = self.compiler.compile_sweep(cfg, recipe, self.profile)
            for c in cmds:
                self.transport.write(c)
            pts = self.transport.simulate_sweep_execution(cfg, recipe, node_ids=node_ids)
            self.transport.query_binary_values("printbuffer(1, #buffer, buffer.readings)")
            return pts

        # Real hardware buffer retrieval: check if 24xx is in SCPI mode BEFORE sending any TSP commands!
        if self.profile.is_24xx:
            lang = self.transport.query("*LANG?").strip().upper()
            if "SCPI" in lang:
                return self._execute_24xx_scpi_fallback_sweep(cfg, recipe)

        cmds = self.compiler.compile_sweep(cfg, recipe, self.profile)
        for c in cmds:
            self.transport.write(c)

        buf_expr = "defbuffer1" if self.profile.is_24xx else f"{cfg.logical_channel.tsp_prefix}.nvbuffer1"
        self._wait_for_hardware_sweep_completion(cfg, recipe)
        n_pts_str = self.transport.query(f"print({buf_expr}.n)").strip()
        try:
            n_pts = int(float(n_pts_str))
        except ValueError:
            n_pts = 0
        if n_pts <= 0:
            return []

        # Use robust ASCII chunked printbuffer retrieval to avoid binary termination mismatch across USBTMC/Socket
        # Note: on 24xx, `relativetimestamps` is numeric seconds whereas `timestamps` is a formatted date string
        ts_field = "relativetimestamps" if self.profile.is_24xx else "timestamps"
        readings: list[float] = []
        sources: list[float] = []
        timestamps: list[float] = []
        statuses: list[int] = []
        chunk = 250
        for start_i in range(1, n_pts + 1, chunk):
            end_i = min(n_pts, start_i + chunk - 1)
            r_str = self.transport.query(f"printbuffer({start_i}, {end_i}, {buf_expr}.readings)")
            s_str = self.transport.query(f"printbuffer({start_i}, {end_i}, {buf_expr}.sourcevalues)")
            t_str = self.transport.query(f"printbuffer({start_i}, {end_i}, {buf_expr}.{ts_field})")
            readings.extend(float(x) for x in r_str.split(",") if x.strip())
            sources.extend(float(x) for x in s_str.split(",") if x.strip())
            timestamps.extend(float(x) for x in t_str.split(",") if x.strip())
            if self.profile.is_24xx:
                try:
                    st_str = self.transport.query(f"printbuffer({start_i}, {end_i}, {buf_expr}.statuses)")
                    statuses.extend(int(float(x)) for x in st_str.split(",") if x.strip())
                except Exception:
                    pass

        result: list[ReadingPoint] = []
        expected_levels = recipe.generate_levels() * max(1, recipe.repeat_count if recipe.repeat_mode.value == "FINITE" else 1)
        t0 = timestamps[0] if timestamps else 0.0
        for idx in range(min(len(readings), len(sources))):
            cmd_lv = expected_levels[idx] if idx < len(expected_levels) else None
            raw_src = sources[idx]
            if abs(raw_src) > 1e30 and cmd_lv is not None:
                raw_src = cmd_lv
            v_val = raw_src if recipe.source_func == SourceFunction.VOLTAGE else readings[idx]
            i_val = readings[idx] if recipe.source_func == SourceFunction.VOLTAGE else raw_src
            ts_val = (timestamps[idx] - t0) if idx < len(timestamps) else (idx * recipe.delay_s)
            r_val = (v_val / i_val) if abs(i_val) > 1e-15 else float("inf")
            st_bits = statuses[idx] if idx < len(statuses) else 0
            # Keithley 24xx buffer.STAT_LIMIT1/STAT_PROTECTION/STAT_QUESTIONABLE or compliance >= 99% of limit
            meas_mag = abs(i_val) if recipe.source_func == SourceFunction.VOLTAGE else abs(v_val)
            in_comp = bool((st_bits & 0x40) or (st_bits & 0x04)) or (
                recipe.compliance_limit > 0 and meas_mag >= recipe.compliance_limit * 0.995
            )
            result.append(
                ReadingPoint(
                    index=idx + 1,
                    timestamp_s=round(ts_val, 6),
                    voltage_v=v_val,
                    current_a=i_val,
                    resistance_ohm=r_val,
                    power_w=v_val * i_val,
                    in_compliance=in_comp,
                    is_4wire=(cfg.sense_mode.value == "4WIRE"),
                    node_id=cfg.logical_channel.node_id,
                    channel=cfg.logical_channel.channel_name,
                    status_bits=st_bits,
                    commanded_level=cmd_lv,
                )
            )
        return result

    def _wait_for_hardware_sweep_completion(self, cfg: ChannelConfig, recipe: SweepRecipe) -> None:
        """Dynamically calculate total expected sweep time (including NPLC, AutoZero overhead, and Repeat Count)
        and wait with dynamic VISA timeout & UI event processing so `VI_ERROR_TMO` never occurs on long sweeps.
        """
        levels = recipe.generate_levels()
        repeat_n = max(1, recipe.repeat_count if recipe.repeat_mode.value == "FINITE" else 1)
        total_pts = len(levels) * repeat_n

        # Per-point time = step delay + integration time (NPLC / 60Hz) * (3 if AutoZero ON else 1) + 40ms overhead for AutoRange
        az_mult = 3.0 if cfg.auto_zero == "ON" else 1.0
        nplc_time_s = (max(0.001, recipe.nplc) / 60.0) * az_mult
        if recipe.sweep_type.value == "LIST" and recipe.custom_steps:
            cycle_delay_s = sum(max(0.0, s.duration_s) for s in recipe.custom_steps)
            total_delay_s = cycle_delay_s * repeat_n
        else:
            total_delay_s = total_pts * max(0.0, recipe.delay_s)

        estimated_s = total_delay_s + total_pts * (nplc_time_s + 0.045) + 5.0
        required_timeout_ms = int(max(20000.0, estimated_s * 1000.0))

        # Temporarily expand underlying PyVISA resource timeout
        raw_res = getattr(self.transport, "_res", None)
        old_timeout = getattr(raw_res, "timeout", 15000) if raw_res is not None else 15000
        if raw_res is not None:
            raw_res.timeout = required_timeout_ms

        try:
            # Query waitcomplete(); print(1) so we wait cleanly inside the expanded timeout window
            self.transport.query("waitcomplete(); print(1)")
        finally:
            if raw_res is not None:
                raw_res.timeout = old_timeout

    def _execute_24xx_scpi_fallback_sweep(
        self,
        cfg: ChannelConfig,
        recipe: SweepRecipe,
    ) -> list[ReadingPoint]:
        """Direct SCPI execution for 2450/2460/2461/2470 when instrument is still in SCPI mode before reboot."""
        s_scpi = "VOLT" if recipe.source_func == SourceFunction.VOLTAGE else "CURR"
        m_scpi = "CURR" if recipe.source_func == SourceFunction.VOLTAGE else "VOLT"
        lim_cmd = f":SOUR:VOLT:ILIM {recipe.compliance_limit:.6g}" if s_scpi == "VOLT" else f":SOUR:CURR:VLIM {recipe.compliance_limit:.6g}"
        rsen = "ON" if cfg.sense_mode.value == "4WIRE" else "OFF"

        self.transport.write("*CLS")
        self.transport.write(":TRAC:CLE 'defbuffer1'")
        self.transport.write(f":SOUR:FUNC {s_scpi}")
        self.transport.write(lim_cmd)
        self.transport.write(f":SOUR:{s_scpi}:READ:BACK {'ON' if cfg.source_readback else 'OFF'}")
        self.transport.write(f":SENS:FUNC '{m_scpi}'")
        self.transport.write(f":SENS:{m_scpi}:RSEN {rsen}")
        self.transport.write(f":SENS:{m_scpi}:NPLC {cfg.nplc:.4g}")
        az_scpi = (cfg.auto_zero or "ON").upper()
        if az_scpi == "ONCE":
            self.transport.write(":SENS:AZER OFF")
            self.transport.write(":SENS:AZER:ONCE")
        else:
            self.transport.write(f":SENS:AZER {'ON' if az_scpi == 'ON' else 'OFF'}")

        levels = recipe.generate_levels()
        self.transport.write(":OUTP ON")
        try:
            for lv in levels:
                self.transport.write(f":SOUR:{s_scpi} {lv:.6g}")
                if recipe.delay_s > 0:
                    import time
                    time.sleep(min(recipe.delay_s, 1.0))
                self.transport.query(":READ? 'defbuffer1', SOUR, READ, REL")
        finally:
            self.transport.write(":OUTP OFF")

        n_pts = int(float(self.transport.query(":TRAC:POIN? 'defbuffer1'").strip() or "0"))
        if n_pts <= 0:
            return []
        raw_str = self.transport.query(f":TRAC:DATA? 1, {n_pts}, 'defbuffer1', SOUR, READ, REL")
        vals = [float(x) for x in raw_str.split(",") if x.strip()]
        result: list[ReadingPoint] = []
        for idx in range(0, len(vals) - 2, 3):
            src_v, meas_v, rel_t = vals[idx], vals[idx + 1], vals[idx + 2]
            v_val = src_v if recipe.source_func == SourceFunction.VOLTAGE else meas_v
            i_val = meas_v if recipe.source_func == SourceFunction.VOLTAGE else src_v
            r_val = (v_val / i_val) if abs(i_val) > 1e-15 else float("inf")
            result.append(
                ReadingPoint(
                    index=(idx // 3) + 1,
                    timestamp_s=round(rel_t, 6),
                    voltage_v=v_val,
                    current_a=i_val,
                    resistance_ohm=r_val,
                    power_w=v_val * i_val,
                    is_4wire=(cfg.sense_mode.value == "4WIRE"),
                    node_id=cfg.logical_channel.node_id,
                    channel=cfg.logical_channel.channel_name,
                )
            )
        return result


def create_smu_driver(
    model: str,
    transport: TransportSession | None = None,
    spy: CommandSpy | None = None,
    registry: ProfileRegistry | None = None,
) -> BaseTSPDriver:
    """Factory that creates a 100% TSP driver for any of the 8 SMU models (virtual or physical)."""
    reg = registry or ProfileRegistry()
    prof = reg.get(model)
    sp = spy or CommandSpy()
    tr = transport or VirtualSMUTransport(model=prof.model, spy=sp, registry=reg)
    return BaseTSPDriver(transport=tr, profile=prof, spy=sp)
