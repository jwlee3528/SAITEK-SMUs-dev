"""Unified VISA Session & Transport Manager (USB, GPIB, LAN Raw Socket 5025, DST 5030, Simulator)."""

from __future__ import annotations

import socket
import struct
import time
from dataclasses import dataclass
from typing import Any, Protocol

from smu_app.core.command_spy import CommandSpy


@dataclass
class DiscoveredInstrument:
    """Metadata discovered during multi-interface VISA scan."""

    resource_name: str
    interface_type: str  # "USB", "LAN_SOCKET", "LAN_VXI11", "GPIB", "SIMULATOR"
    manufacturer: str = "KEITHLEY"
    model: str = "2450"
    serial_number: str = "SIM0001"
    firmware_version: str = "1.7.12"
    current_language: str = "TSP"
    is_simulator: bool = False


class TransportSession(Protocol):
    """Protocol implemented by both real PyVISA resources and VirtualSMUTransport."""

    resource_name: str

    def write(self, command: str) -> None: ...
    def query(self, command: str) -> str: ...
    def query_binary_values(self, command: str, datatype: str = "d") -> list[float]: ...
    def device_clear(self) -> None: ...
    def close(self) -> None: ...


class PyVISATransportSession:
    """Wraps a real PyVISA MessageBasedResource with CommandSpy logging and IEEE 488.2 binary parsing."""

    def __init__(self, visa_resource: Any, spy: CommandSpy, target_label: str = "SMU") -> None:
        self._res = visa_resource
        self.resource_name: str = getattr(visa_resource, "resource_name", target_label)
        self._spy = spy
        self._label = target_label

    def write(self, command: str) -> None:
        t0 = time.perf_counter()
        self._res.write(command)
        dt = (time.perf_counter() - t0) * 1000.0
        self._spy.record("TX", self._label, command, elapsed_ms=dt)

    def query(self, command: str) -> str:
        t0 = time.perf_counter()
        self._spy.record("TX", self._label, command)
        resp = str(self._res.query(command)).strip()
        dt = (time.perf_counter() - t0) * 1000.0
        self._spy.record("RX", self._label, resp, elapsed_ms=dt)
        return resp

    def query_binary_values(self, command: str, datatype: str = "d") -> list[float]:
        t0 = time.perf_counter()
        self._spy.record("TX", self._label, f"{command} [IEEE488.2 BINARY '{datatype}']")
        vals = list(self._res.query_binary_values(command, datatype=datatype, is_big_endian=False))
        dt = (time.perf_counter() - t0) * 1000.0
        self._spy.record("RX", self._label, f"<Binary Block: {len(vals)} points>", elapsed_ms=dt)
        return [float(v) for v in vals]

    def device_clear(self) -> None:
        """Out-of-band VI_CLEAR for non-blocking emergency ALL STOP."""
        t0 = time.perf_counter()
        try:
            self._res.clear()
        except Exception:
            pass
        dt = (time.perf_counter() - t0) * 1000.0
        self._spy.record("SYS", self._label, "VI_CLEAR (Device Clear)", elapsed_ms=dt)

    def close(self) -> None:
        try:
            self._res.close()
        finally:
            self._spy.record("SYS", self._label, "Session Closed")


def parse_ieee4882_binary_block(raw_bytes: bytes, datatype: str = "d") -> list[float]:
    """Parse an IEEE 488.2 definite-length binary block (#<num_digits><byte_count><payload>)."""
    idx = raw_bytes.find(b"#")
    if idx < 0 or len(raw_bytes) < idx + 2:
        raise ValueError("Invalid IEEE 488.2 binary header: missing '#'")
    num_digits = int(chr(raw_bytes[idx + 1]))
    header_end = idx + 2 + num_digits
    byte_count = int(raw_bytes[idx + 2 : header_end].decode("ascii"))
    payload = raw_bytes[header_end : header_end + byte_count]
    elem_size = struct.calcsize(f"<{datatype}")
    count = len(payload) // elem_size
    return list(struct.unpack(f"<{count}{datatype}", payload[: count * elem_size]))


