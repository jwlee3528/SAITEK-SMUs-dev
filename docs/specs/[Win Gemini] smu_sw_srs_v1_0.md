# Tektronix / Keithley SMU 심화 제어·측정 SW 소프트웨어 사양서 (SRS v1.0)

* **문서 버전:** v1.0
* **기준 문서:** `1차 요구사항.pptx`, `SMU_심화프로그램_요구사항정의서_v2.pptx`, `requirements_feasibility_analysis.md`, Keithley SMU 8종 레퍼런스 매뉴얼
* **대상 모델 (8종):**
  * **24xx Touch 시리즈:** `2450`, `2460`, `2461`, `2470`
  * **26xx System 시리즈:** `2600B`, `2601B-PULSE`, `2606B`, `2651A`

---

## 1. 시스템 개요 및 핵심 아키텍처 결정

### 1.1 개발 목적 및 기술 스택
SMU(Source Measure Unit)의 동시 소스·측정(Source & Measure) 특성을 활용하여 단일 PC에서 동일 모델 1~10대를 동기화 제어하고, 모델별 특화 기능(고전압·고전류 펄스·고속 디지타이징), 사용자 정의 파형 출력, 안전 보호 및 대용량 데이터 분석을 통합 제공한다.

* **개발 언어 및 런타임:** Python 3.10+
* **장비 통신 및 제어:** `PyVISA` (USB/LAN/GPIB), Raw Socket (Port 5025 / DST Port 5030), 내장 **TSP(Test Script Processor) 엔진**
* **GUI 및 고속 렌더링:** `PySide6`, `PyQtGraph` (+ LTTB 다운샘플링)
* **데이터 처리 및 내보내기:** `NumPy`, `Pandas`, `OpenPyXL` (`.xlsx`), 스트리밍 CSV, `PyArrow`/`h5py` (`.parquet`/`.h5`)

### 1.2 핵심 아키텍처 설계 결정 (하드웨어 제약 해결)
매뉴얼 및 기술 타당성 분석에서 도출된 펌웨어·통신 제약을 해소하기 위해 아래 4가지 원칙을 표준 아키텍처로 확정한다.

1. **제어 엔진 100% TSP 단일화:** 24xx 계열의 Master-Slave(`TSP-Link`) 제어는 TSP 모드에서만 동작하며 26xx 계열은 TSP 전용이므로, 내부 제어 엔진을 TSP로 단일화한다. 24xx 장비 연결 시 `*LANG?`를 조회하여 SCPI 상태이면 `*LANG TSP` 전환 및 재부팅을 자동 안내·수행한다.
2. **세대별 파형 컴파일러 이원화 (Strategy Pattern):** 24xx(`smu.*`, TriggerFlow, Config List)와 26xx(`smua.*/smub.*`, Trigger Timer, List Sweep)의 문법 비호환성을 해결하기 위해 `TSP24xxGenerator`와 `TSP26xxGenerator`를 분리 구현한다. Master-Slave 클러스터는 동일 시리즈(24xx끼리 또는 26xx끼리)로 제한한다.
3. **연속(Continuous) 파형 모드의 이원화:** 고속 디지타이징 중 실시간 스트리밍으로 인한 지터(Jitter) 및 버퍼 오버플로우를 방지하기 위해 **저속 연속 모드(주기 $\ge 20\text{--}50\text{ ms}$)**와 **고속 버스트 반복 모드(주기 $< 20\text{ ms}$, $1\text{ MS/s}$)**를 명확히 분리한다.
4. **이진(Binary) 블록 전송 기본 채택:** 대용량 버퍼 추출 시 ASCII 텍스트 대신 IEEE 488.2 Binary(Single/Double Precision) 포맷을 기본 사용하여 전송 속도를 5~10배 단축한다.

---

## 2. 시스템 계층 구조 및 프로젝트 디렉토리

```text
SAITEK_SMU_SW/
├── docs/
│   ├── manuals/               # 8종 SMU 레퍼런스 매뉴얼 (2450~2651A)
│   └── specs/                 # 요구사항 정의서 및 본 사양서
├── configs/
│   └── profiles/              # 8종 SMU 하드웨어 스펙·SOA·명령어 프로파일 (YAML/JSON)
└── src/smu_app/
    ├── ui/                    # PySide6 동적 UI, 파형 편집기, PyQtGraph 뷰, Command Spy
    ├── core/
    │   ├── validator/         # SOA, Duty Cycle, Pulse Rate, Pre-Flight Check 검증기
    │   ├── compiler/          # TSP24xxGenerator, TSP26xxGenerator
    │   └── analyzer/          # 통계, dV/dI, Vth, Vbd, Ileak, Rise/Fall Time 분석기
    ├── drivers/
    │   ├── transport/         # PyVISA, Raw Socket(5025), DST(5030), Binary 파서
    │   ├── tsp_24xx/          # 2450, 2460, 2461, 2470 드라이버
    │   ├── tsp_26xx/          # 2600B, 2601B-PULSE, 2606B, 2651A 드라이버
    │   └── simulator/         # VirtualSMUDriver (물리 소자 모델 기반)
    └── export/                # Excel(.xlsx), Streaming CSV, Parquet/HDF5, Recipe I/O
```

