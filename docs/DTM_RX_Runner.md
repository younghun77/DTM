# DTM RX Runner

> **목적**: nRF52840 Dongle을 이용하여 BLE Direct Test Mode (DTM)의 **RX 측정**을 자동화하고,
> 동시에 **Test Sample(DUT)** 의 BT TX 동작과 **테스트 시점(sync)을 일치**시키기 위한 통합 GUI 도구.
>
> 기존 nRF Connect "Direct Test Mode" 데스크톱 앱이 **dongle 단독 제어만** 제공하던 것과 달리,
> DTM RX Runner는 **Dongle ↔ Test Sample** 양쪽을 한 번의 클릭으로 동기화하여
> 양산 라인 / 신뢰성 시험에서 동일한 측정 조건을 반복 재현할 수 있게 한다.

---

## 1. 한눈에 보는 가치 (Why)

| 기존 도구 (nRF Connect DTM) | DTM RX Runner |
|---|---|
| Dongle 한쪽만 RX/TX 제어 | **Dongle RX + DUT TX 동시 제어** |
| 운영자가 수동으로 DUT 측 스크립트 실행 → 타이밍 오차 발생 | **SSH로 DUT TX 스크립트 자동 트리거**, RX 윈도와 자동 정렬 |
| 결과를 따로 기록 | **CSV 자동 저장 + Plot/통계** (min/avg/max) |
| 1회성 측정 | **AUTO RUN N회 반복**, 실패 시 DUT 로그 자동 수집 |
| 고정 시나리오 | **MANUAL 모드**: SSH/Ethernet/스크립트/Reboot frame을 운영자가 지정 |

핵심 한 줄: **"Test Sample의 TX와 Dongle의 RX 측정 시점을 항상 일치시킨다."**

---

## 2. 주요 특징 (Features)

### 2.1 Test Sync (테스트 동기화)
- **START RX** 한 번으로 다음이 순서대로 수행됨:
  1. 영구(persistent) SSH 세션을 통해 DUT에 **TX script 실행 명령** 송신
  2. 짧은 startup delay 후 Dongle에 **`Receiver Test` (DTM)** 명령 송신
  3. 지정된 RX duration 동안 패킷 수신, **`Test End`** 로 카운트 회수
- 즉, **DUT TX 시작 → Dongle RX 시작 → 동시 종료** 가 한 시퀀스로 묶임.
- AUTO RUN 모드에서는 위 사이클을 N회 자동 반복하며, 매 iteration마다 동기화가 보장됨.

### 2.2 Test Script 선택
- DUT에는 OEM 별로 여러 BT TX/Off 스크립트가 존재 (`bt_tx_test_39ch.sh`, `bt_test_off.sh`, etc.).
- MANUAL 모드에서:
  - **SSH로 원격 디렉터리(예: `/opt/factory/rootfs/usr/bin`)를 listing** 하여
    실행 가능한 `*.sh` 목록을 가져옴.
  - **TX script** 와 **Test End script** 를 운영자가 GUI에서 골라서 적용 가능.
  - 즉, **새 OEM 샘플이 들어와도 코드 수정 없이** 스크립트만 선택해서 동일한 측정 가능.

### 2.3 3가지 동작 모드
| 모드 | DUT 제어 | 용도 |
|---|---|---|
| **DUT-LINK** (기본) | 자동 (기본 BMW Telematics 설정) | 표준 양산 라인 |
| **MANUAL** | 운영자 지정 SSH/Ethernet/스크립트/Reboot frame | 신규 OEM, 디버깅 |
| **STANDALONE** | 없음 (Dongle 단독) | 외부 신호원 + Dongle만으로 RX 측정 |

### 2.4 실패 자동 진단
- AUTO RUN 도중 `rx_count == 0` 또는 알려진 실패 패턴(예: `[ERROR][BT]`, firmware crash 시그니처) 발견 시
  - DUT 로그(`bt_test.log`, `bt_bootstrap.log`) **SFTP 자동 다운로드**
  - `/proc/tty/driver/serial` 덤프
  - `FAILURE_SUMMARY.txt` 생성

### 2.5 운영자 편의성
- **Dongle 자동 탐지** (VID `0x1915`), 실패 시 수동 COM 포트 picker fallback
- **결과 CSV 자동 저장** (`results/YY-MM-DD/rx_result.csv`)
- **Plot CSV**: min/max/avg 통계 + 그래프 (matplotlib, 없을 시 Tk fallback)
- **STAGE 인디케이터**: 현재 진행 단계(`RX running`, `cooldown 30s`, `rebooting DUT` …) 실시간 표시
- 진행 중인 액션 버튼에 highlight ring 표시

