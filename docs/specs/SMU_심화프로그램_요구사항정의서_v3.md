# Tektronix / Keithley SMU 심화 제어·측정 프로그램 요구사항 정의서 (v3.0)

* **문서 버전**: v3.0 (1차 요구사항 + 8종 레퍼런스 매뉴얼 교차 분석 + 하드웨어 제약 해법 및 고도화 제안 반영판)
* **지원 대상 SMU (8종)**:
  * **24xx Touch 시리즈 (4종)**: `2450` (±200V/±1A), `2460` (±100V/±7A), `2461` (±100V/10A Pulse/1MS/s), `2470` (±1100V/10fA)
  * **26xx System 시리즈 (4종)**: `2600B` (2601B/2602B/2611B/2612B/2635B/2636B 등), `2601B-PULSE` (10A/10V 10µs Pulser), `2606B` (1U 4CH 고밀도), `2651A` (±40V/20A DC, 50A Pulse)
* **관련 파일**: [SMU_심화프로그램_요구사항정의서_v3.pptx](file:///f:/02.Project/08. SAITEK SMU SW dev/docs/요구사항/SMU_심화프로그램_요구사항정의서_v3.pptx)

---

## 1. 프로젝트 목적 및 시스템 아키텍처 (01. OVERVIEW)

### 1.1 개발 목적
SMU(Source Measure Unit)의 동시 소스(인가)·측정 특성을 활용하여 단일 PC에서 동일 시리즈/모델 1~10대를 통합 제어·동기화하고, 각 세부 모델의 고유 기능(고전압, 고전류 펄스, 고속 디지타이징 등), 사용자 정의 파형 출력, 하드웨어/DUT 안전 보호, 소자 특성 자동 분석 및 대용량 데이터 저장을 원스톱으로 제공한다.

### 1.2 통신 인터페이스 통합 지원
* **멀티 인터페이스 자동 탐색**: USB(USBTMC), LAN(TCP/IP), GPIB(IEEE-488) 연결 장비 자동 검색 및 리소스 매핑.
* **LAN 프로토콜 최적화**: 고속 제어용 Raw Socket(Port 5025), VXI-11(Port 1024), 비정상 종료 세션 강제 정리용 Dead Socket Termination(DST, Port 5030) 지원.
* **물리 장비 식별(ID)**: 다중 장비 랙 구성 시 LXI ID 점멸 또는 Beeper(`beeper.beep()`)로 대상 장비 위치 즉시 확인.
* **초기화 자동 진단**: 연결 시 `*IDN?`(모델/시리얼/펌웨어), 전원 주파수(50/60Hz), 현재 명령어 모드(`*LANG?`) 자동 조회.

### 1.3 100% TSP 엔진 단일화 및 세대별 이중 컴파일러 (v3.0 확정)
* **100% TSP 엔진 단일화**: 26xx 계열(TSP 전용) 및 24xx 계열의 다중 동기화(`TSP-Link`는 TSP 모드에서만 동작) 제약을 해결하기 위해 SW 내부 제어 엔진을 **100% TSP(Test Script Processor)** 기반으로 단일화.
* **24xx TSP 모드 자동 전환**: 2450/2460/2461/2470 연결 시 `*LANG?` 조회 후 `SCPI` 상태이면 `*LANG TSP` 자동 전환 및 재부팅 가이드 제공.
* **세대별 이중 파형 컴파일러 (Strategy 패턴)**:
  * **`TSP24xxGenerator`**: `smu.*` 객체 + `Source Config List` + `TriggerFlow`(`trigger.model.setblock`) 블록 구조로 컴파일.
  * **`TSP26xxGenerator`**: `smua.*`/`smub.*` 객체 + `List Sweep` + `Trigger Timer`(`trigger.timer[N].delaylist`) 구조로 컴파일.
* **Command Spy & 에러 큐 파서**: UI 하단 접이식 패널에 PC ↔ SMU 송수신 명령어, 응답 시간(ms), 장비 내부 Error Queue 한글 원인 가이드를 실시간 표시·로깅.

### 1.4 모델 프로파일 기반 동적 UI 및 물리 시뮬레이터
* **모델별 기능 자동 활성화**: 연결/선택된 모델의 프로파일 DB(전압·전류·전력·펄스·디지타이저·Contact Check 등)에 따라 UI 입력 범위 및 메뉴 동적 구성.
* **설정 충돌 사전 차단**: 예: `2601B-PULSE` Pulser 활성화 시 미지원 옵션(Auto Zero, High-C 등) 자동 비활성화.
* **레시피(Setup) 관리**: 장비 설정, 파형 정의, 분석/저장 파라미터를 YAML/JSON 파일로 저장·불러오기 및 장비 내부 Setup 슬롯 연계.
* **물리 모델 기반 Virtual SMU Simulator (v3.0 고도화)**: 실물 장비 미연결 상태에서도 단순 UI 테스트를 넘어 가상 소자 모델(선형 저항 Ohm, 다이오드 Shockley 방정식, RC 과도응답 + 가우시안 노이즈)을 계산해 실제 버퍼와 동일한 데이터를 생성·검증.

---

## 2. 지원 대상 SMU 모델별 핵심 기능 매트릭스 (02. MODEL MATRIX)

| 모델군 | 최대 출력 (DC / Pulse) | 명령어 체계 (SW 내부) | TSP-Link 동기화 | 심화 SW 핵심 구현 대상 (모델 특화 기능) |
| :--- | :--- | :--- | :---: | :--- |
| **2450** | ±200V / ±1A DC (20W) | SCPI, TSP (**TSP 제어**) | 지원 | 6.5-Digit 범용 SMU, TriggerFlow, Config List, 최대 100만 pts 버퍼 |
| **2460** | ±100V / ±7A DC (100W) | SCPI, TSP (**TSP 제어**) | 지원 | 고전류(7A) 4-Quadrant 소스/싱크, 후면 스크류 터미널 지원 |
| **2461** | ±100V / 7A DC, **10A Pulse (1kW)** | SCPI, TSP (**TSP 제어**) | 지원 | 전용 Pulse Train/Sweep, **1 MS/s 고속 디지타이저**, Contact Check, Autocalibration(`ACAL`) |
| **2470** | **±1100V** / ±1A DC (10 fA) | SCPI, TSP (**TSP 제어**) | 지원 | SiC/GaN 고전압 절연·누설 측정, **Current Breakdown Protection(절연 파괴 보호)** |
| **2600B 시리즈** | 40V~200V / 1.5~3A (10A Pulse) | TSP 전용 | 지원 (`26x4B` 제외) | 1~2CH(`smua`/`smub`), 100 aA(2635B/36B), Power Limit, 타이머 펄스, 직/병렬 결합 |
| **2601B-PULSE** | 40V/3A DC, **10A/10V Pulser (10µs)** | TSP 전용 | 지원 | 전용 고속 Pulser(10µs), **듀얼 1 MS/s 18-bit 디지타이저(V/I 동시 캡처, 버스트 3,500pts)** |
| **2606B** | ±20V / ±3A (2-Quadrant) | TSP 전용 | 지원 | **1U 4채널 고밀도 랙마운트(2모듈×2CH)**, 물리 전면패널 없음(원격 제어 필수) |
| **2651A** | ±40V / 20A DC, **50A Pulse (2000W)** | TSP 전용 | 지원 | 초고전력 SMU, **Fast ADC(1µs) + Integrating ADC 듀얼**, 전용 Sink Mode, 2대 결합 100A/80V |

---

## 3. 다중 SMU(1~10대) Master-Slave 동기화 및 출력 결합 (03. MULTI-SMU SYNC)

### 3.1 Master-Slave 구조 및 TSP-Link 고속 동기화
* **동일 시리즈 클러스터링 (1~10대)**: 24xx와 26xx 간 TSP 데이터 테이블 구조 비호환을 방지하기 위해 동일 시리즈/모델 1~10대를 Node 1(Master) ~ Node 10(Slave)으로 자동 할당 및 `tsplink.initialize()` 수행.
* **단일 PC 세션 병렬 실행**: PC는 Master(Node 1) 1대와만 통신(LAN Raw Socket 5025 권장)하고, Master가 TSP-Link 케이블을 통해 전체 Slave 노드를 `node[N].execute()`로 병렬 제어.
* **하드웨어 트리거 동기화**: TSP-Link 내장 3개 Trigger Sync Line을 활용해 전 채널 동시 소스 인가 및 동시 측정(1µs 이하 동기화).
* **TSP-Link 미지원 모델(`2604B/2614B/2634B`) 예외 분기**: 해당 모델 감지 시 개별 LAN/USB 세션 기반 멀티스레드 SW 동기화로 자동 전환하고, **UI 상단에 타이밍 편차(1~20ms Skew 발생 가능, 고속 펄스 동기화 불가) 경고 배너를 명시 표시**.

### 3.2 논리 채널 매핑
* 1CH(2450/2460/2461/2470/2651A), 2CH(2602B/2612B/2636B), 4CH(2606B) 장비를 `Node-Channel` 단위 논리 채널로 추상화.
* **채널 역할(Role) 할당**: 10개 DUT 동시 병렬 테스트 모드 및 다단자 소자(FET Gate/Drain/Source 연동 스위프) 모드 지원.

### 3.3 출력 결합(Combining SMUs) 마법사 및 오결선 방지 게이트 (v3.0 안전 강화)
* **병렬(고전류 2배) / 직렬(고전압 2배) 결합**: 동일 모델 2대 결합으로 2651A 2대 → 100A 펄스 또는 80V 펄스 확장.
* **물리 결선 확인 인터랙티브 게이트**: 잘못된 배선 상태에서 출력이 켜져 장비가 손상되는 것을 막기 위해 **단계별 배선 다이어그램 + 사용자 물리 결선 확인 체크박스**를 모두 통과해야만 출력 활성화 허용.
* **하드웨어 안전 규칙 및 10% 마진 자동화**: 전압원 병렬 금지 / 전류원 직렬 금지 / 2606B 결합 금지 강제, Slave의 Compliance를 Master 대비 10% 낮게 자동 설정, 종료/비상정지 시 안전 순서 동기 차단.

---

## 4. 세부 모델별 파형·펄스 구현 및 SOA 사전 안전 검증 (04~05. WAVEFORM & PULSE)

### 4.1 표준 스위프 & 임의 파형 편집기
* **표준 파형 템플릿**: Linear Staircase, Logarithmic Staircase(Asymptote 지원), Dual(왕복) Sweep, 사각파/삼각파/계단파 프리셋.
* **임의 파형 (List Sweep)**: `[Step, 시간(s), 전압/전류(V/A), Limit, 측정여부(Measure/Digitize)]` 테이블 직접 편집 및 CSV/Excel 임포트.
* **파형 미리보기(Preview)**: 출력 전 Time-Domain 파형 그래프 프리뷰, 총 소요 시간 및 수집 포인트 수 자동 계산.

### 4.2 연속(Continuous) 출력 시 데이터 수집 모드 이원화 (v3.0 아키텍처 보완)
* 단발성(Single-Shot, 1회) 및 지정 횟수(Finite $N$회) 외에 **연속(Continuous) 출력 모드**를 통신 대역폭 및 타이밍 제약에 맞춰 2가지로 이원화:
  1. **저속 연속 스트리밍 모드 (스텝 주기 $\ge$ 20~50ms)**: 파형 연속 출력과 동시에 Reading Buffer를 청크(Chunk) 단위로 폴링하여 실시간 스크롤 그래프 갱신.
  2. **고속 버스트 캡처 반복 모드 (스텝 주기 < 20ms, 1 MS/s 디지타이징)**: 통신 인터럽트로 인한 펄스 타이밍 Jitter를 방지하고 2601B-PULSE 버스트 한계(3,500pts)를 준수하기 위해, **[1~N 사이클 고속 출력 및 내부 버퍼 캡처 $\rightarrow$ 짧은 휴지기 동안 PC로 Binary 일괄 전송 및 그래프 갱신 $\rightarrow$ 다음 버스트 재시작]** 루프로 동작.

### 4.3 고속 펄스 & 디지타이징 특화 구현
* **2461 고속 펄스**: Pulse Train / Pulse Sweep, 최대 10A/100V(최소 폭 150µs), 펄스 상단(Top) 적분 측정 및 1 MS/s 디지타이징.
* **2601B-PULSE 전용 Pulser**: 10A/10V 초고속 펄스(최소 10µs), 듀얼 1 MS/s 18-bit 디지타이저 V/I 동시 캡처, 1회 버스트 최대 3,500 pts 제약 자동 관리.
* **2651A 초고전력 펄스**: 최대 50A/40V(최소 300µs), Fast ADC(1µs~10µs 샘플링) 과도응답 캡처.
* **2600B 타이머 기반 펄스**: Trigger Timer 2개(Period Timer + Pulse Width Timer) 조합 펄스 열 생성.

### 4.4 SOA · Duty Cycle 사전 검증 및 정밀 보정
* **SOA(안전 동작 영역) 사전 차단**: 모델별 DC/Pulse 허용 영역(예: 2601B 10A@20V 최대 1.8ms·1% Duty / 2651A 50A@40V 최대 300µs·1% Duty) 초과 입력 시 실행 전 원천 차단.
* **극성 전환 지연(100µs) 자동 보정**: 2600B/2651A에서 0V $\rightarrow$ 음수(-) 펄스 전환 시 발생하는 100µs 지연을 방지하기 위해 0V Bias를 `-1e-12`(Negative Zero)로 자동 치환 옵션 제공.
* **2601B-PULSE Rate 한계 검증**: `Pulse Rate + Meas Rate × Filter Count < 33,000` 제약 사전 검증.

---

## 5. 안전 보호(Safety) 및 측정 정밀도 최적화 (06. SAFETY & PRECISION)

### 5.1 논블로킹 긴급 정지 및 Pre-Flight 자동 진단 (v3.0 강화)
* **비동기 즉시 긴급 정지 (Non-blocking ALL STOP)**:
  * 긴 소프트웨어 루프 대신 장비의 **Hardware Trigger Model**로 파형을 구동하고, 일반 명령 큐 블로킹을 우회하는 **비동기 `abort` / VXI-11 Device Clear 신호**를 전송하여 클릭 즉시 전 노드(`Master + Slaves`) 트리거 중단 및 Output OFF 보장.
* **Pre-Flight Check (측정 시작 전 0.5초 자동 진단 루틴)**:
  * 측정 시작 버튼 클릭 시 본 파형 인가 직전 자동으로 ① 전 노드 `tsplink.state` 온라인 여부, ② ±42V 초과 설정 시 후면 Interlock 체결 여부, ③ 4-Wire 설정 시 Contact Check 통과 여부, ④ 내부 Reading Buffer 가용 용량을 0.5초 내 일괄 점검.
* **Interlock / Output-Off State / OVP / Contact Check / 2470 Breakdown Protection**:
  * 에너지 저장 DUT(배터리/커패시터) 보호용 Output-Off 상태(`Normal`, `High-Z`, `Zero`, `Guard`) 선택.
  * 4-Wire Sense 단선 시 전압 폭주 방지용 OVP 및 V/I/Power Compliance 실시간 UI 하이라이트.
  * 2461/2600B/2606B/2651A 4-Wire 접촉 저항 검사 및 2470 Current Breakdown Protection 연동.

### 5.2 측정 정밀도 및 특수 부하 제어
* 2-Wire / 4-Wire 및 Front / Rear 터미널 전환, NPLC(0.001~25) 및 속도 프리셋.
* Auto Zero(ON/OFF/ONCE), Source Readback, 디지털 필터(Moving/Repeating Average, Median), Relative Offset.
* High-Capacitance Mode(최대 50µF 발진 방지), Offset-Compensated Ohms(열기전력 제거), 2651A Sink Mode, 2461 원격 Autocalibration(`ACAL`).

---

## 6. 시각화, 소자 특성 자동 분석 및 초고속 데이터 출력 (07. VISUALIZATION & DATA)

### 6.1 실시간 그래프 및 반도체/펄스 파라미터 자동 추출 (v3.0 고도화)
* **다중 그래프 및 커서**: I-V Curve, Time-Domain(V/I/R/P), 다중 채널 Overlay / Swim Lanes, Linear $\leftrightarrow$ Log 즉시 전환, 듀얼 커서($\Delta X, \Delta Y, 1/R$), Run History 중첩 비교.
* **반도체/배터리 핵심 파라미터 자동 추출**:
  * I-V 측정 데이터에서 **미분 저항($dV/dI$)**, **문턱 전압($V_{th}$)**, **항복 전압($V_{bd}$)**, **누설 전류($I_{leak}$)** 자동 산출.
* **펄스 과도응답 메트릭 자동 추출**:
  * 디지타이저 파형에서 **상승/하강 시간(Rise/Fall Time, 10%~90%)**, **Overshoot(%)**, **Settling Time**, **Pulse Top 구간 평균 V/I** 자동 산출.

### 6.2 IEEE 488.2 Binary 버퍼 고속 수집 및 가상 테이블 (v3.0 고도화)
* **IEEE 488.2 Binary 블록 전송 기본 적용**: 10만~100만 포인트 버퍼 추출 시 ASCII 텍스트 전송 대비 **수집 속도를 5~10배 단축**하는 Single/Double Precision Binary 전송 적용.
* **1µs 타임스탬프 & 상태 비트 & Limit Test**: 각 포인트의 타임스탬프, 상태 플래그(Compliance, Overrange, 4-Wire), 상·하한 판정(Pass/Fail 색상 구분) 표시.
* **LTTB 다운샘플링 & 가상 스크롤 테이블**: 100만 포인트 이상에서도 UI 지연 없이 즉각 확대/축소 및 테이블 탐색 지원.

### 6.3 다중 포맷 내보내기 (Excel / 스트리밍 CSV / Parquet / HDF5)
* **구조화된 Excel (`.xlsx`) 리포트**: ① `Summary` 시트(장비 정보, 설정값, 자동 추출 파라미터 요약, 그래프 차트 이미지) + ② 채널별 `Raw Data` 시트 자동 분리.
* **대용량 스트리밍 CSV**: Excel 행 제한(1,048,576행) 초과 시 병목 없는 청크 스트리밍 CSV 저장.
* **Parquet / HDF5 고성능 포맷 추가**: 대용량 파형 데이터의 초고속 입출력 및 Python/MATLAB 후처리를 위한 `Parquet`, `HDF5` 저장 지원.

---

## 7. 단계별 개발 로드맵 (09. ROADMAP)

* **Phase 1 (기반 아키텍처 · 안전 · 단일 장비 제어 & 시뮬레이터)**
  * TSP 단일화 통신/드라이버 계층(USB/LAN 5025·5030/GPIB), 24xx TSP 자동 전환기, 8종 모델 프로파일 & SOA DB
  * 물리 모델 기반 `VirtualSMUDriver` 시뮬레이터 및 실시간 `Command Spy` 콘솔
  * 논블로킹 `ALL STOP`, `Pre-Flight Check` 진단, Interlock/Output-Off/Contact Check, 기본 DC/Sweep, LTTB 그래프 및 Excel/CSV 저장
* **Phase 2 (다중 장비 TSP-Link 동기화 · 이중 파형 컴파일러 · 출력 결합)**
  * TSP-Link 1~10대 Master-Slave 자동 초기화, 3 Sync Line 하드웨어 동기화, `26x4B` SW 동기화 예외 분기 및 경고
  * `TSP24xxGenerator` / `TSP26xxGenerator` 이중 파형 컴파일러, 임의 파형 편집기/프리뷰, 저속 스트리밍 vs 고속 버스트 반복 모드
  * 2606B(4CH)/2602B(2CH) 논리 채널 매핑 및 직/병렬 출력 결합 결선 확인 게이트 마법사
* **Phase 3 (고속 펄스/디지타이저 · 특성 자동 분석 · Binary/Parquet 대용량 고도화)**
  * 2461(10A/1MS/s), 2601B-PULSE(10µs/3,500pts 버스트 관리), 2651A(50A/Fast ADC) 전용 고속 펄스/디지타이저 모듈
  * SOA / Duty Cycle / Pulse Rate 사전 검증 차단기 및 반도체($dV/dI, V_{th}, V_{bd}$)/펄스(Rise/Fall Time) 자동 분석기
  * IEEE 488.2 Binary 고속 버퍼 추출 최적화, Parquet/HDF5 내보내기, Run History 비교 리포트 완성