---

## 3. 상세 기능 사양 (Functional Specifications)

### 3.1 통신 및 장비 관리 (`REQ-COM`)

| ID | 기능명 | 상세 구현 사양 | 비고 / 우선순위 |
| :--- | :--- | :--- | :---: |
| **REQ-COM-01** | 멀티 인터페이스 자동 탐색 | `PyVISA`를 통해 USB(USBTMC), GPIB, LAN(VXI-11, Raw Socket Port 5025) 연결 장비를 자동 스캔하고 리소스 목록을 표시한다. | Phase 1 (필수) |
| **REQ-COM-02** | 초기 진단 및 TSP 모드 전환 | 연결 즉시 `*IDN?`(모델·시리얼·펌웨어), 전원 주파수($50/60\text{ Hz}$), `*LANG?`를 조회한다. 24xx 모델이 SCPI 모드인 경우 `*LANG TSP` 전환 및 재부팅 절차를 안내/실행한다. | Phase 1 (필수) |
| **REQ-COM-03** | Dead Socket 클린업 | LAN 비정상 종료로 포트가 점유된 경우 DST(Port 5030)에 접속 후 종료하여 잔여 소켓을 강제 해제하는 복구 루틴을 제공한다. | Phase 1 (필수) |
| **REQ-COM-04** | 물리 장비 식별 (ID) | 다중 랙 환경에서 선택된 장비의 전면 LED/LXI ID 점멸 또는 비퍼(`beeper.beep()`)를 울려 물리적 위치를 식별한다. | Phase 1 (권장) |
| **REQ-COM-05** | Command Spy 콘솔 | UI 하단 접이식 패널에 PC-SMU 간 송수신 명령어, 응답 지연($\text{ms}$), Event Log/Error Queue 한글 파싱 결과를 실시간 표시하고 로그 파일로 저장한다. | Phase 1 (필수) |

---

### 3.2 모델 프로파일 및 동적 UI / 시뮬레이터 (`REQ-MDL`)

| ID | 기능명 | 상세 구현 사양 | 비고 / 우선순위 |
| :--- | :--- | :--- | :---: |
| **REQ-MDL-01** | 8종 모델 프로파일 DB | 각 모델의 DC/Pulse 한계, 4사분면(Source/Sink) 경계, 최소 NPLC, 최소 펄스 폭(`2601B-PULSE`: $10\text{ }\mu\text{s}$, `2461`: $150\text{ }\mu\text{s}$, `2651A`: $300\text{ }\mu\text{s}$), 채널 수, 버퍼 용량을 YAML/JSON으로 관리한다. | Phase 1 (필수) |
| **REQ-MDL-02** | 동적 UI 및 충돌 차단 | 연결/선택된 모델 프로파일에 따라 입력 범위와 메뉴를 동적 구성한다. 충돌 옵션은 자동 비활성화한다(예: `2601B-PULSE` Pulser ON 시 Auto Zero·High-C 비활성화, `26x4B` 모델에서 TSP-Link·DIO·Contact Check 비활성화). | Phase 1 (필수) |
| **REQ-MDL-03** | Virtual SMU Simulator | 실물 장비 없이 전 기능을 검증할 수 있도록 저항, 다이오드(Shockley 방정식), RC 과도응답 및 가우시안 노이즈가 적용된 가상 버퍼 데이터를 생성하는 `VirtualSMUDriver`를 구현한다. | Phase 1 (필수) |
| **REQ-MDL-04** | 레시피(Setup) 관리 | 장비 구성, 파형 테이블, 측정·안전 파라미터를 JSON/YAML 파일로 저장/불러오기 하며, 장비 내부 Setup 슬롯 저장/호출과 연계한다. | Phase 1 (필수) |

---

### 3.3 다중 장비 동기화 및 출력 결합 (`REQ-SYN`)