---

## 3. 아키텍처 (Architecture)

### 3.1 구성 요소 관계

```
                        ┌────────────────────────────────────────┐
                        │            Operator PC (Windows)       │
                        │   ┌────────────────────────────────┐   │
                        │   │     DTM RX Runner (Tk GUI)     │   │
                        │   │                                │   │
                        │   │  ┌─────────────┐  ┌──────────┐ │   │
                        │   │  │ DTM Engine  │  │  DUT     │ │   │
                        │   │  │ (2-wire)    │  │  Control │ │   │
                        │   │  └──────┬──────┘  └────┬─────┘ │   │
                        │   └─────────┼──────────────┼───────┘   │
                        │             │              │           │
                        │   USB CDC-ACM│              │           │
                        │   19200 8N1 │              │           │
                        └─────────────┼──────────────┼───────────┘
                                      │              │
                                      │              │ ① SSH  (paramiko, key auth)
                                      │              │    └─ TX/Off script 실행, 로그 SFTP
                                      │              │ ② TCP  (KU-style binary frame)
                                      │              │    └─ Reboot / Service-enable frame
                                      ▼              ▼
                            ┌──────────────────┐  ┌────────────────────────────┐
                            │ nRF52840 Dongle  │  │      Test Sample (DUT)     │
                            │  (DTM firmware)  │  │   BT chipset + Linux       │
                            │                  │  │   /opt/factory/.../*.sh    │
                            │   2.4 GHz BLE    │◀═│   bt_tx_test_39ch.sh, …    │
                            │   RX 측정        │  │                            │
                            └──────────────────┘  └────────────────────────────┘
                                       ▲ 2.4GHz BLE radio ▲
                                       └──────TX/RX───────┘
```

- **DTM Engine**: Bluetooth Core Vol 6, Part F 의 2-wire UART 프로토콜 구현
  (2-byte command / 2-byte event).
- **DUT Control**: `dut_control.py` 모듈 — paramiko 기반 영구 SSH 세션 + raw TCP control frame.
- **연결 채널 3종**
  1. **USB CDC-ACM (PC ↔ Dongle)**: DTM 명령 송수신
  2. **SSH (PC ↔ DUT)**: 스크립트 실행, 로그 다운로드 (key-based auth)
  3. **TCP (PC ↔ DUT, 기본 :20000)**: KU 프로토콜 Reboot / Service-enable frame
  4. **2.4 GHz BLE (DUT ↔ Dongle)**: 실제 측정 대상 RF 경로

### 3.2 내부 모듈 구조

```
tools/
├── dtm_rx_runner.py     ← GUI + DTM 2-wire 엔진 + 시퀀서
└── dut_control.py       ← SSHSession, run_bt_tx_test, bt_test_off,
                            reboot_dut, fetch_dut_logs, analyze_dut_logs
```

`DtmRxRunner` 클래스가 모든 UI/시퀀스를 보유하고,
DUT 측 기능은 `dut_control` 모듈을 통해 공유 SSH 세션 위에서 호출된다
(매 iteration마다 새로 handshaking 하지 않아 측정 jitter 감소).

---

## 4. Test Sequence (테스트 시퀀스)

### 4.1 단일 RX 측정 (START RX → END RX)

```
Operator       GUI (DTM RX Runner)         Dongle              DUT (Test Sample)
   │                  │                       │                        │
   │── START RX ─────▶│                       │                        │
   │                  │── SSH exec ──────────────────────────────────▶│ TX script 실행
   │                  │                       │                        │  (bt_tx_test_*.sh)
   │                  │  ⏱ startup_delay 3s   │                        │  → BLE TX 시작
   │                  │── DTM Receiver Test ─▶│                        │
   │                  │◀── status OK ─────────│                        │
   │                  │                       │◀═════ BLE PRBS9 ══════│
   │                  │   (RX dur 동안 수신)  │                        │
   │── END RX ───────▶│                       │                        │
   │                  │── DTM Test End ──────▶│                        │
   │                  │◀── packet_count ──────│                        │
   │                  │── CSV 저장            │                        │
   │                  │── SSH exec ──────────────────────────────────▶│ Test End script 실행
   │                  │                       │                        │  (bt_test_off.sh)
   │                  │                       │                        │  → BLE TX 중지
   │                  │── STAGE: idle         │                        │
   │◀── 결과/Log 표시 │                       │                        │
```

### 4.2 AUTO RUN (N회 반복, with reboot)

