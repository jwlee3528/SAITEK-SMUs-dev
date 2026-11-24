# Tektronix (Keithley) SMU Control & Data Analysis Suite (v3.0)

Tektronix / Keithley **24xx Touch 시리즈 4종(`2450`, `2460`, `2461`, `2470`)** 및 **26xx System 시리즈 4종(`2600B`, `2601B-PULSE`, `2606B`, `2651A`)** 총 8종 SMU를 100% TSP 아키텍처로 통합 제어하고, 다중 장비(1~10대) 동기화, 안전 보호(SOA/Pre-Flight/ALL STOP), 실시간 특성 분석 및 고속 데이터 출력(Excel/CSV/Parquet)을 수행하는 Python 프로젝트입니다.

## 프로젝트 폴더 구조 (v3.0 Architecture)

```text
├── docs/                           # 문서 및 매뉴얼
│   ├── manuals/                    # 8종 SMU 모델별 레퍼런스 매뉴얼 분류 보관
│   │   ├── 2450/                   # 2450 Reference Manual
│   │   ├── 2460/                   # 2460 Reference Manual
│   │   ├── 2461/                   # 2461 (10A Pulse / 1MS/s) Reference Manual
│   │   ├── 2470/                   # 2470 (1100V High Voltage) Reference Manual
│   │   ├── 2600B/                  # 2600B Series Reference Manual
│   │   ├── 2601B_PULSE/            # 2601B-PULSE (10us Pulser) Reference Manual
│   │   ├── 2606B/                  # 2606B (1U 4-CH High Density) Reference Manual
│   │   └── 2651A/                  # 2651A (50A High Power Pulse) Reference Manual
│   ├── 요구사항/                    # 요구사항 정의서 PPTX (1차, v2.0, v3.0)
│   └── specs/                      # 개발 기준 상세 요구사항 사양서 (v3.0 Markdown)
│
├── configs/
│   ├── profiles/
│   │   └── smu_profiles.yaml       # ★ 8종 SMU 하드웨어 스펙, 펄스 한계 및 SOA 포락선 DB
│   ├── instruments/                # 장비 연결 프로필
│   └── sequences/                  # 측정 레시피 예시
│
├── src/
│   └── smu_app/
│       ├── main.py                 # GUI 및 CLI 헤드리스 데모 실행 진입점
│       ├── core/
│       │   ├── profiles.py         # 8종 모델 프로파일 & SOA 로더 (ProfileRegistry)
│       │   ├── models.py           # Phase 1~3 공통 데이터 구조 (LogicalChannel, SweepRecipe 등)
│       │   ├── command_spy.py      # 실시간 TSP 패킷/응답시간 로거 및 한글 에러 가이드
│       │   └── visa_manager.py     # 멀티 인터페이스(USB/LAN 5025·5030/GPIB) & IEEE 488.2 Binary 코덱
│       ├── compiler/
│       │   └── tsp_compiler.py     # 세대별 이중 TSP 파형 컴파일러 (TSP24xxGenerator / TSP26xxGenerator)
│       ├── simulator/
│       │   └── virtual_smu.py      # 물리 소자 모델(저항, 다이오드 Shockley, RC 과도응답, 고전압 절연) 시뮬레이터
│       ├── drivers/
│       │   └── base_smu.py         # 100% TSP 통합 드라이버 (*LANG TSP 자동 전환, 논블로킹 ALL STOP)
│       ├── safety/
│       │   └── safety_manager.py   # SOA/Duty Cycle/2601B Rate 사전 차단기 & 0.5초 Pre-Flight Check
│       ├── sync/
│       │   └── cluster_manager.py  # Phase 2 기반: 1~10대 TSP-Link 클러스터, 26x4B SW 동기화 분기, 출력 결합 게이트
│       ├── features/
│       │   └── sweep_engine.py     # Pre-Flight -> 다중 노드 TSP 실행 -> 자동 특성 분석 통합 파이프라인
│       ├── analysis/
│       │   └── iv_pulse_analyzer.py# LTTB 고속 다운샘플링 및 반도체(dV/dI, Vth, Vbd, Ileak)·펄스 파라미터 자동 추출
│       ├── storage/
│       │   └── exporter.py         # 구조화된 Excel(.xlsx + 내장 차트), 대용량 스트리밍 CSV, Parquet 저장기
│       └── ui/
│           └── main_window.py      # PyQt6 + PyQtGraph 통합 데스크톱 대시보드
│
├── data/                           # 측정 데이터 및 자동 생성 리포트 저장소
└── tests/
    └── unit/
        └── test_phase1_suite.py    # Phase 1-3 핵심 아키텍처 자동화 단위/통합 테스트 스위트
```

## 실행 방법 (Quick Start)

1. **데스크톱 GUI 대시보드 실행**:
   ```powershell
   $env:PYTHONPATH="src"
   python -m smu_app.main
   ```
2. **CLI 헤드리스 시뮬레이션 및 리포트(Excel/CSV/Parquet) 자동 생성 데모**:
   ```powershell
   $env:PYTHONPATH="src"
   python -m smu_app.main --headless-demo --model 2461 --nodes 2
   ```
3. **단위 및 통합 테스트 실행**:
   ```powershell
   $env:PYTHONPATH="src"
   python -m unittest discover -s tests/unit -p "test_*.py" -v
   ```