| ID | 기능명 | 상세 구현 사양 | 비고 / 우선순위 |
| :--- | :--- | :--- | :---: |
| **REQ-SYN-01** | TSP-Link 1~10대 동기화 | 동일 시리즈 1~10대 선택 시 Node 1(Master) ~ Node 10(Slave)을 자동 할당하고 `tsplink.initialize()`/`tsplink.reset()`을 수행한다. Master 1대만 PC와 통신하며, TSP-Link 3개 하드웨어 Trigger Sync Line으로 전 채널을 $\mu\text{s}$ 단위로 동시 구동한다. | Phase 2 (필수) |
| **REQ-SYN-02** | 미지원 모델 SW 동기화 | TSP-Link 미지원 모델(`2604B`, `2614B`, `2634B`) 감지 시 개별 LAN/USB 세션 기반 SW 동기화로 자동 전환하고, UI 상단에 **"SW 동기화 모드 ($1\text{--}20\text{ ms}$ 시작 편차 발생, 고속 펄스 동기화 불가)"** 배너를 명시한다. | Phase 2 (필수) |
| **REQ-SYN-03** | 논리 채널 매핑 및 역할 할당 | 1CH, 2CH(`2602B` 등), 4CH(`2606B`: 2모듈 $\times$ 2CH) 장비를 `Node-Channel` 단위로 추상화한다. 독립 병렬 측정 외에 3단자/다단자 소자 평가용 역할(Gate/Drain/Source) 지정 및 `tsplink.group` 그룹 제어를 지원한다. | Phase 2 (필수) |
| **REQ-SYN-04** | 출력 결합(Combining) 마법사 | 동일 모델 2대 병렬(전류원, 2600B $20\text{ A}$ / 2651A $100\text{ A}$) 또는 역극성 직렬(전압원, 2602B·2651A $80\text{ V}$) 결합을 지원한다. **안전 강제 규칙:** (1) 전압원 병렬·전류원 직렬·2606B 결합 원천 차단, (2) 인터랙티브 결선 확인 체크리스트 통과 시에만 출력 활성, (3) Slave Compliance를 Master 대비 $10\%$ 낮게 자동 설정, (4) 종료/에러 시 `High-Z` 동기 차단. | Phase 2 (필수) |

---

### 3.4 사용자 정의 파형·펄스 및 SOA 검증 (`REQ-WFM`)

| ID | 기능명 | 상세 구현 사양 | 비고 / 우선순위 |
| :--- | :--- | :--- | :---: |
| **REQ-WFM-01** | 표준 스위프 & 임의 파형 편집기 | Linear, Log(Asymptote 지원), Dual Sweep 및 사각파·삼각파·계단파 프리셋을 제공한다. 임의 파형은 `[Step, 시간(s), V/I 레벨, Limit, 측정여부]` 테이블 편집 및 CSV/Excel 불러오기, 실행 전 시간축 프리뷰와 총 소요 시간 계산을 지원한다. | Phase 1~2 (필수) |
| **REQ-WFM-02** | 반복 및 연속 모드 이원화 | 단발성(1회), 유한 반복($N$회), 무한 연속(Infinite) 모드를 제공한다. 연속 모드는 **(1) 저속 연속(주기 $\ge 20\text{--}50\text{ ms}$: 청크 실시간 스크롤)**과 **(2) 고속 버스트 반복(주기 $< 20\text{ ms}$: $1\text{--}N$사이클 버퍼 캡처 후 휴지기 일괄 전송 및 재시작)**으로 구분 동작한다. | Phase 2 (필수) |
| **REQ-WFM-03** | 고속 펄스 & 디지타이징 | • `2461`: Pulse Train/Sweep($10\text{ A}/100\text{ V}$, $\ge 150\text{ }\mu\text{s}$), 펄스 상단 측정, $1\text{ MS/s}$ 디지타이징.<br>• `2601B-PULSE`: 전용 Pulser($10\text{ A}/10\text{ V}$, $\ge 10\text{ }\mu\text{s}$), 듀얼 $1\text{ MS/s}$ 18-bit V/I 동시 캡처, 1회 버스트 최대 3,500 pts 제약 관리.<br>• `2651A`: $50\text{ A}/40\text{ V}$ 펄스($\ge 300\text{ }\mu\text{s}$), Fast ADC($10\text{ }\mu\text{s}$ 샘플링).<br>• `2600B`: Period Timer + Pulse Width Timer 조합 펄스 열 생성. | Phase 3 (필수) |
| **REQ-WFM-04** | SOA·Duty Cycle 사전 검증기 | 실행 전 PC에서 모델별 4사분면 SOA, 발열(주변 온도·Sink 동작 포함), 최대 Pulse Width/Duty Cycle을 계산하여 초과 시 실행을 차단한다. `2601B-PULSE`는 $\text{Pulse Rate} + (\text{Meas Rate} \times \text{Filter Count}) < 33,000$ 수식을 사전 검증한다. | Phase 1~3 (필수) |
| **REQ-WFM-05** | 극성 전환 지연 자동 보정 | `2600B`/`2651A`에서 $0\text{ V}$(양수 취급)에서 음수($-$) 펄스로 전환 시 발생하는 $100\text{ }\mu\text{s}$ 지연을 제거하기 위해, $0\text{ V}$ Bias를 $-1 \times 10^{-12}$(Negative Zero)로 자동 치환하는 옵션을 제공한다. | Phase 2 (권장) |