```
for i in 1..N:
    [DUT-TX-START]   SSH:  TX script 실행 (async, blocking 아님)
    [SETTLE]         3s startup delay
    [DONGLE]         CMD_RESET → CMD_RECEIVER(ch, len, PRBS9)
    [RX WINDOW]      RX duration (interruptible by STOP)
    [DONGLE]         CMD_END  → packet_count 회수
    [CSV]            results/YY-MM-DD/rx_result.csv append

    if rx_count == 0:                     # 즉시 실패 처리
        fetch_dut_logs(SFTP) → analyze → FAILURE_SUMMARY.txt
        break

    if reboot_between:                    # AUTO RUN (with reboot)
        SSH:  Test End script (bt_test_off.sh)   # HCI 해제
        1s settle
        TCP:  SERVICE_ENABLE frame → ACK
        TCP:  REBOOT frame          → DUT reboot
    else:                                 # AUTO (no reboot)
        SSH:  Test End script

    [LOG SCAN]       fetch_dut_logs → known failure patterns?
                     (SSH 불안정 시 3회 retry, 실패해도 loop 계속)
    [COOLDOWN]       N초 대기 (interruptible)

[FINISH]            STAGE: idle, AUTO 버튼 복구
```

### 4.3 MANUAL 모드 셋업 시퀀스

```
1) Mode 토글: DUT-LINK → MANUAL
2) "DUT settings…" 클릭 → MANUAL DUT CONNECTION 다이얼로그
   ├─ SSH host / user / PEM key (Browse…)
   ├─ Ethernet host / port  (reboot frame 대상)
   ├─ Reboot frame (hex)    ← 비우면 REBOOT/AUTO RUN 자동 비활성화
   ├─ Service-enable frame  (optional)
   └─ Script dir
3) [Connect SSH] → 원격 *.sh 목록 자동 fetch
4) TX script / Test End script를 [Select script] 로 선택
5) [Apply settings] → 활성 설정 로그 출력
6) 다이얼로그 [Close] 후 START RX / AUTO RUN 사용
```

---

## 5. DTM 2-wire 프로토콜 요약 (참고)

Bluetooth Core, Vol 6, Part F.

| 방향 | Byte 0 | Byte 1 |
|---|---|---|
| Tester → DUT (command) | `[7:6]=CMD` (00 Reset / 01 RX / 10 TX / 11 End), `[5:0]=freq (0..39)` | `[7:2]=length`, `[1:0]=PKT (00=PRBS9)` |
| DUT → Tester (event)   | `[7]=0` status (`[0]=0 OK / 1 err`), `[7]=1` packet-report | packet count LSB (상위는 `byte0[6:0]`) |

- 통신: USB CDC-ACM, **19200 8N1**
- 채널 freq(MHz) = `2402 + 2 × N`

---

## 6. 산출물 (Outputs)

```
results/
└── YY-MM-DD/
    ├── rx_result.csv                      ← test_index, timestamp, channel, length, rx_count
    └── logs/
        └── HHMMSS_iter<N>_<reason>/
            ├── bt_test.log
            ├── bt_bootstrap.log
            └── FAILURE_SUMMARY.txt        ← 채널/길이/findings/serial driver dump
```

- CSV는 [Plot CSV] 버튼으로 즉시 그래프화 (min/avg/max 표시).
- 실패 아티팩트는 동일 폴더에 자동 누적 → 사후 분석 용이.

---

## 7. 운영자 체크리스트 (Quick Start)

1. nRF52840 Dongle을 PC USB에 연결 → 상단 상태등이 **● Dongle: COMxx** 로 바뀌는지 확인.
2. **Channel / Length / Iterations / RX dur / Cooldown** 입력.
3. 표준 라인이면 **DUT-LINK** 모드 그대로, 신규 샘플이면 **MANUAL** 로 토글 후 DUT 설정.
4. 단발 측정: **START RX → END RX**
   반복 측정: **AUTO RUN** (reboot 포함) 또는 **AUTO (no reboot)**.
5. 종료 후 **Open results folder** 로 CSV / 실패 로그 확인.

---

## 8. 변경/확장 포인트 (For Maintainers)

- **새 OEM 추가** → MANUAL 모드에서 SSH/Eth/script만 지정. 코드 변경 불필요.
- **다른 Reboot 프로토콜** → MANUAL의 Reboot frame(hex) 입력으로 대체 가능 (`dut_control.reboot_dut` 가
  `reboot_frame` / `service_enable_frame` 인자 지원).
- **결과 저장 경로 변경** → 환경변수 `DTM_RX_RESULT_BASE` 로 override.
- **DTM Dongle 변경** → `_find_dongle_port()` 의 VID/PID 필터 확장.

---

*문서 작성: DTM RX Runner v1.x — `tools/dtm_rx_runner.py` 기준.*