def build_ieee4882_binary_block(values: list[float], datatype: str = "d") -> bytes:
    """Encode float list into an IEEE 488.2 definite-length binary block."""
    payload = struct.pack(f"<{len(values)}{datatype}", *values)
    len_str = str(len(payload)).encode("ascii")
    header = b"#" + str(len(len_str)).encode("ascii") + len_str
    return header + payload + b"\n"


class VISAConnectionManager:
    """Manages VISA resource discovery, LAN Dead Socket Termination (Port 5030), and Simulator sessions."""

    def __init__(self, spy: CommandSpy | None = None) -> None:
        self.spy = spy or CommandSpy()
        self._rm: Any = None

    @staticmethod
    def build_raw_socket_resource(ip_address: str, port: int = 5025) -> str:
        return f"TCPIP0::{ip_address}::{port}::SOCKET"

    @staticmethod
    def terminate_dead_lan_socket(ip_address: str, dst_port: int = 5030, timeout_s: float = 2.0) -> bool:
        """Connect briefly to Keithley Dead Socket Termination (DST) port 5030 to clear hung LAN sessions."""
        try:
            with socket.create_connection((ip_address, dst_port), timeout=timeout_s):
                time.sleep(0.05)
            return True
        except OSError:
            return False

    @staticmethod
    def parse_idn_string(idn: str) -> tuple[str, str, str, str]:
        """Parse '*IDN?' response into (manufacturer, model, serial, firmware)."""
        parts = [p.strip() for p in idn.split(",")]
        while len(parts) < 4:
            parts.append("UNKNOWN")
        manufacturer = parts[0]
        model = parts[1].upper().replace("MODEL", "").strip()
        serial = parts[2]
        firmware = parts[3]
        return manufacturer, model, serial, firmware

    def discover_instruments(self, include_simulators: bool = True) -> list[DiscoveredInstrument]:
        """Scan connected VISA instruments plus built-in Virtual SMU simulators for all 8 models."""
        found: list[DiscoveredInstrument] = []
        try:
            import pyvisa

            if self._rm is None:
                self._rm = pyvisa.ResourceManager()
            for res_name in self._rm.list_resources():
                upper_r = res_name.upper()
                # Filter out legacy PC COM/LPT serial ports (ASRL*) so we never accidentally probe non-SMU serial ports
                if upper_r.startswith("ASRL"):
                    continue
                iface = "USB" if "USB" in upper_r else ("GPIB" if "GPIB" in upper_r else "LAN")
                found.append(
                    DiscoveredInstrument(
                        resource_name=res_name,
                        interface_type=iface,
                        is_simulator=False,
                    )
                )
        except Exception as exc:
            self.spy.record("SYS", "VISA", f"Hardware VISA scan skipped/fallback: {exc}")

        if include_simulators:
            for model in ["2450", "2460", "2461", "2470", "2600B", "2601B-PULSE", "2606B", "2651A"]:
                found.append(
                    DiscoveredInstrument(
                        resource_name=f"SIM::{model}::INSTR",
                        interface_type="SIMULATOR",
                        manufacturer="KEITHLEY INSTRUMENTS",
                        model=model,
                        serial_number=f"SIM-{model}-01",
                        firmware_version="v3.0-SIM",
                        current_language="TSP",
                        is_simulator=True,
                    )
                )
        return found

    def open_transport_session(
        self,
        resource_name: str,
        model: str = "2450",
        timeout_ms: int = 15000,
    ) -> TransportSession:
        """Open a real PyVISA hardware session if `resource_name` is a physical VISA address,
        or return a VirtualSMUTransport if `resource_name` starts with 'SIM::'.
        """
        from smu_app.simulator.virtual_smu import VirtualSMUTransport

        clean_res = (resource_name or "").strip()
        if not clean_res or clean_res.upper().startswith("SIM::"):
            return VirtualSMUTransport(model=model, spy=self.spy)

        import pyvisa

        if self._rm is None:
            self._rm = pyvisa.ResourceManager()
        res = self._rm.open_resource(clean_res)
        res.timeout = timeout_ms
        res.read_termination = "\n"
        res.write_termination = "\n"
        self.spy.record("SYS", model, f"Opened Hardware VISA Session: {clean_res} (timeout={timeout_ms}ms)")
        return PyVISATransportSession(visa_resource=res, spy=self.spy, target_label=f"HW-{model}")