---

### 3.5 안전 보호 및 측정 정밀도 최적화 (`REQ-SAF`)

| ID | 기능명 | 상세 구현 사양 | 비고 / 우선순위 |
| :--- | :--- | :--- | :---: |
| **REQ-SAF-01** | 비동기 긴급 정지 (ALL STOP) | UI 상단 전역에 상시 노출한다. 클릭 즉시 VXI-11 Device Clear 또는 비동기 `abort`를 우선 전송하여 Hardware Trigger Model(`trigger.model.abort()`/`smua.abort()`)을 즉시 중단하고 전 노드 출력을 `OFF`로 차단한다. | Phase 1 (필수) |
| **REQ-SAF-02** | Pre-Flight Check 자동 진단 | 측정 시작 버튼 클릭 직후 본 출력 전 $0.5\text{ s}$ 이내에 **(1) 전 노드 `tsplink.state` 정상 여부, (2) $\pm 42\text{ V}$ 초과 시 Interlock 체결 여부, (3) 4-Wire 선택 시 Contact Check 통과 여부, (4) 버퍼 잔여 용량**을 일괄 진단한다. | Phase 1~2 (필수) |
| **REQ-SAF-03** | DUT/장비 보호 옵션 | • **Interlock 감시:** 미체결 시 $\pm 42\text{ V}$ 초과 설정 차단 및 에러 5074 사전 안내.<br>• **Output-Off State:** `NORMAL`, `HIGHZ`(배터리/커패시터 보호), `ZERO`, `GUARD` 선택.<br>• **OVP & Compliance:** 4-Wire 단선 폭주 방지 OVP 및 V/I/Power Compliance 도달 시 UI 즉시 하이라이트.<br>• **특화 보호:** 4-Wire `Contact Check`(임계 저항 $\Omega$ 설정), `2470` 전용 `Current Breakdown Protection`. | Phase 1 (필수) |
| **REQ-SAF-04** | 측정 정밀도·속도 최적화 | 2-Wire/4-Wire, Front/Rear 터미널, NPLC($0.001\text{--}25\text{ PLC}$ 및 Fast/Med/Normal/Hi-Acc 프리셋), Auto Zero(ON/OFF/ONCE), Source Readback, Digital Filter(Moving/Repeat/Median, 1~100), Relative Offset, High-Capacitance Mode(최대 $50\text{ }\mu\text{F}$), Offset-Compensated Ohms, `2651A` Sink Mode, `2461` 원격 `ACAL` 실행 및 내부 온도 변화($\pm 5^\circ\text{C}$) 모니터링을 제공한다. | Phase 1 (필수) |

---

### 3.6 시각화, 고속 수집, 분석 및 내보내기 (`REQ-DAT`)

