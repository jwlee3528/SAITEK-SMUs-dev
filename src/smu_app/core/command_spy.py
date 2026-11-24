"""Real-time TSP/SCPI Command Spy & Instrument Error Queue Parser (v3.0 Value-Add)."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Callable


# Common Keithley 24xx / 26xx error code explanations in Korean
KEITHLEY_ERROR_GUIDE_KO: dict[int, str] = {
    0: "정상 (에러 없음)",
    -102: "문법 오류 (Syntax error): 전송된 명령어 철자나 파라미터 형식을 확인하세요.",
    -113: "정의되지 않은 헤더 (Undefined header): 현재 명령어 세트(TSP/SCPI)가 일치하는지 확인하세요.",
    -222: "데이터 범위 초과 (Data out of range): 해당 모델의 최대 전압/전류/NPLC 범위를 초과했습니다.",
    802: "인터락 미체결 출력 차단 (Output blocked by interlock): 후면 안전 인터락 케이블을 확인하세요.",
    5007: "허용 전력/범위 초과 (Operation would exceed safe operating area): 전압×전류 SOA 한계를 초과했습니다.",
    5061: "고용량 모드 제약 (Operation not permitted while High-C mode is enabled).",
    5074: "후면 인터락 미체결 (Interlock not engaged for >42V output): ±42V 초과 출력 시 인터락 체결이 필수입니다.",
    5076: "측정 Range에 맞춰 Source Limit가 자동 조정됨 (Source Limit changed due to new measure Range).",
    5079: "전면 바나나 단자 초저전류 안내 (Consider using rear triaxial connectors for <1µA): 전면 단자 사용 시 오토레인지 하한을 10µA로 설정하면 경고가 해제됩니다.",
}


@dataclass(frozen=True)
class SpyEntry:
    """Single logged traffic record between PC and SMU."""

    timestamp: datetime
    direction: str  # "TX", "RX", "ERR", "SYS"
    target: str
    payload: str
    elapsed_ms: float = 0.0
    error_code: int | None = None
    guide_ko: str = ""

    def format_line(self) -> str:
        ts = self.timestamp.strftime("%H:%M:%S.%f")[:-3]
        ms_str = f" ({self.elapsed_ms:.2f}ms)" if self.elapsed_ms > 0 else ""
        guide_str = f" -> [{self.guide_ko}]" if self.guide_ko else ""
        return f"[{ts}] [{self.direction:<3}] [{self.target}] {self.payload}{ms_str}{guide_str}"


class CommandSpy:
    """Thread-safe ring-buffer traffic logger with live observer callbacks for GUI console."""

    def __init__(self, max_entries: int = 5000) -> None:
        self._entries: deque[SpyEntry] = deque(maxlen=max_entries)
        self._listeners: list[Callable[[SpyEntry], None]] = []

    def subscribe(self, callback: Callable[[SpyEntry], None]) -> None:
        if callback not in self._listeners:
            self._listeners.append(callback)

    def unsubscribe(self, callback: Callable[[SpyEntry], None]) -> None:
        if callback in self._listeners:
            self._listeners.remove(callback)

    def record(
        self,
        direction: str,
        target: str,
        payload: str,
        elapsed_ms: float = 0.0,
        error_code: int | None = None,
    ) -> SpyEntry:
        guide = ""
        if error_code is not None and error_code != 0:
            guide = KEITHLEY_ERROR_GUIDE_KO.get(
                error_code, f"장비 에러 코드 ({error_code}): 레퍼런스 매뉴얼 Error Summary를 참조하세요."
            )
        entry = SpyEntry(
            timestamp=datetime.now(),
            direction=direction,
            target=target,
            payload=payload.strip(),
            elapsed_ms=elapsed_ms,
            error_code=error_code,
            guide_ko=guide,
        )
        self._entries.append(entry)
        for cb in list(self._listeners):
            try:
                cb(entry)
            except Exception:
                pass
        return entry

    def entries(self) -> list[SpyEntry]:
        return list(self._entries)

    def clear(self) -> None:
        self._entries.clear()

    def export_log(self, file_path: str) -> None:
        with open(file_path, "w", encoding="utf-8") as fh:
            for e in self._entries:
                fh.write(e.format_line() + "\n")