| ID | 기능명 | 상세 구현 사양 | 비고 / 우선순위 |
| :--- | :--- | :--- | :---: |
| **REQ-DAT-01** | 버퍼 일괄 수집 및 Binary 파서 | 장비 내부 Reading Buffer(`24xx` 최대 100만 pts, `26xx` 14만 pts 이상)에 저장 후 **IEEE 488.2 Binary 블록**으로 일괄 추출한다. 각 포인트의 타임스탬프($1\text{ }\mu\text{s}$ 분해능)와 상태 비트(Compliance, Overrange, 4-Wire)를 함께 수집한다. | Phase 1~3 (필수) |
| **REQ-DAT-02** | 고속 그래프 및 가상 테이블 | `PyQtGraph` + **LTTB 다운샘플링**으로 10만~100만 포인트 데이터를 끊김 없이 렌더링한다. I-V Curve, Time-Domain(V/I/R/P), 다중 채널 Overlay/Swim Lanes, Linear/Log 축 전환, 듀얼 커서($\Delta X, \Delta Y, 1/R$), Run History 중첩 비교, Limit Test(Pass/Fail) 색상 구분 가상 스크롤 테이블을 제공한다. | Phase 1~2 (필수) |
| **REQ-DAT-03** | 심화 파라미터 자동 분석 | 기본 통계(Mean, Min, Max, Pk-Pk, StdDev) 외에 **미분 저항($dV/dI$)**, **문턱 전압($V_{th}$)**, **항복 전압($V_{bd}$)**, **누설 전류($I_{leak}$)**, **펄스 상승/하강 시간(Rise/Fall Time)** 자동 추출 모듈을 제공한다. | Phase 2~3 (권장) |
| **REQ-DAT-04** | 다중 포맷 내보내기 | • **Excel (`.xlsx`):** `Summary` 시트(장비 정보·레시피·통계·차트 이미지) + 채널별 `Raw Data` 시트 분리.<br>• **스트리밍 CSV:** 104만 행 초과 대용량 데이터 대응 청크 기반 고속 저장.<br>• **Parquet / HDF5:** 고속 후처리·빅데이터 분석용 바이너리 저장 옵션.<br>• **자동 백업 & 이미지:** `[날짜_시간_모델_테스트명]` 규칙 Auto-Export 및 PNG/SVG 그래프 저장. | Phase 1~3 (필수) |

---

## 4. 비기능 요구사항 (Non-Functional Requirements)

| 분류 | 항목 | 목표 기준 (Target Metric) |
| :--- | :--- | :--- |
| **성능 (Performance)** | 버퍼 데이터 전송 | IEEE 488.2 Binary 블록 전송 기준, ASCII 전송 대비 **5~10배 이상 단축** |
| **성능 (Performance)** | 대용량 그래프 렌더링 | 10만~100만 포인트 데이터 로드 및 줌/팬 시 LTTB 다운샘플링으로 UI 프리징 없음 |
| **안전성 (Safety)** | 긴급 정지 응답성 | `ALL STOP` 클릭 시 비동기 `abort` 전송으로 진행 중인 트리거 모델 즉각 중단 및 전 채널 출력 차단 |
| **신뢰성 (Reliability)** | 사전 진단 시간 | `Pre-Flight Check` (TSP-Link, Interlock, Contact Check, 버퍼 점검) $0.5\text{ s}$ 이내 완료 |
| **유지보수성 (Maintainability)** | 신규 모델 확장성 | 하드웨어 스펙·SOA 테이블이 외부 YAML/JSON 프로파일로 분리되어 신규 SMU 모델 추가 시 코어 코드 수정 최소화 |

---

## 5. 단계별 개발 로드맵 및 마일스톤

| 단계 | 핵심 개발 범위 | 주요 산출물 |
| :--- | :--- | :--- |
| **Phase 1**<br>기반·안전·단일 제어 | • `PyVISA`/Raw Socket(5025)/DST(5030) 통신 및 **TSP 단일화 드라이버**<br>• 8종 모델 프로파일 DB 및 **Virtual SMU Simulator**<br>• 전역 `ALL STOP`, Interlock, Output-Off, OVP/Compliance, Contact Check, **Command Spy**<br>• 단일 채널 DC 소스/측정, 기본 Sweep, Binary 버퍼 파서, I-V/Time 그래프 및 Excel/CSV 출력 | 단일 장비 제어 MVP, 시뮬레이터, 모델 프로파일 DB |
| **Phase 2**<br>다중 동기화·임의 파형 | • **TSP-Link Master-Slave(1~10대)** 동기화 및 `26x4B` SW 동기화 분기<br>• `TSP24xxGenerator` / `TSP26xxGenerator` 파형 컴파일러 및 임의 파형 편집기(저속 연속/고속 버스트 이원화)<br>• 다채널 매핑(`2606B` 4CH 등), 출력 결합 마법사, **Pre-Flight Check**, 극성 지연($-1 \times 10^{-12}$) 보정 | 다중 SMU 동기화 및 사용자 정의 파형 엔진 |
| **Phase 3**<br>고속 펄스·심화 분석 | • `2461` / `2601B-PULSE` / `2651A` 전용 고속 펄스 & $1\text{ MS/s}$ 디지타이저 모듈<br>• 모델별 **SOA·Duty Cycle·Pulse Rate 사전 검증기** 완성<br>• **심화 분석 모듈**($dV/dI, V_{th}, V_{bd}, I_{leak}$, Rise/Fall Time), Run History 비교, Parquet/HDF5 및 리포트 고도화 | 최종 릴리즈 패키지(v1.0) 및 사용자 매뉴얼 |